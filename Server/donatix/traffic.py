"""Посещаемость сайта: кто зашёл, откуда, что смотрел и когда покупают.

Считаем сами, без Google Analytics и чужих скриптов: данные остаются у нас,
блокировщики рекламы их не режут, а сайт не грузит лишний JavaScript.

Как считается:
- просмотр — открытая страница сайта (HTML, код 200); API, картинки и админка
  не считаются, роботы поисковиков и сервисов проверки — тоже;
- посетитель — браузер: случайный id в cookie dx_vid на год (не личные данные);
- визит — заход на сайт: cookie dx_sid, обрывается после 30 минут без действий;
- источник визита — по первой странице: utm_source, иначе сайт, с которого
  пришли (Google, Telegram, Instagram…), иначе «прямой заход».

Просмотры копятся в памяти и пишутся в базу пачкой раз в пару секунд — так
ответ страницы не ждёт записи на диск.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import db

VISITOR_COOKIE, SESSION_COOKIE = "dx_vid", "dx_sid"
SESSION_SECONDS = 30 * 60
KEEP_DAYS = 400
ONLINE_MINUTES = 5

SKIP_PREFIXES = ("/static", "/media", "/api", "/admin", "/pay-icons", "/panel/data", "/auth/", "/pay/",
                 "/currency/", "/robots.txt", "/sitemap.xml", "/favicon")

_BOTS = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|preview|curl|wget|python|httpx|aiohttp|go-http|java/|"
                   r"headless|lighthouse|pingdom|uptime|monitor|scan|feed|semrush|ahrefs|mj12|dataprovider|okhttp",
                   re.I)

# Хост ссылки → понятное имя источника
_SOURCES = (
    ("google.", "Google"), ("yandex.", "Яндекс"), ("ya.ru", "Яндекс"), ("bing.", "Bing"), ("duckduckgo", "DuckDuckGo"),
    ("t.me", "Telegram"), ("telegram", "Telegram"), ("instagram", "Instagram"), ("facebook", "Facebook"),
    ("fb.", "Facebook"), ("tiktok", "TikTok"), ("youtube", "YouTube"), ("youtu.be", "YouTube"), ("vk.com", "ВКонтакте"),
    ("whatsapp", "WhatsApp"), ("wa.me", "WhatsApp"), ("twitter", "X (Twitter)"), ("x.com", "X (Twitter)"),
    ("ok.ru", "Одноклассники"), ("mail.ru", "Mail.ru"),
)
_UTM_NAMES = {"ig": "Instagram", "insta": "Instagram", "instagram": "Instagram", "tg": "Telegram",
              "telegram": "Telegram", "fb": "Facebook", "facebook": "Facebook", "tiktok": "TikTok", "tt": "TikTok",
              "google": "Google", "youtube": "YouTube", "yt": "YouTube", "vk": "ВКонтакте", "whatsapp": "WhatsApp"}

DIRECT = "Прямые заходы"

# Не считаем браузеры админов целиком — и до входа в аккаунт тоже
HUMAN = ("visitor NOT IN (SELECT visitor FROM visits WHERE user_id IN "
         "(SELECT id FROM users WHERE role = 'admin'))")

_lock = threading.Lock()
_queue: list[tuple] = []
_last_flush = 0.0
_last_purge = 0.0


# ─────────────────────────────────────────── распознавание


def is_bot(ua: str) -> bool:
    return not ua or bool(_BOTS.search(ua))


def device_of(ua: str) -> tuple[str, str, str]:
    """(устройство, браузер, система) по User-Agent — грубо, но для статистики хватает."""
    u = ua.lower()
    if "ipad" in u or "tablet" in u or ("android" in u and "mobile" not in u):
        device = "Планшет"
    elif "mobi" in u or "iphone" in u or "android" in u:
        device = "Телефон"
    else:
        device = "Компьютер"
    if "iphone" in u or "ipad" in u or "ios" in u:
        os_name = "iOS"
    elif "android" in u:
        os_name = "Android"
    elif "windows" in u:
        os_name = "Windows"
    elif "mac os" in u or "macintosh" in u:
        os_name = "macOS"
    elif "linux" in u:
        os_name = "Linux"
    else:
        os_name = "Другая"
    for key, name in (("telegram", "Telegram"), ("instagram", "Instagram"), ("fban", "Facebook"), ("fbav", "Facebook"),
                      ("yabrowser", "Яндекс Браузер"), ("edg/", "Edge"), ("opr/", "Opera"), ("opera", "Opera"),
                      ("samsungbrowser", "Samsung"), ("firefox", "Firefox"), ("fxios", "Firefox"),
                      ("crios", "Chrome"), ("chrome", "Chrome"), ("safari", "Safari")):
        if key in u:
            return device, name, os_name
    return device, "Другой", os_name


def source_of(referrer: str, own_host: str, utm_source: str = "") -> str:
    if utm_source:
        return _UTM_NAMES.get(utm_source.lower(), utm_source[:40])
    host = (urlparse(referrer).hostname or "").lower() if referrer else ""
    if not host or host == own_host or host.endswith("." + own_host):
        return DIRECT
    for key, name in _SOURCES:
        if key in host:
            return name
    return host.removeprefix("www.")[:60]


# ─────────────────────────────────────────── запись


def track(request: Any, response: Any, user_id: int | None) -> None:
    """Засчитать просмотр страницы и поставить cookie посетителя и визита."""
    path = request.url.path
    if request.method != "GET" or response.status_code != 200 or path.startswith(SKIP_PREFIXES):
        return
    if not response.headers.get("content-type", "").startswith("text/html"):
        return
    ua = request.headers.get("user-agent", "")
    if is_bot(ua):
        return
    visitor = request.cookies.get(VISITOR_COOKIE, "")
    is_new = not re.fullmatch(r"[0-9a-f]{16}", visitor or "")
    if is_new:
        visitor = secrets.token_hex(8)
    session = request.cookies.get(SESSION_COOKIE, "")
    new_session = not re.fullmatch(r"[0-9a-f]{16}", session or "")
    if new_session:
        session = secrets.token_hex(8)

    q = parse_qs(request.url.query)
    utm = {k: (q.get(k) or [""])[0][:80] for k in ("utm_source", "utm_medium", "utm_campaign")}
    referrer = request.headers.get("referer", "")[:300]
    # Источник важен только у первой страницы визита — дальше человек ходит по нашему сайту
    source = source_of(referrer, request.url.hostname or "", utm["utm_source"]) if new_session else ""
    device, browser, os_name = device_of(ua)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        _queue.append((now, visitor, session, user_id, path[:200], source, referrer if new_session else None,
                       utm["utm_source"] or None, utm["utm_medium"] or None, utm["utm_campaign"] or None,
                       device, browser, os_name, int(is_new)))

    secure = request.url.scheme == "https"
    response.set_cookie(VISITOR_COOKIE, visitor, max_age=365 * 24 * 3600, httponly=True, samesite="lax", secure=secure)
    response.set_cookie(SESSION_COOKIE, session, max_age=SESSION_SECONDS, httponly=True, samesite="lax", secure=secure)


def flush(db_path: Any, force: bool = False) -> int:
    """Записать накопленные просмотры. Раз в сутки — стереть совсем старые."""
    global _last_flush, _last_purge
    now = time.monotonic()
    if not force and now - _last_flush < 2:
        return 0
    with _lock:
        batch = _queue[:]
        _queue.clear()
    _last_flush = now
    purge = now - _last_purge > 24 * 3600
    if not batch and not purge:
        return 0
    conn = db.connect(db_path)
    try:
        if batch:
            conn.executemany(
                "INSERT INTO visits (ts, visitor, session, user_id, path, source, referrer, utm_source, utm_medium, "
                "utm_campaign, device, browser, os, is_new) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
        if purge:
            _last_purge = now
            old = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute("DELETE FROM visits WHERE ts < ?", (old,))
    finally:
        conn.close()
    return len(batch)


def reset() -> None:
    global _last_flush, _last_purge
    with _lock:
        _queue.clear()
    _last_flush = _last_purge = 0.0


# ─────────────────────────────────────────── отчёт

PERIODS = {"today": ("Сегодня", 0), "7d": ("7 дней", 7), "30d": ("30 дней", 30), "90d": ("90 дней", 90)}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _bounds(period: str, tz_hours: int, now: datetime | None = None) -> tuple[datetime, datetime, int]:
    """Начало и конец периода в UTC и число дней. «Сегодня» — с местной полуночи."""
    now = now or datetime.now(timezone.utc)
    tz = timezone(timedelta(hours=tz_hours))
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    days = PERIODS[period][1]
    start = local_midnight - timedelta(days=max(days - 1, 0))
    return start.astimezone(timezone.utc), now, max(days, 1)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _core(conn: sqlite3.Connection, since: str, until: str) -> dict[str, Any]:
    """Главные цифры за [since, until): посетители, визиты, просмотры, время, отказы."""
    human = HUMAN
    r = conn.execute(
        f"SELECT COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors, COUNT(DISTINCT session) AS sessions, "
        f"COUNT(DISTINCT CASE WHEN is_new = 1 THEN visitor END) AS new_visitors "
        f"FROM visits v WHERE ts >= ? AND ts < ? AND {human}", (since, until)).fetchone()
    s = conn.execute(
        "SELECT COUNT(*) AS n, SUM(pages = 1) AS bounces, "
        "COALESCE(AVG(dur), 0) AS avg_dur FROM ("
        "  SELECT session, COUNT(*) AS pages, "
        "  (julianday(MAX(ts)) - julianday(MIN(ts))) * 86400 AS dur "
        f"  FROM visits v WHERE ts >= ? AND ts < ? AND {human} GROUP BY session)", (since, until)).fetchone()
    sessions = s["n"] or 0
    return {
        "views": r["views"] or 0, "visitors": r["visitors"] or 0, "sessions": r["sessions"] or 0,
        "new_visitors": r["new_visitors"] or 0,
        "pages_per_session": round((r["views"] or 0) / sessions, 1) if sessions else 0,
        "avg_duration": int(s["avg_dur"] or 0),
        "bounce": round((s["bounces"] or 0) / sessions * 100) if sessions else 0,
    }


def _business(conn: sqlite3.Connection, since: str, until: str) -> dict[str, Any]:
    """Регистрации, пополнения и покупки за тот же период — чтобы видеть конверсию."""
    s, u = since, until
    regs = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'client' AND created_at >= ? AND created_at < ?",
                        (s, u)).fetchone()[0]
    topped = conn.execute("SELECT COUNT(DISTINCT user_id) FROM transactions WHERE type = 'credit' AND order_id IS NULL "
                          "AND created_at >= ? AND created_at < ?", (s, u)).fetchone()[0]
    o = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS buyers, COALESCE(SUM(total_micro), 0) AS rev "
                     "FROM orders WHERE status = 'completed' AND created_at >= ? AND created_at < ?", (s, u)).fetchone()
    return {"registrations": regs, "topped_users": topped, "orders": o["n"] or 0, "buyers": o["buyers"] or 0,
            "revenue": o["rev"] or 0}


def _delta(cur: float, prev: float) -> dict[str, Any] | None:
    if not prev:
        return None if not cur else {"pct": None, "up": True}
    pct = round((cur - prev) / prev * 100)
    return {"pct": pct, "up": pct >= 0}


def _top(conn: sqlite3.Connection, sql: str, args: tuple, limit: int = 10) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(sql + f" LIMIT {int(limit)}", args)]
    peak = max((r["n"] for r in rows), default=0)
    for r in rows:
        r["share"] = round(r["n"] / peak * 100) if peak else 0
    return rows


def _heat(conn: sqlite3.Connection, sql: str, args: tuple) -> dict[str, Any]:
    """Сетка «день недели × час»: где пик и насколько ярко красить клетку."""
    grid = [[0] * 24 for _ in range(7)]
    for r in conn.execute(sql, args):
        # SQLite: 0 = воскресенье → переводим в 0 = понедельник
        grid[(int(r["wd"]) + 6) % 7][int(r["h"])] += r["n"]
    peak = max(max(row) for row in grid)
    by_hour = [sum(grid[d][h] for d in range(7)) for h in range(24)]
    by_day = [sum(row) for row in grid]
    best = None
    if peak:
        d, h = max(((d, h) for d in range(7) for h in range(24)), key=lambda x: grid[x[0]][x[1]])
        best = {"day": WEEKDAYS_FULL[d], "hour": h, "n": grid[d][h]}
    top_hour = max(range(24), key=lambda h: by_hour[h]) if any(by_hour) else None
    top_day = max(range(7), key=lambda d: by_day[d]) if any(by_day) else None
    return {
        "cells": [[{"n": n, "lvl": _level(n, peak)} for n in row] for row in grid],
        "peak": peak, "best": best, "by_hour": by_hour, "by_day": by_day,
        "top_hour": top_hour, "top_day": WEEKDAYS_FULL[top_day] if top_day is not None else None,
        "total": sum(by_day),
    }


def _level(n: int, peak: int) -> int:
    """0…5: ступень яркости клетки. Пустая клетка — 0, самая горячая — 5."""
    if not n or not peak:
        return 0
    return max(1, min(5, round(n / peak * 5 + 0.49)))


def report(conn: sqlite3.Connection, period: str = "7d", tz_hours: int = 5,
           now: datetime | None = None) -> dict[str, Any]:
    period = period if period in PERIODS else "7d"
    start, end, days = _bounds(period, tz_hours, now)
    # +1 с: просмотр, записанный в эту же секунду, тоже входит в период
    since, until = _iso(start), _iso(end + timedelta(seconds=1))
    prev_since = _iso(start - (end - start))
    shift = f"{tz_hours:+d} hours"
    human = HUMAN

    core, prev = _core(conn, since, until), _core(conn, prev_since, since)
    # Заказы и пользователи хранят время с «Z» на конце — сравниваем в том же виде
    biz, prev_biz = _business(conn, since, until + "Z"), _business(conn, prev_since, since)

    # По дням (или по часам за сегодня): посетители, визиты, просмотры
    hourly = period == "today"
    fmt = "%Y-%m-%d %H" if hourly else "%Y-%m-%d"
    rows = {r["b"]: r for r in conn.execute(
        f"SELECT strftime('{fmt}', ts, '{shift}') AS b, COUNT(DISTINCT visitor) AS visitors, "
        f"COUNT(DISTINCT session) AS sessions, COUNT(*) AS views FROM visits "
        f"WHERE ts >= ? AND ts < ? AND {human} GROUP BY b", (since, until))}
    orders_by = {r["b"]: r for r in conn.execute(
        f"SELECT strftime('{fmt}', created_at, '{shift}') AS b, COUNT(*) AS n FROM orders "
        f"WHERE status = 'completed' AND created_at >= ? AND created_at < ? GROUP BY b", (since, until + "Z"))}
    series = []
    local = start.astimezone(timezone(timedelta(hours=tz_hours)))
    local_end = end.astimezone(timezone(timedelta(hours=tz_hours)))
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    cur = local
    while cur <= local_end and len(series) < 400:
        key = cur.strftime(fmt)
        r = rows.get(key)
        series.append({"label": cur.strftime("%H:00" if hourly else "%d.%m"),
                       "full": cur.strftime("%H:00–%H:59" if hourly else "%d.%m.%Y") + (
                           "" if hourly else f", {WEEKDAYS[cur.weekday()].lower()}"),
                       "visitors": r["visitors"] if r else 0, "sessions": r["sessions"] if r else 0,
                       "views": r["views"] if r else 0,
                       "orders": orders_by[key]["n"] if key in orders_by else 0})
        cur += step
    peak = max([s["visitors"] for s in series] + [1])
    for s in series:
        s["h"] = round(s["visitors"] / peak * 100, 1)

    # Когда заходят (начало визита) и когда покупают (выполненный заказ) — по местному времени
    visits_heat = _heat(conn, (
        f"SELECT strftime('%w', first, '{shift}') AS wd, CAST(strftime('%H', first, '{shift}') AS INTEGER) AS h, "
        f"COUNT(*) AS n FROM (SELECT MIN(ts) AS first FROM visits WHERE ts >= ? AND ts < ? AND {human} "
        f"GROUP BY session) GROUP BY wd, h"), (since, until))
    buys_heat = _heat(conn, (
        f"SELECT strftime('%w', created_at, '{shift}') AS wd, CAST(strftime('%H', created_at, '{shift}') AS INTEGER) "
        f"AS h, COUNT(*) AS n FROM orders WHERE status = 'completed' AND created_at >= ? AND created_at < ? "
        f"GROUP BY wd, h"), (since, until + "Z"))
    hour_peak_v = max(visits_heat["by_hour"] + [1])
    hour_peak_b = max(buys_heat["by_hour"] + [1])
    hours = [{"h": h, "visits": visits_heat["by_hour"][h], "buys": buys_heat["by_hour"][h],
              "vh": round(visits_heat["by_hour"][h] / hour_peak_v * 100, 1),
              "bh": round(buys_heat["by_hour"][h] / hour_peak_b * 100, 1)} for h in range(24)]

    per_session = f"FROM visits WHERE ts >= ? AND ts < ? AND {human}"
    first_hits = (f"FROM visits v WHERE v.ts >= ? AND v.ts < ? AND v.{human} AND v.id = "
                  "(SELECT MIN(id) FROM visits w WHERE w.session = v.session)")
    online = conn.execute(
        f"SELECT COUNT(DISTINCT visitor) FROM visits WHERE ts >= ? AND {human}",
        (_iso(datetime.now(timezone.utc) - timedelta(minutes=ONLINE_MINUTES)),)).fetchone()[0]

    visitors = core["visitors"]
    funnel = [
        {"t": "Посетители", "n": visitors},
        {"t": "Регистрации", "n": biz["registrations"]},
        {"t": "Пополнили баланс", "n": biz["topped_users"]},
        {"t": "Купили", "n": biz["buyers"]},
    ]
    for i, f in enumerate(funnel):
        base = funnel[i - 1]["n"] if i else 0
        f["w"] = round(f["n"] / visitors * 100, 1) if visitors else 0
        f["conv"] = round(f["n"] / base * 100, 1) if i and base else None

    return {
        "period": period, "periods": {k: v[0] for k, v in PERIODS.items()}, "tz_hours": tz_hours,
        "days": days, "online": online, "core": core, "biz": biz,
        "delta": {k: _delta(core[k], prev[k]) for k in ("visitors", "sessions", "views", "avg_duration", "bounce")}
        | {k: _delta(biz[k], prev_biz[k]) for k in ("registrations", "orders", "revenue")},
        "conversion": round(biz["registrations"] / visitors * 100, 1) if visitors else 0,
        "series": series, "hourly": hourly, "visits_heat": visits_heat, "buys_heat": buys_heat, "hours": hours,
        "weekdays": WEEKDAYS, "funnel": funnel,
        "pages": _top(conn, f"SELECT path, COUNT(*) AS n, COUNT(DISTINCT visitor) AS u {per_session} "
                            "GROUP BY path ORDER BY n DESC", (since, until)),
        "entries": _top(conn, f"SELECT v.path, COUNT(*) AS n {first_hits} GROUP BY v.path ORDER BY n DESC",
                        (since, until)),
        "sources": _top(conn, f"SELECT CASE WHEN source = '' THEN '{DIRECT}' ELSE source END AS name, COUNT(*) AS n "
                              f"{first_hits} GROUP BY name ORDER BY n DESC", (since, until)),
        "campaigns": _top(conn, f"SELECT COALESCE(utm_campaign, '—') AS name, utm_source AS src, COUNT(*) AS n "
                                f"{first_hits} AND v.utm_source IS NOT NULL GROUP BY name, src ORDER BY n DESC",
                          (since, until)),
        "devices": _top(conn, f"SELECT device AS name, COUNT(DISTINCT visitor) AS n {per_session} "
                              "GROUP BY device ORDER BY n DESC", (since, until)),
        "browsers": _top(conn, f"SELECT browser AS name, COUNT(DISTINCT visitor) AS n {per_session} "
                               "GROUP BY browser ORDER BY n DESC", (since, until), 8),
        "systems": _top(conn, f"SELECT os AS name, COUNT(DISTINCT visitor) AS n {per_session} "
                              "GROUP BY os ORDER BY n DESC", (since, until), 8),
    }


def visitors_between(conn: sqlite3.Connection, since: str, until: str) -> int:
    """Уникальные посетители за промежуток — для утреннего отчёта."""
    return conn.execute(
        f"SELECT COUNT(DISTINCT visitor) FROM visits WHERE ts >= ? AND ts < ? AND {HUMAN}",
        (since, until)).fetchone()[0]


def page_title(path: str) -> str:
    return {"/": "Главная", "/register": "Регистрация", "/login": "Вход", "/docs": "Документация API",
            "/terms": "Соглашение", "/privacy": "Конфиденциальность", "/panel": "Кабинет",
            "/panel/balance": "Пополнение баланса", "/panel/orders": "Заказы", "/panel/bots": "Мой Telegram-бот",
            "/panel/catalog": "Каталог", "/panel/telegram": "Telegram"}.get(path, path)


def duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} с"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} мин {s:02d} с"
    h, m = divmod(m, 60)
    return f"{h} ч {m:02d} мин"
