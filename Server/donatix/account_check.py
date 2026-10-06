"""Проверка аккаунта игрока до оплаты: поставщик (или FlashTopup) по ID возвращает ник.

Проверку умеют не все игры — список берём у поставщика и держим час в памяти.
Если поставщик недоступен, заказ не блокируем: проверка — подсказка, а не условие.

Каждый найденный ник запоминаем в базе (player_names): тот же ID второй раз не спрашиваем —
ответ мгновенный и бесплатный, даже если проверка у поставщика платная. Ник могут поменять,
поэтому старую запись (старше FRESH) показываем сразу, а в фоне тихо проверяем заново.
«Не найден» помним недолго — чаще всего это опечатка. Внешних запросов — не больше BUDGET в минуту."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .suppliers import Supplier, SupplierError, SupplierRejected

log = logging.getLogger(__name__)
_TTL = 3600
FRESH = 7 * 86400          # найденный ник считаем свежим неделю, потом — фоновая перепроверка
NOT_FOUND_TTL = 6 * 3600   # «не найден» помним 6 часов
BUDGET = 40                # внешних проверок в минуту на весь сайт (защита от перебора и лишних трат)
_calls: list[float] = []
_db_path: str | None = None
_refreshing: set[tuple] = set()
_lock = threading.Lock()
_supported: tuple[float, frozenset[str]] | None = None
_results: dict[tuple, tuple[float, dict[str, Any]]] = {}
_flash: Any = None   # FlashTopup — запасная проверка ника, когда наш поставщик игру не проверяет


def configure(config: Any) -> None:
    """Ключи FlashTopup из .env — включают поиск ника по ID для игр, которые есть у них."""
    global _flash, _db_path
    _db_path = str(getattr(config, "db_path", "") or "") or None
    if getattr(config, "flashtopup_api_id", "") and getattr(config, "flashtopup_api_key", ""):
        from .flashtopup import FlashTopup
        _flash = FlashTopup(config.flashtopup_api_id, config.flashtopup_api_key)
    else:
        _flash = None


def _flash_game(product: dict[str, Any]) -> dict[str, Any] | None:
    if _flash is None or product.get("kind") != "topup":
        return None
    from .shopbot import base_name
    return _flash.match(base_name(product.get("category_name") or ""))


def _id_fields(fields: dict[str, str]) -> tuple[str, str]:
    """Наши поля → user_id и server_id FlashTopup: ID игрока — первое, сервер/зона — по названию."""
    server = next((v for k, v in fields.items() if any(w in k.lower() for w in ("server", "zone", "сервер"))), "")
    user = next((v for k, v in fields.items() if v and v != server), "")
    return user.strip(), server.strip()


def supported(supplier: Supplier) -> frozenset[str]:
    """Категории (игры), для которых поставщик проверяет аккаунт."""
    global _supported
    with _lock:
        if _supported and time.monotonic() - _supported[0] < _TTL:
            return _supported[1]
    try:
        cats = frozenset(supplier.validate_id_categories())
    except (SupplierError, AttributeError):
        cats = frozenset()
    with _lock:
        _supported = (time.monotonic(), cats)
    return cats


def can_check(supplier: Supplier, product: dict[str, Any]) -> bool:
    if product["kind"] != "topup":
        return False
    return product["category_id"] in supported(supplier) or _flash_game(product) is not None


def _store_key(supplier: Supplier, product: dict[str, Any], fields: dict[str, str]) -> tuple[str, str, str]:
    """(игра, ID, сервер) — один и тот же игрок на сайте, в боте и в API партнёров."""
    if product["category_id"] in supported(supplier):
        game = "s:" + product["category_id"]
    else:
        flash = _flash_game(product)
        game = "f:" + (flash["validation_code"] if flash else product["category_id"])
    user, server = _id_fields(fields)
    if not user:
        user = "|".join(f"{k}={v.strip()}" for k, v in sorted(fields.items()))
    return game, user[:100], server[:50]


def _db():
    if not _db_path:
        return None
    from . import db
    try:
        return db.connect(_db_path)
    except Exception:  # noqa: BLE001
        return None


def _saved(key: tuple[str, str, str]) -> dict[str, Any] | None:
    conn = _db()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM player_names WHERE game = ? AND uid = ? AND server = ?", key).fetchone()
        if row is not None:
            conn.execute("UPDATE player_names SET hits = hits + 1 WHERE game = ? AND uid = ? AND server = ?", key)
        return dict(row) if row else None
    finally:
        conn.close()


def _save(key: tuple[str, str, str], result: dict[str, Any]) -> None:
    conn = _db()
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO player_names (game, uid, server, valid, player_name, region, strict, checked_at, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1) ON CONFLICT(game, uid, server) DO UPDATE SET valid = excluded.valid, "
            "player_name = COALESCE(excluded.player_name, player_names.player_name), region = excluded.region, "
            "strict = excluded.strict, checked_at = excluded.checked_at",
            (*key, int(bool(result["valid"])), result.get("player_name"), result.get("region"),
             int(result.get("strict", True)), time.time()))
    finally:
        conn.close()


def _from_saved(row: dict[str, Any]) -> dict[str, Any]:
    return {"valid": bool(row["valid"]), "player_name": row["player_name"], "region": row["region"],
            "message": "" if row["valid"] else "Игрок с таким ID не найден.", "strict": bool(row["strict"]),
            "cached": True}


def _budget_ok() -> bool:
    now = time.monotonic()
    with _lock:
        _calls[:] = [t for t in _calls if now - t < 60]
        if len(_calls) >= BUDGET:
            return False
        _calls.append(now)
        return True


def _ask(supplier: Supplier, product: dict[str, Any], fields: dict[str, str]) -> dict[str, Any] | None:
    """Спросить поставщика / FlashTopup. None — не ответили (ничего не запоминаем)."""
    category = product["category_id"]
    strict = True
    if category in supported(supplier):
        try:
            result = supplier.validate_account(category, {k: v.strip() for k, v in fields.items()})
        except SupplierRejected as exc:
            result = {"valid": False, "message": str(exc)}
        except SupplierError:
            return None
    else:
        game = _flash_game(product)
        user, server = _id_fields(fields)
        if game is None or not user:
            return None
        from .flashtopup import FlashError
        try:
            result = _flash.check_id(game["validation_code"], user, server)
        except FlashError as exc:
            log.warning("FlashTopup check-id: %s", exc)
            return None
        strict = False   # чужая проверка — ник показываем, но заказ из-за неё не блокируем
    return {"valid": bool(result.get("valid")), "player_name": result.get("player_name") or None,
            "region": result.get("region") or None, "message": result.get("message") or "", "strict": strict}


def _refresh_later(supplier: Supplier, product: dict[str, Any], fields: dict[str, str], key: tuple) -> None:
    """Старый ник — показали из памяти, а в фоне проверяем, не поменял ли игрок имя."""
    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            if _budget_ok():
                result = _ask(supplier, product, fields)
                if result is not None and (result["valid"] or result["player_name"]):
                    _save(key, result)
                    with _lock:
                        _results.pop(key, None)
        except Exception:  # noqa: BLE001
            log.exception("перепроверка ника")
        finally:
            with _lock:
                _refreshing.discard(key)
    threading.Thread(target=run, name="donatix-nick-refresh", daemon=True).start()


def check(supplier: Supplier, product: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    """{"valid": True/False/None, "player_name", "region", "message", "strict"}. None — проверить не удалось."""
    key = _store_key(supplier, product, fields)
    with _lock:
        hit = _results.get(key)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
    row = _saved(key)
    if row is not None:
        age = time.time() - row["checked_at"]
        if row["valid"] and age < FRESH:
            return _from_saved(row)
        if row["valid"]:                          # давно проверяли — отвечаем сразу, обновляем в фоне
            _refresh_later(supplier, product, fields, key)
            return _from_saved(row)
        if age < NOT_FOUND_TTL:
            return _from_saved(row)
    if not _budget_ok():
        return {"valid": None, "player_name": None, "region": None, "strict": True,
                "message": "Слишком много проверок — попробуйте через минуту."}
    result = _ask(supplier, product, fields)
    if result is None:
        if row is not None:                        # поставщик молчит — лучше старый ответ, чем никакого
            return _from_saved(row)
        return {"valid": None, "player_name": None, "region": None, "strict": True,
                "message": "Проверка сейчас недоступна."}
    _save(key, result)
    with _lock:
        if len(_results) > 5000:
            _results.clear()
        _results[key] = (time.monotonic(), result)
    return result


def reset() -> None:
    global _supported
    with _lock:
        _supported = None
        _results.clear()
        _calls.clear()
