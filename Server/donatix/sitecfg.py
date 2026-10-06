"""Настройки сайта, которые админ меняет сам — в веб-админке или в админ-боте.

Хранятся в базе и накладываются поверх .env на общий объект Config: наценки,
контакт поддержки, приём новых клиентов, конструктор ботов для клиентов.
"""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from . import cache, db
from .config import TIERS, Config

KIND_KEYS = ("telegram_stars", "telegram_premium", "steam_topup", "steam_gift")
KIND_TITLES = {"telegram_stars": "⭐ Telegram Stars", "telegram_premium": "👑 Telegram Premium",
               "steam_topup": "Пополнение Steam", "steam_gift": "Steam Гифты"}
TIER_TITLES = {"bronze": "Bronze (по умолчанию)", "silver": "Silver", "gold": "Gold"}


class SettingsError(ValueError):
    pass


def _flag(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    value = db.get_setting(conn, key)
    return default if value in (None, "") else value == "1"


def registration_open(conn: sqlite3.Connection) -> bool:
    return _flag(conn, "site.reg_open", True)


def client_bots_enabled(conn: sqlite3.Connection) -> bool:
    return _flag(conn, "site.client_bots", True)


def max_bots(conn: sqlite3.Connection) -> int:
    try:
        return max(0, int(db.get_setting(conn, "site.max_bots") or 3))
    except ValueError:
        return 3


def admin_2fa_active(conn: sqlite3.Connection, config: Config) -> bool:
    """Код из Telegram при входе в админку: включён в настройках и админ-бот подключён."""
    on = (db.get_setting(conn, "site.admin_2fa") or "1") == "1"
    return on and bool(config.alert_telegram_token and config.alert_telegram_chat_id)


def _min_orders(conn: sqlite3.Connection) -> int:
    from .bots import min_orders
    return min_orders(conn)


def max_bots_total(conn: sqlite3.Connection) -> int:
    """Сколько всего ботов может работать на сервере (0 — без предела)."""
    try:
        return max(0, int(db.get_setting(conn, "site.max_bots_total") or 0))
    except ValueError:
        return 0


def load(conn: sqlite3.Connection, config: Config) -> None:
    """Наложить сохранённое на config (при старте и после каждого изменения)."""
    if db.get_setting(conn, "migr.min_tjs_100") is None:
        # Минимум пополнения стал 100 сомони. Прежний 500 по умолчанию меняем один раз;
        # другой, заданный админом вручную, не трогаем.
        if db.get_setting(conn, "pay.min_tjs") in ("500", "500.00"):
            db.set_setting(conn, "pay.min_tjs", "100")
        db.set_setting(conn, "migr.min_tjs_100", "1")
    if db.get_setting(conn, "migr.min_tjs_0") is None:
        # Минимум пополнения убран: любая сумма. Один раз ставим 0 (в админке можно вернуть)
        db.set_setting(conn, "pay.min_tjs", "0")
        db.set_setting(conn, "migr.min_tjs_0", "1")
    for tier in TIERS:
        value = db.get_setting(conn, f"markup.{tier}")
        if value:
            config.markups[tier] = Decimal(value)
    for kind in KIND_KEYS:
        value = db.get_setting(conn, f"markup.{kind}")
        if value == "-":
            config.kind_markups.pop(kind, None)
        elif value:
            config.kind_markups[kind] = Decimal(value)
    support = db.get_setting(conn, "site.support")
    if support is not None:
        config.support_contact = support
    channel = db.get_setting(conn, "site.tg_channel")
    if channel is not None:
        config.tg_channel = channel
    rate = db.get_setting(conn, "supplier.rate_per_min")
    from .throttle import SUPPLIER
    SUPPLIER.configure(int(rate) if rate and rate.isdigit() else config.supplier_rate_per_min)
    if db.get_setting(conn, "migr.auto_approve") is None:
        # Одобрение новых клиентов больше не нужно: включаем автоматический доступ один раз
        # и активируем тех, кто уже ждал (заблокированных не трогаем). Админ может снова
        # включить проверку в настройках — это решение сохранится.
        db.set_setting(conn, "site.require_approval", "0")
        conn.execute("UPDATE users SET status = 'active' WHERE status = 'pending' AND role = 'client'")
        db.set_setting(conn, "migr.auto_approve", "1")
    approval = db.get_setting(conn, "site.require_approval")
    if approval in ("0", "1"):
        config.require_approval = approval == "1"


def _referral_example(conn: sqlite3.Connection, config: Config) -> str:
    """Пример на текущей наценке: сколько с заказа $10 получит друг и сколько останется вам."""
    from decimal import Decimal
    pct = Decimal(_referral_percent(conn))
    markup = Decimal(str(config.kind_markups.get("topup", config.markups["bronze"])))
    profit = Decimal("10") * markup / 100
    bonus = profit * pct / 100
    return (f"Пример при наценке {markup.normalize()}%: заказ с закупкой $10 → ваша прибыль ${profit:.2f}, "
            f"другу ${bonus:.2f}, вам остаётся ${profit - bonus:.2f}. В минус уйти нельзя: бонус — доля от прибыли.")


def _referral_percent(conn: sqlite3.Connection) -> int:
    from .referrals import percent
    return percent(conn)


def _personal(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Клиенты с личной наценкой: для них общая наценка уровня не действует."""
    return conn.execute("SELECT login, markup_override FROM users WHERE role = 'client' "
                        "AND markup_override IS NOT NULL AND markup_override != '' ORDER BY login").fetchall()


def view(conn: sqlite3.Connection, config: Config) -> dict[str, Any]:
    from decimal import Decimal

    from .money import apply_markup
    personal = _personal(conn)
    return {
        "personal": [{"login": r["login"], "markup": r["markup_override"]} for r in personal],
        "example_price": f"{apply_markup(Decimal('10'), config.markups.get('bronze', Decimal('0'))):.2f}",
        "markups": {t: config.markups.get(t) for t in TIERS},
        "kind_markups": {k: config.kind_markups.get(k) for k in KIND_KEYS},
        "support": config.support_contact,
        "tg_channel": config.tg_channel,
        "require_approval": config.require_approval,
        "reg_open": registration_open(conn),
        "client_bots": client_bots_enabled(conn),
        "max_bots": max_bots(conn),
        "max_bots_total": max_bots_total(conn),
        "min_orders": _min_orders(conn),
        "watch": _watch_rules(conn),
        "admin_2fa": (db.get_setting(conn, "site.admin_2fa") or "1") == "1",
        "daily_report": (db.get_setting(conn, "report.daily_on") or "1") == "1",
        "referral_percent": _referral_percent(conn),
        "referral_example": _referral_example(conn, config),
        **_dcoin_view(conn),
        "supplier_rate": _throttle_status(),
    }


def _dcoin_view(conn: sqlite3.Connection) -> dict[str, Any]:
    from . import dcoin
    st = dcoin.state(conn)
    return {"dcoin_per_usd": dcoin.per_usd(conn), "dcoin_pool_pct": dcoin.pool_pct(conn),
            "dcoin_open_days": dcoin.open_days(conn), "dcoin_refund_pct": dcoin.refund_pct(conn),
            "dcoin_pool": st["pool"], "dcoin_supply": dcoin.fmt_d(st["supply"])}


def _watch_rules(conn: sqlite3.Connection) -> dict:
    from .bot_watch import rules
    return rules(conn)


def _throttle_status() -> dict[str, int]:
    from .throttle import SUPPLIER
    return SUPPLIER.status()


def _pct(raw: str, what: str, allow_empty: bool = False) -> str:
    raw = (raw or "").replace(",", ".").replace("%", "").strip()
    if not raw and allow_empty:
        return "-"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise SettingsError(f"{what}: нужно число, например 8.") from None
    if not Decimal("0") <= value <= Decimal("100"):
        raise SettingsError(f"{what}: от 0 до 100 %.")
    return str(int(value)) if value == value.to_integral() else str(value)


def clean_channel(raw: str) -> str:
    """Ссылка на канал: https://t.me/…, @name или t.me/… → https://t.me/…; пусто — канал не показываем."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = "https://t.me/" + raw[1:]
    raw = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "https://t.me/", raw)
    if not re.fullmatch(r"https://t\.me/[+A-Za-z0-9_/-]{3,120}", raw):
        raise SettingsError("Канал: ссылка вида https://t.me/… или @имя_канала.")
    return raw


def save(conn: sqlite3.Connection, config: Config, data: dict[str, Any]) -> None:
    values: dict[str, str] = {}
    for tier in TIERS:
        if f"markup_{tier}" in data:
            values[f"markup.{tier}"] = _pct(data[f"markup_{tier}"], f"Наценка {tier}")
    for kind in KIND_KEYS:
        if f"markup_{kind}" in data:
            values[f"markup.{kind}"] = _pct(data[f"markup_{kind}"], KIND_TITLES[kind], allow_empty=True)
    if "support" in data:
        values["site.support"] = str(data["support"]).strip()[:100]
    if "tg_channel" in data:
        values["site.tg_channel"] = clean_channel(str(data["tg_channel"]))
    for key in ("reg_open", "client_bots", "require_approval", "admin_2fa"):
        if key in data:
            values[f"site.{key}"] = "1" if data[key] in (True, "1", "on") else "0"
    if "supplier_rate" in data:
        try:
            rate = int(str(data["supplier_rate"]).strip())
        except ValueError:
            raise SettingsError("Запросов в минуту — целое число.") from None
        if not 5 <= rate <= 600:
            raise SettingsError("Запросов к поставщику в минуту — от 5 до 600.")
        values["supplier.rate_per_min"] = str(rate)
    for key, lo, hi, what in (("max_bots_total", 0, 1000, "Всего ботов на сервере"),
                              ("min_orders", 0, 1000, "Заказов до своего бота"),
                              ("inactive_days", 1, 365, "Дней без продаж"),
                              ("warn_every_days", 1, 60, "Дней между предупреждениями"),
                              ("warnings", 1, 10, "Предупреждений")):
        if key in data:
            try:
                n = int(str(data[key]).strip())
            except ValueError:
                raise SettingsError(f"{what} — целое число.") from None
            if not lo <= n <= hi:
                raise SettingsError(f"{what} — от {lo} до {hi}.")
            values[("site." if key == "max_bots_total" else "bots.") + key] = str(n)
    if "referral_percent" in data:
        try:
            n = int(str(data["referral_percent"]).strip())
        except ValueError:
            raise SettingsError("Реферальный процент — целое число.") from None
        if not 0 <= n <= 50:
            raise SettingsError("Реферальный процент — от 0 до 50.")
        values["referral.percent"] = str(n)
    for key, what, lo, hi in (("dcoin_per_usd", "D-коинов за $1", 0, 10_000),
                              ("dcoin_pool_pct", "Процент прибыли в копилку D-коина", 0, 25),
                              ("dcoin_open_days", "Дней до открытия обмена D-коинов", 0, 365),
                              ("dcoin_refund_pct", "Падение цены D-коина при возврате, %", 0, 10)):
        if key in data:
            try:
                n = int(str(data[key]).strip())
            except ValueError:
                raise SettingsError(f"{what} — целое число.") from None
            if not lo <= n <= hi:
                raise SettingsError(f"{what} — от {lo} до {hi}.")
            values["dcoin." + key.removeprefix("dcoin_")] = str(n)
    if "daily_report" in data:
        values["report.daily_on"] = "1" if data["daily_report"] in (True, "1", "on") else "0"
    if "watch_on" in data:
        values["bots.watch_on"] = "1" if data["watch_on"] in (True, "1", "on") else "0"
    if "max_bots" in data:
        try:
            n = int(str(data["max_bots"]).strip())
        except ValueError:
            raise SettingsError("Лимит ботов — целое число.") from None
        if not 0 <= n <= 50:
            raise SettingsError("Лимит ботов — от 0 до 50.")
        values["site.max_bots"] = str(n)
    if str(data.get("same_for_all", "")) == "1" and "markup.bronze" in values:
        for tier in TIERS:                       # одна наценка для всех уровней — как у Bronze
            values[f"markup.{tier}"] = values["markup.bronze"]
    with db.tx(conn):
        for key, value in values.items():
            db.set_setting(conn, key, value)
        if str(data.get("reset_personal", "")) == "1":   # личные наценки больше не перебивают общую
            conn.execute("UPDATE users SET markup_override = NULL WHERE role = 'client'")
    load(conn, config)
    cache.clear_everywhere(conn)


def toggle(conn: sqlite3.Connection, config: Config, key: str) -> bool:
    current = view(conn, config)[key]
    save(conn, config, {key: "0" if current else "1"})
    return not current


def bump_markup(conn: sqlite3.Connection, config: Config, tier: str, delta: Decimal) -> Decimal:
    value = max(Decimal("0"), min(Decimal("100"), config.markups[tier] + delta))
    save(conn, config, {f"markup_{tier}": str(value)})
    return config.markups[tier]


_refreshed = {"at": 0.0}
REFRESH_EVERY = 3.0


def refresh(conn: sqlite3.Connection, config: Config) -> None:
    """Раз в несколько секунд перечитать настройки и отметку кеша: сайт работает в нескольких
    процессах, и правка в админке (наценка, курс, флаги) должна дойти до всех."""
    import time
    now = time.monotonic()
    if now - _refreshed["at"] < REFRESH_EVERY:
        return
    _refreshed["at"] = now
    load(conn, config)
    cache.sync_epoch(conn)
