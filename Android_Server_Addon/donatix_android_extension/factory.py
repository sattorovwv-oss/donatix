from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from donatix import accounts, google_auth
from donatix.api import ApiError, _limit
from donatix.app import create_app as create_site
from donatix.deps import check_csrf, csrf_token, get_conn, session_user

from .fcm import Sender
from .push import Worker
from .store import Store, site_readonly

PREFIX = "/api/v1/android"
COOKIE = "dx_android_oauth"


class HandoffMiddleware:
    """Only mobile-tagged successful OAuth responses get a different redirect.

    Runs outside the existing SessionMiddleware, observes its scope after the
    original handler, and never changes that handler or serializes site cookies.
    """

    def __init__(self, app, secret):
        self.app, self.secret = app, secret

    def sign(self, ticket, expires):
        raw = f"{ticket}.{expires}"
        return raw + "." + hmac.new(self.secret.encode(), ("android-oauth:" + raw).encode(), hashlib.sha256).hexdigest()

    def ticket(self, raw):
        try:
            ticket, exp, _ = raw.split(".")
            if len(ticket) != 43 or int(exp) <= time.time() or int(exp) > time.time() + 610:
                return None
            return ticket if secrets.compare_digest(raw, self.sign(ticket, exp)) else None
        except (ValueError, TypeError):
            return None

    async def __call__(self, scope, receive, send):
        relevant = scope["type"] == "http" and scope.get("path") in ("/auth/google/callback", "/login/code")
        ticket = self.ticket(Request(scope).cookies.get(COOKIE, "")) if relevant else None

        async def response(message):
            if message["type"] == "http.response.start" and ticket:
                session = scope.get("session", {})
                headers = message.get("headers", [])
                location = next((v for k, v in headers if k.lower() == b"location"), b"")
                if message["status"] == 303 and location in (b"/panel", b"/admin") and session.get("user_id") and session.get("sid"):
                    message = dict(message)
                    message["headers"] = [(k, (PREFIX + "/oauth/confirm/" + ticket).encode() if k.lower() == b"location" else v)
                                          for k, v in headers]
                    message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, response)


class Prepare(BaseModel):
    challenge: str = Field(pattern=r"^[0-9a-f]{64}$")


