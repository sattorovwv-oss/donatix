"""Автоплатёж криптой: баланс зачисляется сам, без чека и без админа.

TRC20 (свой кошелёк). Клиенту показываем ваш адрес и уникальную сумму —
например 50.0037 USDT: по последним цифрам узнаём, чей это перевод. Раз в
минуту смотрим входящие USDT на адрес через TronGrid; пришла ровно такая
сумма после создания заявки — зачисляем. Один перевод засчитывается один раз.

Binance Pay (мерчант). Создаём заказ в Binance, клиент платит по ссылке.
Статус узнаём сами запросом к Binance (подписанным вашим секретом) — поэтому
подделать «оплачено» извне нельзя, даже присылая фальшивые уведомления.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_UP, Decimal
from typing import Any

import httpx

from . import db
from .config import Config

log = logging.getLogger(__name__)

USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # контракт USDT в сети Tron
TRONGRID = "https://api.trongrid.io"
BINANCE = "https://bpay.binanceapi.com"
TRON_ADDRESS_RE = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
AUTO_KINDS = {"trc20": "Автозачисление: USDT TRC20 (блокчейн)", "binance": "Автозачисление: Binance Pay",
              "bybit": "Автозачисление: Bybit (перевод по UID)"}
BYBIT = "https://api.bybit.com"
UNIQUE_KINDS = ("trc20", "bybit")   # у этих перевод узнаём по «хвосту» суммы
WINDOW_HOURS = 48          # сколько ждём перевод по заявке

_lock = threading.Lock()
_last_check = 0.0


class CryptoPayError(RuntimeError):
    pass


# ── TRC20 ────────────────────────────────────────────────────


def tron_address(details: str) -> str:
    found = TRON_ADDRESS_RE.search(details or "")
    return found.group(0) if found else ""


def unique_amount(conn: sqlite3.Connection, usd: Decimal) -> Decimal:
    """Сумма к переводу с «хвостом» 0.0001–0.0099, которого нет у других ждущих заявок."""
    base = usd.quantize(Decimal("0.01"), rounding=ROUND_UP)
    taken = {r[0] for r in conn.execute(
        "SELECT pay_amount FROM payments WHERE auto_kind IN ('trc20', 'bybit') AND status = 'pending'")}
    for _ in range(200):
        amount = base + Decimal(secrets.randbelow(99) + 1) / 10000
        if str(amount) not in taken:
            return amount
    raise CryptoPayError("Слишком много заявок одновременно — попробуйте через минуту.")


def _get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    r = httpx.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def incoming_usdt(config: Config, address: str, since_ms: int) -> list[dict[str, Any]]:
    """Входящие USDT на адрес: [{tx, amount(Decimal), ts(ms)}]."""
    headers = {"Accept": "application/json"}
    if config.trongrid_key:
        headers["TRON-PRO-API-KEY"] = config.trongrid_key
    data = _get_json(f"{TRONGRID}/v1/accounts/{address}/transactions/trc20",
                     {"only_to": "true", "only_confirmed": "true", "limit": 200, "contract_address": USDT_TRC20,
                      "min_timestamp": since_ms}, headers)
    out = []
    for t in data.get("data") or []:
        token = t.get("token_info") or {}
        if token.get("address") and token["address"] != USDT_TRC20:
            continue
        if str(t.get("to", "")) != address:
            continue
        decimals = int(token.get("decimals") or 6)
        out.append({"tx": str(t.get("transaction_id")), "ts": int(t.get("block_timestamp") or 0),
                    "amount": Decimal(str(t.get("value") or "0")) / (Decimal(10) ** decimals)})
    return out


def _ms(stamp: str) -> int:
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def check_trc20(conn: sqlite3.Connection, config: Config) -> int:
    """Сверить ждущие TRC20-заявки с блокчейном. Возвращает, сколько зачислено."""
    from . import payments
    rows = conn.execute(
        "SELECT * FROM payments WHERE auto_kind = 'trc20' AND status = 'pending' ORDER BY id").fetchall()
    fresh = [r for r in rows if time.time() * 1000 - _ms(r["created_at"]) < WINDOW_HOURS * 3600 * 1000]
    done = 0
    by_address: dict[str, list[sqlite3.Row]] = {}
    for r in fresh:
        if r["pay_address"]:
            by_address.setdefault(r["pay_address"], []).append(r)
    for address, items in by_address.items():
        since = min(_ms(r["created_at"]) for r in items) - 60_000
        try:
            txs = incoming_usdt(config, address, since)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("trc20: не удалось проверить %s: %s", address, exc)
            continue
        for r in items:
            want = Decimal(r["pay_amount"])
            for t in txs:
                if t["amount"] != want or t["ts"] < _ms(r["created_at"]) - 60_000:
                    continue
                if conn.execute("SELECT 1 FROM payments WHERE ext_id = ?", (t["tx"],)).fetchone():
                    continue  # этот перевод уже засчитан другой заявке
                conn.execute("UPDATE payments SET ext_id = ?, reference = ? WHERE id = ? AND status = 'pending'",
                             (t["tx"], t["tx"][:200], r["id"]))
                if payments.confirm(conn, config, r["id"], _admin_id(conn), who="автоматически"):
                    done += 1
                    _tell_admin(conn, config, r["id"], "USDT TRC20")
                    log.info("trc20: заявка #%s оплачена, tx %s", r["id"], t["tx"])
                break
    return done


# ── Bybit (свой аккаунт, ключ «только чтение») ───────────────


def bybit_ready(config: Config) -> bool:
    return bool(config.bybit_key and config.bybit_secret)


def _bybit_get(config: Config, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Подписанный GET к Bybit v5: HMAC-SHA256(timestamp + key + recv_window + query)."""
    from urllib.parse import urlencode
    query = urlencode(params)
    ts, window = str(int(time.time() * 1000)), "10000"
    sign = hmac.new(config.bybit_secret.encode(), f"{ts}{config.bybit_key}{window}{query}".encode(),
                    hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": config.bybit_key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": window,
               "X-BAPI-SIGN": sign}
    try:
        r = httpx.get(f"{BYBIT}{path}?{query}", headers=headers, timeout=15)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CryptoPayError(f"Bybit не отвечает: {exc}") from exc
    if data.get("retCode") != 0:
        raise CryptoPayError(f"Bybit: {data.get('retMsg') or data}")
    return data.get("result") or {}


