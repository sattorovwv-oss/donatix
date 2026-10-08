"""Сайт: главная, регистрация/вход, документация и кабинет клиента."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import accounts, cache, catalog, db, orders, popular, referrals, sitecfg
from .config import PAY_METHODS, Config
from .deps import INDEXABLE, LoginRequired, check_csrf, flash, get_config, get_conn, render, session_user
from .money import apply_markup, fmt, order_total_micro, to_decimal
from .suppliers import KINDS, region_title

router = APIRouter(include_in_schema=False)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def panel_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Row:
    user = session_user(request, conn)
    if user is None:
        if request.method == "GET":
            query = "?" + request.url.query if request.url.query else ""
            request.session["next"] = _safe_next(request.url.path + query)
        raise LoginRequired()
    return user


class Guest(dict):
    """Гость: смотрит каталог и цены без входа. Купить или пополнить — после регистрации."""


def _guest() -> Guest:
    return Guest(id=0, login="Гость", email="", role="guest", status="active", balance_micro=0, tier="bronze",
                 markup_override=None, dcoin=0, guest=True)


def viewer(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Кто смотрит страницу каталога: клиент или гость."""
    return session_user(request, conn) or _guest()


def _safe_next(url: str) -> str:
    """Куда вернуть после входа/регистрации: только свои страницы кабинета."""
    url = (url or "").strip()
    return url if url.startswith("/panel") and not url.startswith("//") and len(url) < 500 else ""


# ── Публичные страницы ───────────────────────────────────────


@router.get("/")
def home(request: Request, conn=Depends(get_conn), config: Config = Depends(get_config)):
    def build():
        cats = [dict(c) for c in catalog.categories(conn)]
        by_kind: dict[str, list] = {}
        for c in cats:
            by_kind.setdefault(c["kind"], []).append(c)
        return by_kind, sum(c["n"] for c in cats), _price_examples(conn, config)

    by_kind, total, examples = cache.get_or_set("home", 120, build)
    return render(request, "home.html", {
        "user": session_user(request, conn),
        "by_kind": by_kind,
        "total_products": total,
        "markups": config.markups,
        "examples": examples,
        "faq": FAQ + (BOT_FAQ if sitecfg.client_bots_enabled(conn) else []),
        "popular": popular.services(conn),
        "client_bots": sitecfg.client_bots_enabled(conn),
        "ref_percent": referrals.percent(conn),
    })


FAQ = [
    ("Что если заказ не выполнится?", "Деньги сразу вернутся на баланс."),
    ("Нужен ли программист?", "Нет. Заказывать можно вручную в панели, а свой Telegram-бот собирается "
                              "в конструкторе без кода."),
    ("Как быстро выполняются заказы?", "Обычно за несколько секунд."),
    ("Как пополнить баланс?", "Картами Алиф, Душанбе Сити, Эсхата или криптовалютой USDT (TRC20, BEP20) — "
                              "на любую сумму."),
]

BOT_FAQ = [
    ("Что такое конструктор ботов?", "Это ваш собственный Telegram-бот-магазин. Клиенты покупают у вас алмазы "
                                     "Free Fire, UC для PUBG Mobile, Telegram Stars и Premium, а заказы "
                                     "выполняются автоматически с вашего баланса Donatix."),
    ("Сколько стоит конструктор?", "Подключение бесплатное. Вы платите только за проданные товары по нашей "
                                   "цене, а наценку в боте ставите сами — разница ваш заработок."),
    ("Как создать своего бота?", "Сделайте 5 заказов на сайте — после этого откроется «Мой Telegram-бот». "
                                 "Создайте бота в @BotFather и вставьте токен в кабинете или отправьте его нашему "
                                 "боту поддержки. Бот запустится сразу с готовыми играми: Free Fire СНГ и "
                                 "Индонезия, PUBG Mobile."),
    ("Как клиенты платят в моём боте?", "На ваши реквизиты: карта, Душанбе Сити, USDT. Вы подтверждаете оплату "
                                        "в боте, и баланс клиента пополняется. Контакт поддержки тоже ваш."),
    ("Что будет, если в боте нет продаж?", "Если продаж долго нет, бот трижды предупредит вас, а потом "
                                           "отключится. Включить его снова или подключить нового может только "
                                           "администратор — напишите в поддержку."),
]


def _price_examples(conn, config: Config) -> list[tuple[str, str]]:
    """Несколько реальных цен из каталога для главной (по базовой наценке)."""
    rows = []
    for pid, qty, label in (("tg-stars", 1000, "Telegram Stars, 1000 звёзд"),
                            ("tg-premium-3", 1, "Telegram Premium, 3 месяца"),
                            ("tg-premium-12", 1, "Telegram Premium, 12 месяцев")):
        p = catalog.get_product(conn, pid)
        if p is None:
            continue
        markup = config.kind_markups.get(p["kind"], config.markups["bronze"])
        total = order_total_micro(apply_markup(to_decimal(p["base_price"]), markup), qty)
        rows.append((label, fmt(total)))
    for c in catalog.categories(conn):
        if c["kind"] == "topup" and len(rows) < 5:
            items = catalog.list_products(conn, category_id=c["category_id"], limit=1)
            if items:
                p = items[0]
                total = order_total_micro(apply_markup(to_decimal(p["base_price"]), config.markups["bronze"]), 1)
                rows.append((f"{p['category_name']} — {p['name']}", fmt(total)))
    return rows


@router.get("/privacy")
def privacy(request: Request, conn=Depends(get_conn)):
    return render(request, "legal.html", {"user": session_user(request, conn), "page": "privacy"})


@router.get("/terms")
def terms(request: Request, conn=Depends(get_conn)):
    return render(request, "legal.html", {"user": session_user(request, conn), "page": "terms"})


# ── Приложение для телефона (Android APK / «Установить на экран») ──

APP_SHORTCUTS = [("Каталог", "/panel/catalog"), ("Пополнить баланс", "/panel/balance"), ("Мои заказы", "/panel/orders")]


@router.get("/manifest.webmanifest")
def web_manifest(config: Config = Depends(get_config)):
    """Описание приложения: по нему APK (TWA) и «Установить приложение» берут имя, иконки и цвета."""
    icons = [{"src": f"/static/app/{n}", "sizes": s, "type": "image/png", "purpose": purpose}
             for n, s, purpose in (("icon-192.png", "192x192", "any"), ("icon-512.png", "512x512", "any"),
                                   ("maskable-192.png", "192x192", "maskable"),
                                   ("maskable-512.png", "512x512", "maskable"))]
    body = {
        "id": "/panel", "name": config.site_name, "short_name": config.site_name,
        "description": "Алмазы, UC, Telegram Stars, Steam — пополнение за секунды",
        "lang": "ru", "dir": "ltr", "start_url": "/panel?source=app", "scope": "/",
        "display": "standalone", "display_override": ["standalone", "minimal-ui"], "orientation": "portrait",
        "background_color": "#0e1016", "theme_color": "#4338ca", "categories": ["shopping", "games"],
        "icons": icons,
        "shortcuts": [{"name": n, "url": u + "?source=app", "icons": [icons[0]]} for n, u in APP_SHORTCUTS],
    }
    return JSONResponse(body, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})