class Claim(BaseModel):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class Device(BaseModel):
    device_id: str = Field(min_length=32, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    token: str = Field(min_length=20, max_length=4096)
    platform: str = Field(default="android", pattern=r"^android$")


class Remove(BaseModel):
    device_id: str = Field(min_length=32, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(config=None, supplier=None, *, store_path=None, sender=None, start_push=True):
    app = create_site(config, supplier)
    config = app.state.config
    path = store_path or os.environ.get("DONATIX_ANDROID_DB")
    if not path:
        raise ValueError("Set DONATIX_ANDROID_DB to a separate extension-owned SQLite file")
    if Path(path).resolve() == Path(config.db_path).resolve():
        raise ValueError("The Android database must be different from the site database")
    store = Store(path, config.secret_key)
    sender = sender or Sender(os.environ.get("DONATIX_FIREBASE_CREDENTIALS", ""),
                              os.environ.get("DONATIX_ANDROID_FIREBASE_PROJECT", "donatix-660fc"))
    worker = Worker(config, store, sender)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            if start_push:
                worker.start()
            try:
                yield
            finally:
                worker.stop()

    app.router.lifespan_context = lifespan
    app.state.android_store, app.state.android_worker = store, worker
    app.add_middleware(HandoffMiddleware, secret=config.secret_key)
    router = APIRouter(prefix=PREFIX, tags=["Optional Android integration"])

    def user(request: Request, conn=Depends(get_conn)):
        current = session_user(request, conn)
        if current is None:
            raise ApiError("Войдите в приложение.", "unauthorized", 401)
        return current

    def header_csrf(request):
        expected, sent = request.session.get("csrf", ""), request.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(expected, sent):
            raise ApiError("Обновите сессию приложения.", "csrf", 403)
        origin = request.headers.get("origin")
        if origin and origin != config.base_url.rstrip("/"):
            raise ApiError("Недопустимый источник запроса.", "origin", 403)

    def active_flow(c, ticket):
        c.execute("DELETE FROM flows WHERE expires<=?", (int(time.time()),))
        row = c.execute("SELECT * FROM flows WHERE id=?", (ticket,)).fetchone()
        if row is None:
            raise ApiError("Ссылка входа устарела.", "expired", 410)
        return row

    def page(body):
        return HTMLResponse('<!doctype html><html lang="ru"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Donatix — вход в приложение</title></head><body style="font:18px system-ui;padding:24px;max-width:480px;margin:auto">'
            + body + '</body></html>', headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})

    @router.get("/config")
    def configuration():
        return {"ok": True, "android_extension_version": 1, "google_enabled": google_auth.enabled(config),
                "fcm_enabled": sender.configured}

    @router.get("/session")
    def session(request: Request, current=Depends(user)):
        return {"ok": True, "user_id": current["id"], "csrf": csrf_token(request), "configured": sender.configured}

    @router.get("/notifications")
    def notifications(current=Depends(user)):
        with site_readonly(config.db_path) as c:
            rows = c.execute("SELECT id,text,link,read_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",
                             (current["id"],)).fetchall()
        return {"ok": True, "items": [dict(r) for r in rows]}

    @router.post("/oauth/prepare")
    def prepare(body: Prepare, request: Request):
        _limit(request, "login", request.client.host if request.client else "?")
        if not google_auth.enabled(config):
            raise ApiError("Google-вход не настроен владельцем сайта.", "disabled", 503)
        ticket = secrets.token_urlsafe(32)
        with store.transaction() as c:
            c.execute("DELETE FROM flows WHERE expires<=?", (int(time.time()),))
            c.execute("INSERT INTO flows(id,challenge,expires) VALUES(?,?,?)", (ticket, body.challenge, int(time.time()) + 600))
        return {"ok": True, "ticket": ticket, "url": config.base_url.rstrip("/") + PREFIX + "/oauth/start/" + ticket}

    @router.get("/oauth/start/{ticket}")
    def start(ticket: str, request: Request):
        with store.connect() as c:
            row = active_flow(c, ticket)
        response = RedirectResponse("/auth/google", 303)
        signature = HandoffMiddleware(None, config.secret_key).sign(ticket, row["expires"])
        response.set_cookie(COOKIE, signature, max_age=600, httponly=True, secure=config.cookie_secure, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/oauth/confirm/{ticket}")
    def confirmation(ticket: str, request: Request, current=Depends(user)):
        with store.connect() as c:
            active_flow(c, ticket)
        return page('<h1>Вход в приложение Donatix</h1><p>Аккаунт: <b>' + escape(current["email"]) + '</b></p>'
            '<p>Подтвердите, если вы начали этот вход в приложении.</p><form method="post">'
            '<input type="hidden" name="csrf" value="' + escape(csrf_token(request)) + '">'
            '<button style="padding:16px;background:#5046ee;color:white;border:0;border-radius:12px">Подтвердить вход</button></form>')

    @router.post("/oauth/confirm/{ticket}")
    async def confirm(ticket: str, request: Request, current=Depends(user)):
        await check_csrf(request)
        signed = HandoffMiddleware(None, config.secret_key).ticket(request.cookies.get(COOKIE, ""))
        if signed != ticket:
            raise ApiError("Начните вход заново из приложения.", "flow_mismatch", 403)
        with store.transaction() as c:
            active_flow(c, ticket)
            if c.execute("UPDATE flows SET user_id=? WHERE id=? AND user_id IS NULL", (current["id"], ticket)).rowcount != 1:
                raise ApiError("Вход уже подтверждён.", "used", 410)
        response = page('<h1>Вход подтверждён</h1><p>Вернитесь в приложение Donatix.</p><a href="donatix://oauth">Открыть приложение</a>')
        response.delete_cookie(COOKIE)
        return response

    @router.post("/oauth/claim")
    def claim(body: Claim, request: Request, conn=Depends(get_conn)):
        _limit(request, "status", "android-oauth:" + body.ticket)
        with store.transaction() as c:
            row = active_flow(c, body.ticket)
            if not secrets.compare_digest(row["challenge"], hashlib.sha256(body.verifier.encode()).hexdigest()):
                raise ApiError("Недействительное подтверждение входа.", "verifier", 403)
            if row["user_id"] is None:
                return {"ok": True, "pending": True}
            current = accounts.get_user(conn, row["user_id"])
            if current is None or current["status"] == "blocked":
                raise ApiError("Аккаунт недоступен.", "blocked", 403)
            c.execute("DELETE FROM flows WHERE id=?", (body.ticket,))
        from donatix.web import _record_login
        request.session.clear()
        request.session["user_id"] = current["id"]
        _record_login(conn, request, current["id"])
        return {"ok": True, "pending": False, "csrf": csrf_token(request)}

    @router.post("/push/register")
    def register(body: Device, request: Request, current=Depends(user)):
        header_csrf(request)
        _limit(request, "account", str(current["id"]))
        sid = request.session["sid"]
        binding = hmac.new(config.secret_key.encode(), f"android-fcm:{current['id']}:{sid}:{body.device_id}".encode(), hashlib.sha256).hexdigest()
        token_hash = hashlib.sha256(body.token.encode()).hexdigest()
        with site_readonly(config.db_path) as site:
            after = site.execute("SELECT COALESCE(MAX(id),0) FROM notifications").fetchone()[0]
        with store.transaction() as c:
            old = c.execute("SELECT * FROM devices WHERE id=?", (body.device_id,)).fetchone()
            c.execute("DELETE FROM devices WHERE token_hash=? AND id<>?", (token_hash, body.device_id))
            if old and (old["sid"], old["user_id"], old["token_hash"]) != (sid, current["id"], token_hash):
                c.execute("DELETE FROM devices WHERE id=?", (body.device_id,))
            if old and (old["sid"], old["user_id"], old["token_hash"]) == (sid, current["id"], token_hash):
                after = old["after_id"]
            c.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                      "user_id=excluded.user_id,sid=excluded.sid,token=excluded.token,token_hash=excluded.token_hash,"
                      "binding=excluded.binding,after_id=excluded.after_id",
                      (body.device_id, current["id"], sid, store.encrypt(body.token), token_hash, binding, after))
        return {"ok": True, "configured": sender.configured, "binding": binding}

    @router.post("/push/unregister")
    def unregister(body: Remove, request: Request, current=Depends(user)):
        header_csrf(request)
        with store.connect() as c:
            c.execute("DELETE FROM devices WHERE id=? AND user_id=? AND sid=?",
                      (body.device_id, current["id"], request.session["sid"]))
        return {"ok": True}

    app.include_router(router)
    return app