def bybit_incoming(config: Config, since_ms: int) -> list[dict[str, Any]]:
    """Зачисленные USDT на аккаунт Bybit: внутренние переводы (по UID) и пополнения из блокчейна."""
    end = int(time.time() * 1000)
    params = {"coin": "USDT", "startTime": max(since_ms, end - 7 * 86400_000), "endTime": end, "limit": 50}
    out = []
    for r in _bybit_get(config, "/v5/asset/deposit/query-internal-record", params).get("rows") or []:
        if str(r.get("status")) == "2":  # 2 — успешно
            out.append({"tx": f"bybit-int-{r.get('id') or r.get('txID')}", "amount": Decimal(str(r.get("amount"))),
                        "ts": int(r.get("createdTime") or 0)})
    for r in _bybit_get(config, "/v5/asset/deposit/query-record", params).get("rows") or []:
        if str(r.get("depositStatus", r.get("status"))) == "3":  # 3 — зачислено
            out.append({"tx": f"bybit-{r.get('txID') or r.get('id')}", "amount": Decimal(str(r.get("amount"))),
                        "ts": int(r.get("successAt") or r.get("createdTime") or 0)})
    return out


def check_bybit(conn: sqlite3.Connection, config: Config) -> int:
    from . import payments
    if not bybit_ready(config):
        return 0
    rows = [r for r in conn.execute("SELECT * FROM payments WHERE auto_kind = 'bybit' AND status = 'pending' "
                                    "ORDER BY id").fetchall()
            if time.time() * 1000 - _ms(r["created_at"]) < WINDOW_HOURS * 3600 * 1000]
    if not rows:
        return 0
    try:
        txs = bybit_incoming(config, min(_ms(r["created_at"]) for r in rows) - 60_000)
    except CryptoPayError as exc:
        log.warning("bybit: %s", exc)
        return 0
    done = 0
    for r in rows:
        want = Decimal(r["pay_amount"])
        for t in txs:
            if t["amount"] != want or (t["ts"] and t["ts"] < _ms(r["created_at"]) - 60_000):
                continue
            if conn.execute("SELECT 1 FROM payments WHERE ext_id = ?", (t["tx"],)).fetchone():
                continue
            conn.execute("UPDATE payments SET ext_id = ?, reference = ? WHERE id = ? AND status = 'pending'",
                         (t["tx"], t["tx"][:200], r["id"]))
            if payments.confirm(conn, config, r["id"], _admin_id(conn), who="автоматически"):
                done += 1
                _tell_admin(conn, config, r["id"], "Bybit")
                log.info("bybit: заявка #%s оплачена (%s)", r["id"], t["tx"])
            break
    return done


# ── Binance Pay ──────────────────────────────────────────────


def binance_ready(config: Config) -> bool:
    return bool(config.binance_pay_key and config.binance_pay_secret)