SERVICE_WORKER = """// Donatix: страницы всегда свежие из сети; без интернета — экран «Нет связи»; push-уведомления.
const CACHE = "dx-v2";
const OFFLINE = "/offline";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll([OFFLINE, "/static/app/icon-192.png", "/static/logo.svg"])));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET" || new URL(r.url).origin !== location.origin) return;
  if (r.mode === "navigate") {           // страницы: только сеть (баланс и заказы не должны быть старыми)
    e.respondWith(fetch(r).catch(() => caches.match(OFFLINE)));
  } else if (r.url.includes("/static/")) {   // css, js, иконки: из кэша, обновляем в фоне
    e.respondWith(caches.open(CACHE).then(c => c.match(r).then(hit => {
      const net = fetch(r).then(resp => { if (resp.ok) c.put(r, resp.clone()); return resp; }).catch(() => hit);
      return hit || net;
    })));
  }
});
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Donatix", {
    body: d.body || "", tag: d.tag, renotify: true, data: { url: d.url || "/panel/notifications" },
    icon: "/static/app/icon-192.png", badge: "/static/app/badge-96.png", vibrate: [80, 40, 80],
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/panel", location.origin).href;
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const w of list) {
      if (w.url.startsWith(location.origin) && "focus" in w) { w.navigate(url); return w.focus(); }
    }
    return clients.openWindow(url);
  }));
});
"""


