"""Вход через Google (OAuth 2.0, authorization code).

Кнопка «Войти через Google» → Google → /auth/google/callback. Там меняем код
на токен и берём почту из userinfo. Если такой клиент уже есть — входит;
нет — создаём аккаунт (как при обычной регистрации, с проверкой админом).
Пароль Google-пользователю не нужен, но его можно задать позже.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import urlencode

import httpx

from . import accounts, db
from .security import hash_password
from .config import Config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleError(Exception):
    pass


def enabled(config: Config) -> bool:
    return bool(config.google_client_id and config.google_client_secret)


def redirect_uri(config: Config) -> str:
    return config.base_url + "/auth/google/callback"


def auth_url(config: Config, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": config.google_client_id, "redirect_uri": redirect_uri(config), "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account",
    })


def fetch_profile(config: Config, code: str, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Код из Google → {sub, email, name}. Только подтверждённая почта."""
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            tok = client.post(TOKEN_URL, data={
                "code": code, "client_id": config.google_client_id, "client_secret": config.google_client_secret,
                "redirect_uri": redirect_uri(config), "grant_type": "authorization_code",
            })
            if tok.status_code != 200:
                raise GoogleError("Google не подтвердил вход. Попробуйте ещё раз.")
            access = tok.json().get("access_token")
            info = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
            if info.status_code != 200:
                raise GoogleError("Не удалось получить почту из Google.")
            data = info.json()
    except httpx.HTTPError as exc:
        raise GoogleError("Google сейчас не отвечает. Попробуйте ещё раз.") from exc
    if not data.get("sub") or not data.get("email") or not data.get("email_verified"):
        raise GoogleError("У аккаунта Google нет подтверждённой почты.")
    return {"sub": str(data["sub"]), "email": str(data["email"]).lower(), "name": str(data.get("name") or "")}


def _free_login(conn: sqlite3.Connection, email: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "", email.split("@")[0])[:24] or "user"
    base = base if len(base) >= 3 else (base + "user")[:6]
    login = base
    while conn.execute("SELECT 1 FROM users WHERE lower(login) = lower(?)", (login,)).fetchone():
        login = f"{base}{secrets.randbelow(9000) + 1000}"
    return login


def find_or_create(conn: sqlite3.Connection, config: Config, profile: dict[str, Any],
                   *, allow_new: bool) -> tuple[sqlite3.Row, bool]:
    """(пользователь, создан ли сейчас)."""
    user = conn.execute("SELECT * FROM users WHERE google_sub = ?", (profile["sub"],)).fetchone()
    if user is None:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (profile["email"],)).fetchone()
        if user is not None:
            # Человек подтвердил через Google, что почта его, — привязываем и пускаем.
            # Но пароль при регистрации почту не подтверждал: аккаунт мог заранее завести чужой.
            # Поэтому его пароль и все открытые сессии гасим — войти дальше может только владелец почты.
            conn.execute("UPDATE users SET google_sub = ?, password_hash = ? WHERE id = ?",
                         (profile["sub"], hash_password(secrets.token_urlsafe(32)), user["id"]))
            conn.execute("UPDATE logins SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL",
                         (db.now(), user["id"]))
    if user is not None:
        return user, False
    if not allow_new:
        raise GoogleError("Регистрация новых партнёров временно закрыта.")
    uid = accounts.create_user(
        conn, email=profile["email"], login=_free_login(conn, profile["email"]),
        password=secrets.token_urlsafe(24),  # вход только через Google, пока клиент не задаст свой пароль
        status="pending" if config.require_approval else "active",
    )
    conn.execute("UPDATE users SET google_sub = ? WHERE id = ?", (profile["sub"], uid))
    return accounts.get_user(conn, uid), True