def _binance_post(config: Config, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Подписанный запрос к Binance Pay: HMAC-SHA512(timestamp\\nnonce\\nbody\\n)."""
    payload = json.dumps(body, separators=(",", ":"))
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    sign = hmac.new(config.binance_pay_secret.encode(), f"{ts}\n{nonce}\n{payload}\n".encode(),
                    hashlib.sha512).hexdigest().upper()
    headers = {"Content-Type": "application/json", "BinancePay-Timestamp": ts, "BinancePay-Nonce": nonce,
               "BinancePay-Certificate-SN": config.binance_pay_key, "BinancePay-Signature": sign}
    try:
        r = httpx.post(BINANCE + path, content=payload, headers=headers, timeout=20)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CryptoPayError(f"Binance Pay не отвечает: {exc}") from exc
    if data.get("status") != "SUCCESS":
        raise CryptoPayError(f"Binance Pay: {data.get('errorMessage') or data.get('code') or data}")
    return data.get("data") or {}


def binance_create(config: Config, payment_id: int, amount_usdt: str) -> dict[str, str]:
    trade_no = f"DX{payment_id}T{secrets.token_hex(6)}"
    data = _binance_post(config, "/binancepay/openapi/v2/order", {
        "env": {"terminalType": "WEB"},
        "merchantTradeNo": trade_no,
        "orderAmount": float(Decimal(amount_usdt)),
        "currency": "USDT",
        "goods": {"goodsType": "02", "goodsCategory": "Z000", "referenceGoodsId": f"topup-{payment_id}",
                  "goodsName": f"Donatix balance #{payment_id}"},
        "returnUrl": f"{config.base_url}/panel/balance",
        "webhookUrl": f"{config.base_url}/pay/binance/webhook",
    })
    url = data.get("checkoutUrl") or data.get("universalUrl") or ""
    if not url:
        raise CryptoPayError("Binance Pay не вернул ссылку на оплату.")
    return {"trade_no": trade_no, "url": url}


def binance_status(config: Config, trade_no: str) -> str:
    return str(_binance_post(config, "/binancepay/openapi/v2/order/query",
                             {"merchantTradeNo": trade_no}).get("status") or "")


def check_binance(conn: sqlite3.Connection, config: Config, only_id: int | None = None) -> int:
    from . import payments
    if not binance_ready(config):
        return 0
    sql = "SELECT * FROM payments WHERE auto_kind = 'binance' AND status = 'pending' AND ext_id IS NOT NULL"
    args: tuple = ()
    if only_id is not None:
        sql, args = sql + " AND id = ?", (only_id,)
    done = 0
    for r in conn.execute(sql, args).fetchall():
        try:
            status = binance_status(config, r["ext_id"])
        except CryptoPayError as exc:
            log.warning("binance: заявка #%s: %s", r["id"], exc)
            continue
        if status == "PAID":
            if payments.confirm(conn, config, r["id"], _admin_id(conn), who="автоматически"):
                done += 1
                _tell_admin(conn, config, r["id"], "Binance Pay")
        elif status in ("EXPIRED", "CANCELED", "CANCELLED", "ERROR"):
            if conn.execute("UPDATE payments SET status = 'cancelled', admin_note = ?, resolved_at = ? "
                            "WHERE id = ? AND status = 'pending'",
                            (f"Binance Pay: {status}", db.now(), r["id"])).rowcount:
                from .notify import notify
                notify(conn, config, r["user_id"], f"Заявка #{r['id']} отменена: ссылка Binance Pay истекла "
                                                   "или оплата отменена. Создайте новую.", "/panel/balance")
    return done


# ── Общее ────────────────────────────────────────────────────


def start(conn: sqlite3.Connection, config: Config, payment_id: int, method: dict[str, Any]) -> None:
    """Сразу после создания заявки: запомнить адрес (TRC20) или создать заказ (Binance Pay)."""
    kind = method.get("auto") or ""
    p = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if kind == "trc20":
        address = tron_address(method.get("details", ""))
        conn.execute("UPDATE payments SET auto_kind = 'trc20', pay_address = ? WHERE id = ?", (address, payment_id))
    elif kind == "bybit":
        uid = re.search(r"\d{5,12}", method.get("details", ""))
        conn.execute("UPDATE payments SET auto_kind = 'bybit', pay_address = ? WHERE id = ?",
                     (uid.group(0) if uid else method.get("details", "")[:60], payment_id))
    elif kind == "binance":
        order = binance_create(config, payment_id, p["pay_amount"])
        conn.execute("UPDATE payments SET auto_kind = 'binance', ext_id = ?, pay_url = ? WHERE id = ?",
                     (order["trade_no"], order["url"], payment_id))


def check_all(conn: sqlite3.Connection, config: Config, min_interval: float = 20) -> int:
    """Проверить все ждущие автоплатежи (воркер — раз в минуту, страница оплаты — не чаще раза в 20 с)."""
    global _last_check
    if time.monotonic() - _last_check < min_interval or not _lock.acquire(blocking=False):
        return 0
    try:
        _last_check = time.monotonic()
        return check_trc20(conn, config) + check_bybit(conn, config) + check_binance(conn, config)
    finally:
        _lock.release()


def reset() -> None:
    global _last_check
    _last_check = 0.0


def _tell_admin(conn: sqlite3.Connection, config: Config, payment_id: int, how: str) -> None:
    from .money import fmt
    from .worker import notify_admin
    p = conn.execute("SELECT p.amount_micro, u.login FROM payments p JOIN users u ON u.id = p.user_id "
                     "WHERE p.id = ?", (payment_id,)).fetchone()
    if p:
        notify_admin(config, f"⚡️ Автоплатёж {how}: заявка #{payment_id}, клиент {p['login']}, "
                             f"зачислено ${fmt(p['amount_micro'])}.")


def _admin_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else 0