@router.get("/sw.js")
def service_worker():
    return Response(SERVICE_WORKER, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/offline")
def offline_page(request: Request):
    return render(request, "offline.html", {"user": None, "noindex": True})


@router.get("/.well-known/assetlinks.json")
def asset_links(config: Config = Depends(get_config)):
    """Связь сайта с APK (Trusted Web Activity): без неё в приложении сверху видна адресная строка."""
    prints = [x.strip().upper() for x in config.android_sha256.split(",") if x.strip()]
    if not config.android_package or not prints:
        return JSONResponse([])
    return JSONResponse([{"relation": ["delegate_permission/common.handle_all_urls"],
                          "target": {"namespace": "android_app", "package_name": config.android_package,
                                     "sha256_cert_fingerprints": prints}}])


@router.get("/panel/push/key")
def push_key(user=Depends(panel_user), conn=Depends(get_conn)):
    from . import webpush
    pair = webpush.keys(conn)
    return JSONResponse({"ok": bool(pair), "key": pair[1] if pair else "", "devices": webpush.count(conn, user["id"])})


@router.post("/panel/push/subscribe", dependencies=[Depends(check_csrf)])
def push_subscribe(request: Request, endpoint: str = Form(""), p256dh: str = Form(""), auth: str = Form(""),
                   user=Depends(panel_user), conn=Depends(get_conn)):
    from . import webpush
    try:
        webpush.subscribe(conn, user["id"], endpoint, p256dh, auth, request.headers.get("user-agent", ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    return JSONResponse({"ok": True})


@router.post("/panel/push/unsubscribe", dependencies=[Depends(check_csrf)])
def push_unsubscribe(endpoint: str = Form(""), user=Depends(panel_user), conn=Depends(get_conn)):
    from . import webpush
    webpush.unsubscribe(conn, user["id"], endpoint)
    return JSONResponse({"ok": True})


@router.post("/panel/push/test", dependencies=[Depends(check_csrf)])
def push_test(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
              config: Config = Depends(get_config)):
    from . import webpush
    if request.app.state.limiter.hit("account", f"pushtest{user['id']}") is not None:
        return JSONResponse({"ok": False, "error": "Слишком часто. Подождите минуту."}, 429)
    n = webpush.send(conn, config, user["id"], "Уведомления работают! Так придёт сообщение о заказе и пополнении.",
                     "/panel")
    return JSONResponse({"ok": bool(n), "devices": n})


@router.get("/robots.txt")
def robots(config: Config = Depends(get_config)):
    body = ("User-agent: *\n"
            "Allow: /$\nAllow: /docs\nAllow: /privacy\nAllow: /terms\nAllow: /register\n"
            "Allow: /static/\nAllow: /media/\n"
            "Disallow: /panel\nDisallow: /admin\nDisallow: /api/\nDisallow: /login\n\n"
            f"Sitemap: {config.base_url}/sitemap.xml\n")
    return PlainTextResponse(body)


@router.get("/sitemap.xml")
def sitemap(conn=Depends(get_conn), config: Config = Depends(get_config)):
    from xml.sax.saxutils import escape

    synced = (db.get_setting(conn, "catalog_synced_at") or db.now())[:10]
    urls = "".join(
        f"<url><loc>{escape(config.base_url + path)}</loc><lastmod>{synced}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, (freq, prio) in INDEXABLE.items()
    )
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    media_type="application/xml")


@router.get("/docs")
def docs(request: Request, conn=Depends(get_conn), config: Config = Depends(get_config)):
    return render(request, "docs.html", {"user": session_user(request, conn), "base_url": config.base_url})


@router.get("/register")
def register_form(request: Request, conn=Depends(get_conn)):

    if session_user(request, conn):
        return _redirect("/panel")
    nxt = _safe_next(request.query_params.get("next", ""))
    if nxt:
        request.session["next"] = nxt
    return render(request, "register.html", {"form": {}, "closed": not sitecfg.registration_open(conn),
                                             "next": request.session.get("next", "")})


@router.post("/register", dependencies=[Depends(check_csrf)])
def register(
    request: Request,
    email: str = Form(""),
    login: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    project: str = Form(""),
    conn=Depends(get_conn),
    config: Config = Depends(get_config),
):

    form = {"email": email, "login": login, "project": project}
    if not sitecfg.registration_open(conn):
        return render(request, "register.html", {"form": form, "closed": True,
                                                 "error": "Регистрация временно закрыта."}, 403)
    wait = request.app.state.limiter.hit("login", _ip(request))
    if wait is not None:
        return render(request, "register.html", {"form": form, "error": "Слишком много попыток. Подождите."}, 429)
    if password != password2:
        return render(request, "register.html", {"form": form, "error": "Пароли не совпадают."}, 400)
    try:
        user_id = accounts.create_user(
            conn, email=email, login=login, password=password, project=project,
            status="pending" if config.require_approval else "active",
        )
    except accounts.AccountError as exc:
        return render(request, "register.html", {"form": form, "error": str(exc)}, 400)
    from . import referrals
    referrals.attach(conn, user_id, request.session.pop("ref", None))
    request.session["user_id"] = user_id
    _record_login(conn, request, user_id)
    from .tgbot import user_event
    from .worker import notify_event
    if config.require_approval:
        notify_event(conn, config, user_event(conn, user_id))
        flash(request, "Аккаунт создан. Мы проверим заявку и активируем доступ — обычно в течение дня.")
    else:
        flash(request, "Аккаунт создан. Добро пожаловать!")
    return _redirect(_safe_next(request.session.pop("next", "")) or "/panel")


@router.get("/login")
def login_form(request: Request, conn=Depends(get_conn)):
    if session_user(request, conn):
        return _redirect("/panel")
    nxt = _safe_next(request.query_params.get("next", ""))
    if nxt:
        request.session["next"] = nxt
    return render(request, "login.html", {"form": {}, "next": request.session.get("next", "")})


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(request: Request, email: str = Form(""), password: str = Form(""), conn=Depends(get_conn)):
    wait = request.app.state.limiter.hit("login", _ip(request))
    if wait is not None:
        return render(request, "login.html", {"form": {"email": email},
                                              "error": "Слишком много попыток входа. Подождите 15 минут."}, 429)
    user = accounts.authenticate(conn, email, password)
    if user is None:
        return render(request, "login.html", {"form": {"email": email}, "error": "Неверный email или пароль."}, 400)
    if user["status"] == "blocked":
        return render(request, "login.html", {"form": {"email": email}, "error": "Аккаунт заблокирован."}, 403)
    return _finish_login(request, conn, user)


def _finish_login(request: Request, conn, user) -> RedirectResponse:
    """Вход после пароля или Google. Админу — ещё код из админ-бота в Telegram (если включено)."""

    config = request.app.state.config
    nxt = _safe_next(request.session.get("next", ""))   # вернуть туда, где человек хотел купить
    request.session.clear()
    if user["role"] == "admin" and sitecfg.admin_2fa_active(conn, config):
        import secrets as _secrets
        import time as _time

        from .worker import notify_admin
        code = f"{_secrets.randbelow(1_000_000):06d}"
        # Код, срок и попытки — на сервере. В cookie только случайный номер попытки входа:
        # cookie подписан, но не зашифрован — хеш кода из него подобрали бы за секунду.
        nonce = _secrets.token_urlsafe(24)
        _clean_2fa(conn)
        db.set_setting(conn, f"2fa.{nonce}", json.dumps(
            {"uid": user["id"], "exp": _time.time() + 300, "tries": 0, "hash": _code_hash(config, nonce, code)}))
        request.session["pending_2fa"] = nonce
        notify_admin(config, f"🔐 Код входа в админку: {code}\nДействует 5 минут. IP: {_ip(request)}. "
                             "Если это не вы — смените пароль.", secret=True)
        return _redirect("/login/code")
    request.session["user_id"] = user["id"]
    _record_login(conn, request, user["id"])
    return _redirect("/admin" if user["role"] == "admin" else (nxt or "/panel"))


@router.get("/login/code")
def login_code_form(request: Request):
    if not request.session.get("pending_2fa"):
        return _redirect("/login")
    return render(request, "login_code.html", {})


def _code_hash(config: Config, nonce: str, code: str) -> str:
    import hashlib
    import hmac
    return hmac.new(config.secret_key.encode(), f"{nonce}:{code}".encode(), hashlib.sha256).hexdigest()


def _clean_2fa(conn) -> None:
    import time as _time
    for row in conn.execute("SELECT key, value FROM settings WHERE key LIKE '2fa.%'").fetchall():
        try:
            if json.loads(row["value"])["exp"] < _time.time():
                conn.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
        except (ValueError, KeyError, TypeError):
            conn.execute("DELETE FROM settings WHERE key = ?", (row["key"],))


@router.post("/login/code", dependencies=[Depends(check_csrf)])
def login_code(request: Request, code: str = Form(""), conn=Depends(get_conn),
               config: Config = Depends(get_config)):
    import secrets as _secrets
    import time as _time
    nonce = request.session.get("pending_2fa")
    key = f"2fa.{nonce}" if isinstance(nonce, str) and nonce else ""
    with db.tx(conn):   # попытки считает сервер — повтор старого cookie их не сбросит
        raw = db.get_setting(conn, key) if key else None
        pending = json.loads(raw) if raw else None
        if not pending or _time.time() > pending["exp"] or pending["tries"] >= 5:
            if key:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            pending = None
        else:
            pending["tries"] += 1
            db.set_setting(conn, key, json.dumps(pending))
    if pending is None:
        request.session.pop("pending_2fa", None)
        flash(request, "Код устарел. Войдите ещё раз — пришлём новый.", "error")
        return _redirect("/login")
    if not _secrets.compare_digest(_code_hash(config, nonce, code.strip()), pending["hash"]):
        return render(request, "login_code.html", {"error": "Неверный код."}, 400)
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    request.session.clear()
    request.session["user_id"] = pending["uid"]
    _record_login(conn, request, pending["uid"])
    return _redirect("/admin")


@router.get("/auth/google")
def google_start(request: Request, config: Config = Depends(get_config)):
    from . import google_auth
    if not google_auth.enabled(config):
        flash(request, "Вход через Google не настроен.", "error")
        return _redirect("/login")
    state = uuid.uuid4().hex
    request.session["g_state"] = state
    return RedirectResponse(google_auth.auth_url(config, state), status_code=303)


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    conn=Depends(get_conn), config: Config = Depends(get_config)):
    import secrets as _secrets

    from . import google_auth, sitecfg
    expected = request.session.pop("g_state", "")
    if error or not code:
        flash(request, "Вход через Google отменён.", "error")
        return _redirect("/login")
    if not expected or not _secrets.compare_digest(state, expected):
        flash(request, "Ссылка входа устарела. Нажмите «Войти через Google» ещё раз.", "error")
        return _redirect("/login")
    try:
        profile = google_auth.fetch_profile(config, code)
        with db.tx(conn):
            user, created = google_auth.find_or_create(conn, config, profile,
                                                       allow_new=sitecfg.registration_open(conn))
    except (google_auth.GoogleError, accounts.AccountError) as exc:
        flash(request, str(exc), "error")
        return _redirect("/login")
    if user["status"] == "blocked":
        flash(request, "Аккаунт заблокирован.", "error")
        return _redirect("/login")
    if created:
        from . import referrals
        referrals.attach(conn, user["id"], request.session.pop("ref", None))
        from .tgbot import user_event
        from .worker import notify_event
        if config.require_approval:
            notify_event(conn, config, user_event(conn, user["id"]))
            flash(request, "Аккаунт создан через Google. Мы проверим заявку и активируем доступ.")
        else:
            flash(request, "Добро пожаловать! Аккаунт создан через Google.")
    notes = request.session.get("flash", [])
    resp = _finish_login(request, conn, user)
    if notes:
        request.session["flash"] = notes  # сообщения о новом аккаунте переживают очистку сессии
    return resp


def _record_login(conn, request: Request, user_id: int) -> None:
    import secrets as _secrets
    sid = _secrets.token_urlsafe(24)
    conn.execute("INSERT INTO logins (user_id, ip, user_agent, created_at, sid) VALUES (?, ?, ?, ?, ?)",
                 (user_id, _ip(request)[:64], request.headers.get("user-agent", "")[:300], db.now(), sid))
    request.session["sid"] = sid


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, conn=Depends(get_conn)):
    sid = request.session.get("sid")
    if sid:   # сессия закрыта на сервере: копия cookie после выхода уже не войдёт
        conn.execute("UPDATE logins SET ended_at = ? WHERE sid = ? AND ended_at IS NULL", (db.now(), sid))
    request.session.clear()
    return _redirect("/")


def _ip(request: Request) -> str:
    # Настоящий адрес уже подставил uvicorn (proxy_headers от nginx на 127.0.0.1).
    # Первое значение X-Forwarded-For пишет сам клиент — по нему лимит входа обходился бы.
    return request.client.host if request.client else "?"


# ── Кабинет ──────────────────────────────────────────────────


@router.get("/panel")
def panel_home(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
               config: Config = Depends(get_config)):
    summary = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_micro), 0) AS spent FROM orders "
        "WHERE user_id = ? AND status = 'completed'",
        (user["id"],),
    ).fetchone()
    key = conn.execute(
        "SELECT id, prefix, created_at, last_used_at, key_enc IS NOT NULL AS can_show FROM api_keys "
        "WHERE user_id = ? AND revoked_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (user["id"],)
    ).fetchone()
    return render(request, "panel/home.html", {
        "user": user, "summary": summary, "key": key, "kinds": KINDS,
        "orders_all": conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user["id"],)).fetchone()[0],
        "markup": accounts.markup_for(user, config),
        "ref_percent": referrals.percent(conn),
        "dc": _dcoin_card(conn, user["id"]),
    })


def _dcoin_card(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    from . import dcoin
    return dcoin.summary(conn, user_id) if dcoin.enabled(conn) else None


def _tz_hours(conn: sqlite3.Connection, user: Any, cookie: str | None) -> int:
    """Сдвиг пояса клиента в часах — для графиков «по часам»."""
    from datetime import datetime as _dt
    from datetime import timezone as _tzu

    from . import timez
    name, _ = timez.resolve(conn, user, cookie)
    offset = _dt.now(_tzu.utc).astimezone(timez.zone(name)).utcoffset()
    return int(offset.total_seconds() // 3600) if offset else 0


@router.post("/panel/timezone", dependencies=[Depends(check_csrf)])
def panel_timezone(request: Request, tz: str = Form("auto"), user=Depends(panel_user), conn=Depends(get_conn)):
    """Часовой пояс клиента: «автоматически» (по устройству) или выбранный — на всех его устройствах."""
    from . import timez
    timez.set_user_choice(conn, user["id"], tz)
    flash(request, "Часовой пояс сохранён: " + (timez.label(tz) if tz != timez.AUTO else "автоматически, по устройству")
          + ".")
    return _redirect("/panel")


@router.get("/panel/stats")
def panel_stats(request: Request, period: str = "30d", user=Depends(panel_user), conn=Depends(get_conn),
                config: Config = Depends(get_config)):
    from . import analytics
    return render(request, "panel/stats.html", {
        "user": user, "a": analytics.build(conn, period, user_id=user["id"],
                                           tz_hours=_tz_hours(conn, user, request.cookies.get("dx_tz"))),
    })


@router.get("/panel/logins")
def panel_logins(request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM logins WHERE user_id = ? ORDER BY id DESC LIMIT 30", (user["id"],)).fetchall()
    return render(request, "panel/logins.html", {"user": user, "rows": rows})


def _cents(value) -> str:
    """Цена «от» для витрины: до центов, вверх — чтобы не обещать меньше реальной."""
    from decimal import ROUND_CEILING, Decimal

    return str(value.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


# Разделы, где товары сгруппированы по играм/сервисам: сначала выбирают игру, потом пакет
_BY_GAME = ("topup", "gift_card", "game_key")


def _cached(key: str, q: str, make):
    """Каталог без поиска меняется только при загрузке — держим его минуту в памяти."""
    return make() if q else cache.get_or_set(key, 60, make)


@router.get("/panel/catalog")
def panel_catalog(
    request: Request, kind: str = "", q: str = "", category: str = "", region: str = "",
    user=Depends(viewer), conn=Depends(get_conn), config: Config = Depends(get_config),
):
    if kind == "telegram":
        return render(request, "panel/telegram.html",
                      _telegram_ctx(conn, config, user, request.query_params.get("tab", "")))
    if kind in ("telegram_stars", "telegram_premium") and not q:
        return _redirect("/panel/catalog?kind=telegram" + ("&tab=premium" if kind == "telegram_premium" else ""))
    kind = kind if kind in KINDS else ""
    q = q[:100]
    if kind in _BY_GAME and not category:
        # Цена «от» — уже с наценкой клиента, закупочную не показываем
        games = [
            {**dict(c), "regions": sorted(filter(None, (c["regions"] or "").split(","))),
             "from_price": _cents(apply_markup(to_decimal(str(c["from_price"])),
                                               accounts.markup_for(user, config, c["kind"])))}
            for c in _cached(f"cats:{kind}", q, lambda: [dict(c) for c in catalog.categories(conn, kind=kind, q=q)])
        ]
        return render(request, "panel/catalog.html", {
            "user": user, "games": games, "kind": kind, "q": q, "kinds": KINDS, "count": len(games),
            "region_title": region_title,
        })
    products = _cached(f"products:{kind}:{category}", q,
                       lambda: catalog.list_products(conn, kind=kind, q=q, category_id=category))
    regions = sorted({p["region"] for p in products if p.get("region")})
    game = products[0] if category and products else None
    if region:
        products = [p for p in products if p.get("region") == region]
    items = [catalog.public_view(p, accounts.markup_for(user, config, p["kind"])) for p in products]
    groups: dict[str, list] = {}
    if game and game["kind"] == "topup":
        # Пакеты одной игры: 💎 алмазы → 🎟 ваучеры и пропуска → ⚡ прокачка
        from . import packs
        items.sort(key=lambda i: packs.order_key(i["name"], float(i["price_usd"])))
        for item in items:
            groups.setdefault(f"{packs.GROUP_EMOJI[item['group']]} {packs.GROUP_TITLES[item['group']]}",
                              []).append(item)
    else:
        for item in items:
            groups.setdefault(f"{item['kind_title']} · {item['category_name']}", []).append(item)
    return render(request, "panel/catalog.html", {
        "user": user, "groups": groups, "kind": kind, "q": q, "kinds": KINDS, "count": len(items),
        "category": category, "game": game, "regions": regions, "region": region, "region_title": region_title,
    })


async def _form(request: Request) -> dict[str, str]:
    return {k: str(v) for k, v in (await request.form()).items()}


def _telegram_ctx(conn, config: Config, user, tab: str, **extra) -> dict:
    """Одна страница «Telegram», как у FazerCards: вкладки Звёзды / Premium, получатель, пакет."""
    def view(kind: str) -> list[dict]:
        markup = accounts.markup_for(user, config, kind)
        return [catalog.public_view(p, markup) for p in catalog.list_products(conn, kind=kind)]
    stars = next(iter(view("telegram_stars")), None)
    plans = sorted(view("telegram_premium"), key=lambda p: to_decimal(p["price_usd"]))
    for pl in plans:
        m = re.search(r"(\d+)", pl["name"])
        n = int(m.group(1)) if m else 0
        word = "месяц" if n % 10 == 1 and n % 100 != 11 else (
            "месяца" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "месяцев")
        pl["plan_title"] = f"{n} {word}" if n else pl["name"]
    tab = "premium" if tab == "premium" or (not stars and plans) else "stars"
    return {"user": user, "tab": tab, "stars": stars, "plans": plans, "idem": str(uuid.uuid4()), "form": {},
            "error": None, **extra}


@router.post("/panel/telegram", dependencies=[Depends(check_csrf)])
def panel_telegram_buy(request: Request, form: dict = Depends(_form), user=Depends(panel_user),
                       conn=Depends(get_conn), config: Config = Depends(get_config)):
    p = catalog.get_product(conn, str(form.get("product_id", "")))
    tab = "premium" if p and p["kind"] == "telegram_premium" else "stars"
    username = str(form.get("field_telegram_username", "")).strip()
    if p is None or p["kind"] not in ("telegram_stars", "telegram_premium"):
        error = "Выберите план Premium." if form.get("tab") == "premium" else "Товар недоступен."
        return render(request, "panel/telegram.html", _telegram_ctx(
            conn, config, user, str(form.get("tab", "")), error=error, form={"telegram_username": username}), 400)
    try:
        order, _ = orders.create_order(
            conn, config, request.app.state.supplier, user, product_id=p["id"],
            quantity=form.get("quantity", 1) if tab == "stars" else 1,
            fields={f["key"]: username for f in p["fields"]},
            client_idem_key="panel-" + str(form.get("idem", ""))[:64], source="panel",
        )
    except orders.OrderError as exc:
        return render(request, "panel/telegram.html", _telegram_ctx(
            conn, config, accounts.get_user(conn, user["id"]), tab, error=str(exc),
            form={"telegram_username": username, "quantity": form.get("quantity", ""), "product_id": p["id"]}), 400)
    return _redirect(f"/panel/orders/{order['public_id']}")


@router.get("/panel/buy/{product_id}")
def panel_buy_form(product_id: str, request: Request, user=Depends(viewer), conn=Depends(get_conn),
                   config: Config = Depends(get_config)):
    p = catalog.get_product(conn, product_id)
    if p is None:
        flash(request, "Товар недоступен.", "error")
        return _redirect("/panel/catalog")
    if p["kind"] in ("telegram_stars", "telegram_premium"):
        return _redirect("/panel/catalog?kind=telegram" + ("&tab=premium" if p["kind"] == "telegram_premium" else ""))
    form = {k[6:]: v[:100] for k, v in request.query_params.items() if k.startswith("field_")}
    return render(request, "panel/buy.html", _buy_ctx(request, conn, config, user, p, form=form))


def _buy_ctx(request: Request, conn, config: Config, user, p: dict, **extra) -> dict:
    """Всё для страницы покупки: товар, другие пакеты этой игры, можно ли проверить аккаунт."""
    from . import account_check
    markup = accounts.markup_for(user, config, p["kind"])
    siblings = []
    if p["kind"] in _BY_GAME:
        siblings = [catalog.public_view(s, markup) for s in
                    catalog.list_products(conn, kind=p["kind"], category_id=p["category_id"], limit=200)]
        if p["kind"] == "topup":
            from . import packs
            siblings.sort(key=lambda i: packs.order_key(i["name"], float(i["price_usd"])))
    return {
        "user": user, "p": catalog.public_view(p, markup), "siblings": siblings, "idem": str(uuid.uuid4()),
        "can_check": account_check.can_check(request.app.state.supplier, p), "form": {},
        "delivery_note": catalog.delivery_note(p.get("category_name")), **extra,
    }


@router.post("/panel/buy/{product_id}", dependencies=[Depends(check_csrf)])
def panel_buy(product_id: str, request: Request, form: dict = Depends(_form), user=Depends(panel_user),
              conn=Depends(get_conn), config: Config = Depends(get_config)):
    # Обычная (не async) функция: FastAPI выполнит её в отдельном потоке,
    # и ожидание ответа поставщика не остановит остальной сайт.
    p = catalog.get_product(conn, product_id)
    if p is None:
        flash(request, "Товар недоступен.", "error")
        return _redirect("/panel/catalog")
    fields = {f["key"]: str(form.get(f"field_{f['key']}", "")) for f in p["fields"]}
    try:
        order, _ = orders.create_order(
            conn, config, request.app.state.supplier, user,
            product_id=product_id, quantity=form.get("quantity", 1), fields=fields,
            client_idem_key="panel-" + str(form.get("idem", ""))[:64], source="panel",
        )
    except orders.OrderError as exc:
        return render(request, "panel/buy.html", _buy_ctx(
            request, conn, config, accounts.get_user(conn, user["id"]), p, error=str(exc),
            form={**fields, "quantity": form.get("quantity", "")}), 400)
    return _redirect(f"/panel/orders/{order['public_id']}")


CART_MAX = 20   # пакетов за один раз — каждый уходит поставщику отдельным заказом


@router.post("/panel/buy-many/{product_id}", dependencies=[Depends(check_csrf)])
def panel_buy_many(product_id: str, request: Request, form: dict = Depends(_form), user=Depends(panel_user),
                   conn=Depends(get_conn), config: Config = Depends(get_config)):
    """Несколько пакетов одной игры на один ID: сначала проверяем, хватит ли денег на всё,
    потом оформляем заказы по очереди — каждый отдельно уходит поставщику."""
    p = catalog.get_product(conn, product_id)
    if p is None:
        flash(request, "Товар недоступен.", "error")
        return _redirect("/panel/catalog")
    fields = {k[6:]: str(v) for k, v in form.items() if k.startswith("field_")}

    def fail(text: str):
        return render(request, "panel/buy.html", _buy_ctx(
            request, conn, config, accounts.get_user(conn, user["id"]), p, error=text, form=fields), 400)

    cart: list[tuple[dict, int]] = []
    for key, value in form.items():
        if not key.startswith("item_"):
            continue
        item = catalog.get_product(conn, key[5:])
        try:
            n = int(value)
        except ValueError:
            n = 0
        if (item is None or item["category_id"] != p["category_id"] or item["kind"] != p["kind"]
                or (item.get("region") or "") != (p.get("region") or "")):   # один ID — один регион
            return fail("Один из пакетов недоступен — уберите его из корзины.")
        if n > 0:
            cart.append((item, n))
    count = sum(n for _, n in cart)
    if not cart:
        return fail("Корзина пуста — нажмите «+» у нужных пакетов.")
    if count > CART_MAX:
        return fail(f"За один раз — не больше {CART_MAX} пакетов.")
    try:
        total = sum(orders.quote(config, user, item, 1)["total_micro"] * n for item, n in cart)
    except orders.OrderError as exc:
        return fail(str(exc))
    have = accounts.get_user(conn, user["id"])["balance_micro"]
    if have + orders.ROUNDING_MICRO < total:
        return fail(f"Не хватает ${fmt(total - have)} — пополните баланс. Нужно ${fmt(total)}, "
                    f"на балансе ${fmt(have)}.")
    idem = str(form.get("idem", ""))[:64]
    made: list[str] = []
    error = ""
    step = 0
    for item, n in cart:
        for _ in range(n):
            step += 1
            try:
                order, _ = orders.create_order(
                    conn, config, request.app.state.supplier, accounts.get_user(conn, user["id"]),
                    product_id=item["id"], quantity=1,
                    fields={f["key"]: fields.get(f["key"], "") for f in item["fields"]},
                    client_idem_key=f"panel-{idem}-{step}", source="panel")
            except orders.OrderError as exc:
                error = str(exc)
                break
            made.append(order["public_id"])
        if error:
            break
    if not made:
        return fail(error or "Заказ не оформлен.")
    if error:
        flash(request, f"Оформлено {len(made)} из {count}. Остальные не оформлены: {error}", "error")
    else:
        flash(request, f"Оформлено заказов: {len(made)}. Они выполняются по очереди — статус каждого ниже.")
    return _redirect("/panel/orders")


@router.get("/panel/data/check-account/{product_id}")
def panel_check_account(product_id: str, request: Request, user=Depends(viewer), conn=Depends(get_conn)):
    from .api import ApiError, account_check_view
    if isinstance(user, Guest):
        return JSONResponse({"ok": False, "error": "Зарегистрируйтесь, чтобы проверить аккаунт."}, 401)
    p = catalog.get_product(conn, product_id)
    if p is None:
        return JSONResponse({"ok": False, "error": "Товар не найден."}, 404)
    if request.app.state.limiter.hit("status", f"web{user['id']}") is not None:
        return JSONResponse({"ok": False, "error": "Слишком часто. Подождите минуту."}, 429)
    fields = {k[6:]: v for k, v in request.query_params.items() if k.startswith("field_")}
    try:
        return {"ok": True, **account_check_view(request.app.state.supplier, p, fields)}
    except ApiError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, exc.http_status)


@router.get("/panel/data/gamekey-regions/{product_id}")
def panel_gamekey_regions(product_id: str, request: Request, user=Depends(viewer), conn=Depends(get_conn)):
    from . import gamekeys
    p = catalog.get_product(conn, product_id)
    if p is None or p["kind"] != "game_key":
        return JSONResponse({"ok": False, "error": "Товар не найден."}, 404)
    return {"ok": True, **gamekeys.regions(request.app.state.supplier, p["supplier_ref"]["game_id"])}


@router.get("/panel/data/steam-gifts/games")
def panel_gift_games(q: str = "", user=Depends(viewer), conn=Depends(get_conn)):
    from . import steam_gifts
    return {"ok": True, "items": steam_gifts.search(conn, q[:100])}


@router.get("/panel/data/steam-gifts/games/{appid}")
def panel_gift_game(appid: int, request: Request, user=Depends(viewer), conn=Depends(get_conn),
                    config: Config = Depends(get_config)):
    from .api import ApiError, steam_gift_view
    try:
        return steam_gift_view(request, conn, config, user, appid)
    except ApiError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "code": exc.code}, status_code=exc.http_status)


@router.get("/panel/orders")
def panel_orders(request: Request, status: str = "", q: str = "", page: int = 1, period: str = "",
                 date_from: str = "", date_to: str = "", user=Depends(panel_user), conn=Depends(get_conn)):
    from urllib.parse import urlencode

    from . import periods, timez
    per = 30
    page = max(page, 1)
    tz_name, _ = timez.resolve(conn, user, request.cookies.get("dx_tz"))   # сутки — по часам клиента
    pr = periods.resolve(timez.zone(tz_name), period, date_from, date_to)
    cond, cond_args = pr.sql("created_at")
    where, args = "user_id = ? AND " + cond, [user["id"], *cond_args]
    if status == "processing":
        where += " AND status IN ('processing', 'attention')"
    elif status in ("completed", "failed"):
        where += " AND status = ?"
        args.append(status)
    if q:
        where += " AND (public_id LIKE ? OR product_name LIKE ? OR fields_json LIKE ?)"
        args += [f"%{q}%"] * 3
    totals = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(status = 'completed'), 0) AS done, "
        "COALESCE(SUM(status = 'failed'), 0) AS failed, "
        f"COALESCE(SUM(CASE WHEN status = 'completed' THEN total_micro END), 0) AS spent FROM orders WHERE {where}",
        args).fetchone()
    total = totals["n"]
    rows = conn.execute(f"SELECT * FROM orders WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        [*args, per, (page - 1) * per]).fetchall()
    rows = orders.with_images(conn, rows)   # картинка игры для карточек
    keep = urlencode({"status": status, "q": q})
    return render(request, "panel/orders.html", {
        "user": user, "orders": rows, "status": status, "q": q, "page": page,
        "pages": max(1, -(-total // per)), "total": total, "totals": totals, "pr": pr, "presets": periods.PRESETS,
        "keep": keep, "keep_all": keep + "&" + urlencode({"period": pr.key if pr.key != "custom" else "",
                                                          "date_from": pr.date_from if pr.key == "custom" else "",
                                                          "date_to": pr.date_to if pr.key == "custom" else ""}),
    })


@router.get("/panel/orders/{public_id}")
def panel_order(public_id: str, request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    row = orders.find_user_order(conn, user["id"], public_id)
    if row is None:
        flash(request, "Заказ не найден.", "error")
        return _redirect("/panel/orders")
    row = orders.refresh_if_stale(conn, request.app.state.supplier, row)
    return render(request, "panel/order.html", {
        "user": user, "o": row, "view": orders.public_view(row),
        "delivery_pretty": json.dumps(orders.public_view(row)["delivery"], ensure_ascii=False, indent=2),
        "delivery_note": catalog.delivery_note(row["product_name"]),   # срок выдачи для Standoff 2 / CoC
    })


@router.get("/panel/transactions")
def panel_transactions(request: Request, type: str = "", page: int = 1,
                       user=Depends(panel_user), conn=Depends(get_conn)):
    per = 40
    page = max(page, 1)
    where, args = "t.user_id = ?", [user["id"]]
    if type in ("credit", "debit"):
        where += " AND t.type = ?"
        args.append(type)
    total = conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT t.*, o.public_id AS order_public FROM transactions t LEFT JOIN orders o ON o.id = t.order_id "
        f"WHERE {where} ORDER BY t.id DESC LIMIT ? OFFSET ?", [*args, per, (page - 1) * per]
    ).fetchall()
    return render(request, "panel/transactions.html", {
        "user": user, "rows": rows, "type": type, "page": page, "pages": max(1, -(-total // per)),
    })


@router.get("/panel/balance")
def panel_balance(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                  config: Config = Depends(get_config)):
    from . import payments, rates
    rates.refresh(conn, config, rates.PAYMENT_SECONDS)
    rows = conn.execute("SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
    conf = payments.settings(conn, config)
    waiting = payments.open_request(conn, user["id"])
    return render(request, "panel/balance.html", {
        "user": user, "methods": payments.methods(conn, config), "payments": rows, "tjs_rate": conf["tjs_rate"],
        "min_usd": conf["min_usd"], "min_tjs": conf["min_tjs"], "waiting": waiting,
        "boost_wait": payments.boost_wait(waiting) if waiting is not None else None,
        "pay_titles": {k: v[0] for k, v in PAY_METHODS.items()} | {m["code"]: m["title"] for m in conf["all_methods"]},
    })


# ── Свой Telegram-бот (конструктор) ──────────────────────────


@router.get("/panel/bots")
def panel_bots(request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    from . import bots, sitecfg
    return render(request, "panel/bots.html", {
        "user": user, "bots": bots.listing(conn, user["id"]), "max_bots": sitecfg.max_bots(conn),
        "elig": bots.eligibility(conn, user),
        "ready": bots.runner_alive(conn) and bots.TEMPLATE_DIR.exists() and sitecfg.client_bots_enabled(conn),
    })


@router.post("/panel/bots", dependencies=[Depends(check_csrf)])
def panel_bots_add(request: Request, token: str = Form(""), admin_ids: str = Form(""),
                   user=Depends(panel_user), conn=Depends(get_conn), config: Config = Depends(get_config)):
    from . import bots
    from .worker import notify_admin
    elig = bots.eligibility(conn, user)   # 5 заказов, запрет после автоотключения, лимит
    if not elig["ok"]:
        flash(request, elig["reason"], "error")
        return _redirect("/panel/bots")
    try:
        username = bots.check_token(token)
        bots.create(conn, config, user_id=user["id"], token=token, admin_ids=admin_ids, username=username)
    except bots.BotError as exc:
        flash(request, str(exc), "error")
        return _redirect("/panel/bots")
    if bots.RUNNER:
        bots.RUNNER.poke()
    notify_admin(config, f"🤖 Клиент {user['login']} подключил бота @{username} в конструкторе.")
    flash(request, f"Бот @{username} подключён — запустится в течение минуты. Откройте его и нажмите /start, "
                   "затем /panel — там игры, цены и реквизиты.")
    return _redirect("/panel/bots")


@router.post("/panel/bots/{bot_id}/{action}", dependencies=[Depends(check_csrf)])
def panel_bots_action(bot_id: int, action: str, request: Request, admin_ids: str = Form(""),
                      user=Depends(panel_user), conn=Depends(get_conn)):
    from . import bots
    if not bots.owned(conn, bot_id, user["id"]):
        flash(request, "Бот не найден.", "error")
        return _redirect("/panel/bots")
    try:
        if action == "stop":
            bots.set_enabled(conn, bot_id, False)
        elif action in ("start", "restart"):
            enabled = conn.execute("SELECT enabled FROM bots WHERE id = ?", (bot_id,)).fetchone()["enabled"]
            ok, why = bots.can_enable(conn, bot_id)
            if not enabled and not ok:   # отключённого за отсутствие продаж включает только админ
                flash(request, why, "error")
                return _redirect("/panel/bots")
            bots.set_enabled(conn, bot_id, True)  # updated_at меняется — процесс перезапустится
        elif action == "admins":
            bots.update_admins(conn, bot_id, admin_ids)
            flash(request, "Админы бота обновлены.")
        elif action == "delete":
            bots.delete(conn, bot_id)
            flash(request, "Бот удалён, его API-ключ отозван.")
    except bots.BotError as exc:
        flash(request, str(exc), "error")
    if bots.RUNNER:
        bots.RUNNER.poke()
    return _redirect("/panel/bots")


@router.get("/pay-icons/{name}")
def pay_icon(name: str, config: Config = Depends(get_config)):
    """Иконки способов оплаты — видны клиентам и ботам, поэтому без входа."""
    from fastapi.responses import FileResponse

    from . import payments
    path = payments.icons_dir(config) / name
    if not payments.ICON_NAME_RE.fullmatch(name) or not path.is_file():
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000, immutable",
                                       "X-Content-Type-Options": "nosniff"})


@router.get("/panel/balance/{payment_id}/pay")
def panel_auto_pay(payment_id: int, request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                   config: Config = Depends(get_config)):
    """Экран автоплатежа: сумма, адрес или кнопка Binance, статус обновляется сам."""
    from . import payments
    row = conn.execute("SELECT * FROM payments WHERE id = ? AND user_id = ?", (payment_id, user["id"])).fetchone()
    if row is None or not row["auto_kind"]:
        return _redirect("/panel/balance")
    return render(request, "panel/auto_pay.html", {"user": user, "p": payments.public(conn, config, row)})


@router.get("/panel/data/payment/{payment_id}")
def panel_payment_status(payment_id: int, user=Depends(panel_user), conn=Depends(get_conn),
                         config: Config = Depends(get_config)):
    from . import cryptopay
    row = conn.execute("SELECT status, auto_kind FROM payments WHERE id = ? AND user_id = ?",
                       (payment_id, user["id"])).fetchone()
    if row is None:
        return JSONResponse({"status": "missing"}, status_code=404)
    if row["status"] == "pending" and row["auto_kind"]:
        cryptopay.check_all(conn, config)  # не чаще раза в 20 секунд на весь сайт
        row = conn.execute("SELECT status FROM payments WHERE id = ?", (payment_id,)).fetchone()
    return JSONResponse({"status": row["status"]}, headers={"Cache-Control": "no-store"})


@router.post("/pay/binance/webhook")
async def binance_webhook(request: Request, conn=Depends(get_conn), config: Config = Depends(get_config)):
    """Уведомление Binance Pay. Телу не верим: по номеру заказа сами спрашиваем статус у Binance."""
    from . import cryptopay
    try:
        body = await request.json()
        data = body.get("data")
        data = json.loads(data) if isinstance(data, str) else (data or {})
        trade_no = str(data.get("merchantTradeNo") or "")
    except (ValueError, AttributeError):
        trade_no = ""
    row = conn.execute("SELECT id FROM payments WHERE ext_id = ? AND auto_kind = 'binance'",
                       (trade_no,)).fetchone() if trade_no else None
    if row:
        cryptopay.check_binance(conn, config, only_id=row["id"])
    return JSONResponse({"returnCode": "SUCCESS", "returnMessage": None})


@router.get("/currency/{code}")
def set_currency(code: str, request: Request):
    """Переключатель USD / TJS: только показ сумм, запоминается в браузере на год."""
    code = "TJS" if code.upper() == "TJS" else "USD"
    from urllib.parse import urlsplit
    ref = urlsplit(request.headers.get("referer") or "")
    back = (ref.path or "/panel") + (f"?{ref.query}" if ref.query else "")
    if not back.startswith("/") or back.startswith("//"):
        back = "/panel"  # только свои страницы — никаких переходов на чужие сайты
    resp = _redirect(back)
    resp.set_cookie("dx_cur", code, max_age=365 * 86400, samesite="lax", httponly=True)
    return resp


@router.get("/panel/data/rate")
def panel_rate(user=Depends(panel_user), conn=Depends(get_conn), config: Config = Depends(get_config)):
    """Страница оплаты спрашивает курс каждые 30 секунд."""
    from . import payments, rates
    rates.refresh(conn, config, rates.PAYMENT_SECONDS)
    conf = payments.settings(conn, config)
    st = rates.status(conn, config)
    return JSONResponse({"tjs_rate": str(conf["tjs_rate"]), "min_usd": str(conf["min_usd"]),
                         "auto": st["auto"], "age_seconds": st["age_seconds"]})


@router.post("/panel/balance", dependencies=[Depends(check_csrf)])
def panel_balance_request(request: Request, method: str = Form(""), amount: str = Form(""),
                          amount_tjs: str = Form(""),
                          reference: str = Form(""), receipt: UploadFile | None = File(None),
                          user=Depends(panel_user), conn=Depends(get_conn),
                          config: Config = Depends(get_config)):
    from . import payments
    from .tgbot import send_receipt
    from . import rates
    data = receipt.file.read(payments.MAX_RECEIPT_BYTES + 1) if receipt and receipt.filename else b""
    if not data and not payments.is_auto(conn, config, method):
        flash(request, "Прикрепите чек об оплате — фото или PDF. Без чека заявку не проверить.", "error")
        return _redirect("/panel/balance")
    if payments.is_auto(conn, config, method):
        data = b""   # крипту проверяет блокчейн — чек не нужен и не принимаем
    rates.refresh(conn, config, rates.PAYMENT_SECONDS)  # сумма к переводу — по свежему курсу
    details = payments.settings(conn, config)["details"].get(method, "")
    seen = payments.read_receipt(config, data, details) if data else None   # ИИ — до транзакции, базу не держим
    try:
        with db.tx(conn):
            pid = payments.create(conn, config, user, method, amount, reference, amount_tjs=amount_tjs)
            if data:
                payments.attach_receipt(conn, config, user["id"], pid, data, seen=seen)
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
        return _redirect("/panel/balance")
    try:
        payments.start_auto(conn, config, pid)
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
        return _redirect("/panel/balance")
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()
    if row["auto_kind"]:
        return _redirect(f"/panel/balance/{pid}/pay")  # автоплатёж: чек и админ не нужны
    send_receipt(conn, config, pid)
    seen_now = json.loads(row["receipt_ai"]) if row["receipt_ai"] else {}
    if seen_now.get("fixed_from"):
        flash(request, f"Заявка #{pid} создана. В чеке {row['pay_amount']} {row['pay_currency']}, а в заявке было "
                       f"{seen_now['fixed_from']} — мы исправили сумму по чеку: зачислится "
                       f"${fmt(row['amount_micro'])}. "
                       "После проверки придёт уведомление.")
    else:
        flash(request, f"Заявка #{pid} создана. Переведите {row['pay_amount']} {row['pay_currency']} по реквизитам — "
                       "после проверки баланс пополнится, вам придёт уведомление.")
    return _redirect("/panel/balance")


@router.post("/panel/balance/{payment_id}/boost", dependencies=[Depends(check_csrf)])
def panel_balance_boost(payment_id: int, request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                        config: Config = Depends(get_config)):
    """«⚡ Ускорить»: заявка заново уходит админу и кассирам, старое сообщение в Telegram удаляется."""
    from . import payments
    try:
        payments.boost(conn, config, user["id"], payment_id)
        flash(request, "⚡ Напомнили администратору — заявка снова наверху списка. Обычно проверяют в течение "
                       "нескольких минут.")
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
    return _redirect("/panel/balance")


@router.post("/panel/balance/{payment_id}/cancel", dependencies=[Depends(check_csrf)])
def panel_balance_cancel(payment_id: int, request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                         config: Config = Depends(get_config)):
    from . import payments
    if payments.cancel(conn, user["id"], payment_id, config):
        flash(request, "Заявка отменена.")
    else:
        flash(request, "Чек уже отправлен — заявку проверяет администратор, отменить её нельзя.", "error")
    return _redirect("/panel/balance")


@router.get("/panel/support")
def panel_support(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                  config: Config = Depends(get_config)):
    """Поддержка в Telegram: одноразовый код — бот узнаёт клиента без email и без AI."""
    from . import supportbot
    bot = supportbot.bot_username(conn, config)
    code = supportbot.make_link_code(conn, user["id"])
    return render(request, "panel/support.html", {"user": user, "bot": bot, "code": code,
                                                   "minutes": supportbot.LINK_TTL // 60})


@router.post("/panel/support/code", dependencies=[Depends(check_csrf)])
def panel_support_code(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                       config: Config = Depends(get_config)):
    """Кнопка «Получить код для бота поддержки»: код приходит в уведомления."""
    from . import supportbot
    supportbot.send_code_notification(conn, config, user["id"])
    flash(request, "Код отправлен — он первым в списке уведомлений. Отправьте его боту поддержки.")
    return _redirect("/panel/notifications")


@router.get("/panel/dcoin")
def panel_dcoin(request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    from . import dcoin
    return render(request, "panel/dcoin.html", {
        "user": user, "d": dcoin.summary(conn, user["id"]), "history": dcoin.history(conn, user["id"], 30),
        "top": dcoin.top(conn), "tfs": list(dcoin.TIMEFRAMES), "days": dcoin.days(conn),
    })


@router.get("/panel/data/dcoin")
def panel_dcoin_data(tf: str = "1m", n: int = 80, user=Depends(panel_user), conn=Depends(get_conn)):
    """Свечи и цифры для живого графика — страница спрашивает каждые несколько секунд."""
    from . import dcoin
    data = dcoin.candles(conn, tf, n)
    d = dcoin.summary(conn, user["id"])
    # копилку и число монет не отдаём: их видит только админ
    data.update({"price": d["price"], "change": d["change"], "balance": d["balance_text"]})
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.post("/panel/dcoin/exchange", dependencies=[Depends(check_csrf)])
def panel_dcoin_exchange(request: Request, amount: str = Form(""), user=Depends(panel_user),
                         conn=Depends(get_conn)):
    from . import dcoin
    from .money import fmt
    try:
        raw = amount.replace(" ", "").replace(",", ".")
        units = dcoin.balance(conn, user["id"]) if raw.lower() in ("all", "все", "всё") \
            else int(round(float(raw) * dcoin.UNIT))
    except (ValueError, OverflowError):
        flash(request, "Укажите, сколько D-коинов обменять.", "error")
        return _redirect("/panel/dcoin")
    try:
        pay = dcoin.exchange(conn, user["id"], units)
    except dcoin.ExchangeError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, f"Обменяли {dcoin.fmt_d(units)} D — на баланс зачислено ${fmt(pay)}.")
    return _redirect("/panel/dcoin")


@router.get("/panel/referrals")
def panel_referrals(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
                    config: Config = Depends(get_config)):
    from . import referrals
    code = referrals.code_for(conn, user["id"])
    return render(request, "panel/referrals.html", {
        "user": user, "link": f"{config.base_url}/register?ref={code}", "percent": referrals.percent(conn),
        "s": referrals.stats(conn, user["id"]),
    })


@router.get("/panel/notifications")
def panel_notifications(request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    from . import notify
    rows = []
    for r in notify.latest(conn, user["id"]):
        row = dict(r)
        # Код входа в бот поддержки — крупно и с кнопкой «Копировать»
        m = re.search(r"\b(DX-[A-Z2-9]{8}|\d{6})\b", row["text"]) if row["text"].startswith("🔐") else None
        row["code"] = m.group(1) if m else ""
        rows.append(row)
    notify.mark_read(conn, user["id"])
    return render(request, "panel/notifications.html", {"user": user, "rows": rows,
                                                        "bot_username": db.get_setting(conn, "support.bot_username")})


@router.get("/panel/api")
def panel_api(request: Request, user=Depends(panel_user), conn=Depends(get_conn),
              config: Config = Depends(get_config)):
    keys = conn.execute(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY revoked_at IS NOT NULL, id DESC", (user["id"],)
    ).fetchall()
    new_key = request.session.pop("new_api_key", None)
    return render(request, "panel/api.html", {
        "user": user, "keys": keys, "new_key": new_key, "base_url": config.base_url,
    })


@router.post("/panel/api/keys", dependencies=[Depends(check_csrf)])
def panel_api_create(request: Request, name: str = Form(""), user=Depends(panel_user), conn=Depends(get_conn)):
    try:
        key = accounts.create_api_key(conn, user["id"], name, request.app.state.config.secret_key)
    except accounts.AccountError as exc:
        flash(request, str(exc), "error")
        return _redirect("/panel/api")
    request.session["new_api_key"] = key
    return _redirect("/panel/api")


@router.post("/panel/api/keys/{key_id}/reveal", dependencies=[Depends(check_csrf)])
def panel_api_reveal(key_id: int, request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    if request.app.state.limiter.hit("account", f"reveal{user['id']}") is not None:
        return JSONResponse({"ok": False, "error": "Слишком часто. Подождите минуту."}, 429)
    key = accounts.reveal_api_key(conn, user["id"], key_id, request.app.state.config.secret_key)
    if key is None:
        return JSONResponse({"ok": False, "error": "Этот ключ создан до обновления — его нельзя показать. "
                                                  "Создайте новый в «Управление ключами»."}, 404)
    return JSONResponse({"ok": True, "key": key}, headers={"Cache-Control": "no-store"})


@router.post("/panel/api/keys/{key_id}/revoke", dependencies=[Depends(check_csrf)])
def panel_api_revoke(key_id: int, request: Request, user=Depends(panel_user), conn=Depends(get_conn)):
    accounts.revoke_api_key(conn, user["id"], key_id)
    flash(request, "Ключ отозван.")
    return _redirect("/panel/api")


@router.post("/panel/api/webhook", dependencies=[Depends(check_csrf)])
def panel_webhook(request: Request, webhook_url: str = Form(""), rotate: str = Form(""),
                  user=Depends(panel_user), conn=Depends(get_conn)):
    try:
        accounts.set_webhook(conn, user["id"], webhook_url)
        if rotate:
            accounts.rotate_webhook_secret(conn, user["id"])
    except accounts.AccountError as exc:
        flash(request, str(exc), "error")
        return _redirect("/panel/api")
    flash(request, "Настройки webhook сохранены.")
    return _redirect("/panel/api")

