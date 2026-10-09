"""Explicit account erasure; queue and file jobs live outside the site schema.

Settled accounting amounts and anti-duplicate payment fingerprints are retained
under an irreversibly anonymized, inaccessible identity. No user's funds are
discarded, no automatic refunds are invented, and other customers are untouched.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import importlib.util
import logging
import os
import secrets
import shutil
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from html import escape
from functools import wraps
from pathlib import Path
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from donatix import accounts, db, security
from donatix.api import ApiError, _limit
from donatix.deps import check_csrf, csrf_token, get_conn, session_user
from donatix.money import fmt

ERASED = "Профиль, email, телефон, привязка Google/Telegram, пароли, сессии, гостевые ключи, ключи API, токены ботов, уведомления, ID игроков, данные покупок, переписка поддержки, её вложения и файлы чеков."
RETAINED = "Обезличенные суммы и статусы завершённых расчётов, платёжные идентификаторы и отпечатки чеков сохраняются для учёта и защиты от повторного зачисления. Восстановить аккаунт по ним нельзя."
PUBLIC_PATH = "/android/account-deletion"
PANEL_PATH = "/panel/android-account-deletion"
LOG = logging.getLogger("donatix.android.erasure")
_MANAGERS = WeakValueDictionary()


def _deleted(user):
    return bool(user and user["password_hash"] == "deleted" and
                str(user["email"]).endswith("@deleted.invalid"))


def _ticket_guards(manager):
    """Stop late support replies from recreating an erased account's data.

    Only the addon's running apps participate. Other accounts use the original
    ticket implementations. Per-account locks serialize ticket writes with
    erasure without modifying any website source or database schema.
    """
    if importlib.util.find_spec("donatix.tickets") is None:
        return
    from donatix import tickets
    _MANAGERS[str(Path(manager.config.db_path).resolve())] = manager
    if getattr(tickets.create, "_android_erasure_guard", False):
        return
    original_create, original_add, original_get = tickets.create, tickets.add, tickets.get
    original_status = tickets.set_status

    def owner_manager(conn):
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        return _MANAGERS.get(str(Path(path).resolve())) if path else None

    def available(conn, uid):
        if _deleted(accounts.get_user(conn, uid)):
            raise tickets.TicketError("Аккаунт и обращение удалены.")

    @wraps(original_create)
    def create(conn, config, user_id, *args, **kwargs):
        active = owner_manager(conn)
        if active is None:
            return original_create(conn, config, user_id, *args, **kwargs)
        with active.user_lock(user_id):
            available(conn, user_id)
            return original_create(conn, config, user_id, *args, **kwargs)

    @wraps(original_add)
    def add(conn, config, ticket_id, *args, **kwargs):
        row, active = original_get(conn, ticket_id), owner_manager(conn)
        if row is None or active is None:
            return original_add(conn, config, ticket_id, *args, **kwargs)
        with active.user_lock(row["user_id"]):
            available(conn, row["user_id"])
            return original_add(conn, config, ticket_id, *args, **kwargs)

    @wraps(original_status)
    def set_status(conn, ticket_id, *args, **kwargs):
        row, active = original_get(conn, ticket_id), owner_manager(conn)
        if row is None or active is None:
            return original_status(conn, ticket_id, *args, **kwargs)
        with active.user_lock(row["user_id"]):
            available(conn, row["user_id"])
            return original_status(conn, ticket_id, *args, **kwargs)

    @wraps(original_get)
    def get(conn, ticket_id, user_id=None):
        row = original_get(conn, ticket_id, user_id)
        return None if row is not None and owner_manager(conn) is not None and _deleted(
            accounts.get_user(conn, row["user_id"])) else row

    create._android_erasure_guard = True
    tickets.create, tickets.add, tickets.get, tickets.set_status = create, add, get, set_status


class DeleteIn(BaseModel):
    confirmation: str = Field(max_length=40)
    password: str = Field(default="", max_length=512)


def authenticate(request, conn, current, password):
    sid = request.session.get("sid")
    row = conn.execute("SELECT created_at FROM logins WHERE sid=? AND user_id=? AND ended_at IS NULL",
                       (sid, current["id"])).fetchone()
    if not row or request.session.get("user_id") != current["id"]:
        raise ApiError("Войдите в свой аккаунт заново.", "session_required", 401)
    if password:
        if not security.verify_password(password, current["password_hash"]):
            raise ApiError("Неверный текущий пароль.", "reauth_required", 403)
        return
    try:
        created = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, TypeError):
        age = 301
    if not -30 <= age <= 300:
        raise ApiError("Введите текущий пароль или выйдите и войдите заново, затем подтвердите удаление в течение 5 минут.",
                       "reauth_required", 403)


class Erasure:
    def __init__(self, config, store):
        self.config, self.store = config, store
        # Let requests already accepted by the original site finish before
        # removing their owner's identity (e.g. an in-flight receipt upload).
        # Access is revoked immediately; pending financial work still blocks
        # erasure after this short drain period.
        self.grace_seconds = 45
        self.stop_event = threading.Event()
        self.thread = None
        with store.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS deletions (
              user_id INTEGER PRIMARY KEY, identity TEXT NOT NULL, marker TEXT NOT NULL,
              state TEXT NOT NULL, requested_at REAL NOT NULL, next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS erasure_files (
              user_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
              after_ts REAL NOT NULL, PRIMARY KEY(user_id,kind,name));
            """)
        _ticket_guards(self)

    def identity(self, user):
        return hmac.new(self.config.secret_key.encode(),
                        f"delete:{user['id']}:{user['created_at']}".encode(), hashlib.sha256).hexdigest()

    @contextmanager
    def user_lock(self, uid):
        with open(str(self.store.path) + ".account-" + str(int(uid)) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _columns(conn, table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @contextmanager
    def lock(self):
        with open(str(self.store.path) + ".erasure.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ApiError("Обработка удаления уже выполняется. Повторите через несколько секунд.", "busy", 409)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def blockers(conn, uid):
        user = accounts.get_user(conn, uid)
        if user is None:
            return []
        result = []
        if user["role"] == "admin":
            result.append("Администратор должен сначала передать управление сервисом.")
        if user["balance_micro"] != 0:
            result.append("Остаток баланса: " + fmt(user["balance_micro"]) + " USD. Требуется расчёт с поддержкой; деньги не будут удалены.")
        if conn.execute("SELECT 1 FROM orders WHERE user_id=? AND status IN ('processing','attention')", (uid,)).fetchone():
            result.append("Есть незавершённые заказы. Удаление завершится после выдачи или возврата и расчёта баланса.")
        if conn.execute("SELECT 1 FROM payments WHERE user_id=? AND status='pending'", (uid,)).fetchone():
            result.append("Есть пополнения на проверке. Сначала необходимо завершить проверку и расчёты.")
        if "intent" in Erasure._columns(conn, "payments") and conn.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND status='paid' AND intent IS NOT NULL AND intent_order IS NULL", (uid,)
        ).fetchone():
            result.append("Оплаченная покупка ещё обрабатывается. Сначала необходимо завершить заказ или возврат.")
        return result

    def info(self, conn, current):
        return {"ok": True, "balance_usd": fmt(current["balance_micro"]),
                "blockers": self.blockers(conn, current["id"]), "coins": current["dcoin"],
                "erased": ERASED, "retained": RETAINED,
                "processing": "Вход отключается сразу. Очистка выполняется в фоне после завершения уже принятых запросов и расчётов.",
                "support_contact": self.config.support_contact,
                "web_url": self.config.base_url.rstrip("/") + PUBLIC_PATH}

    def request(self, conn, current):
        uid = current["id"]
        if current["role"] == "admin":
            raise ApiError("Сначала передайте административные права другому владельцу.", "owner_account", 409)
        with self.lock():
            with db.tx(conn):
                current = accounts.get_user(conn, uid)
                if not current or current["status"] == "blocked":
                    raise ApiError("Аккаунт уже недоступен.", "unauthorized", 401)
                # Persist intent before committing deactivation. A crash can be
                # recovered by the worker without restoring a deleted identity.
                with self.store.transaction() as c:
                    c.execute("INSERT INTO deletions(user_id,identity,marker,state,requested_at) VALUES(?,?,?,'waiting',?) ON CONFLICT(user_id) DO NOTHING",
                              (uid, self.identity(current), "deleted_" + secrets.token_hex(16), time.time()))
                conn.execute("UPDATE users SET status='blocked',webhook_url=NULL,webhook_secret=NULL WHERE id=?", (uid,))
                conn.execute("UPDATE logins SET ended_at=? WHERE user_id=? AND ended_at IS NULL", (db.now(), uid))
                conn.execute("UPDATE api_keys SET revoked_at=?,key_enc=NULL WHERE user_id=?", (db.now(), uid))
                conn.execute("UPDATE bots SET enabled=0,updated_at=? WHERE user_id=?", (db.now(), uid))
                conn.execute("DELETE FROM push_subs WHERE user_id=?", (uid,))
                if self._columns(conn, "guest_keys"):
                    conn.execute("DELETE FROM guest_keys WHERE user_id=?", (uid,))
            self._mobile_cleanup(uid)
            self._poke_bots()
            reasons = self._process(conn, uid)
        state = "waiting" if reasons else "completed"
        return {"ok": True, "state": state, "reasons": reasons,
                "message": ("Заявка принята. Вход и ключи отключены. Очистка выполняется в фоне после завершения уже принятых запросов, расчётов и очистки файлов. Если есть остаток баланса, обратитесь в поддержку для возврата."
                            if reasons else "Аккаунт и персональные данные удалены. Обезличенные расчётные записи сохранены для учёта и защиты от повторных платежей.")}

    def _mobile_cleanup(self, uid):
        with self.store.transaction() as c:
            c.execute("DELETE FROM devices WHERE user_id=?", (uid,))
            c.execute("DELETE FROM flows WHERE user_id=?", (uid,))

    @staticmethod
    def _poke_bots():
        from donatix import bots
        if bots.RUNNER:
            bots.RUNNER.poke()

    def _update(self, conn, table, uid, values):
        columns = self._columns(conn, table)
        values = {key: value for key, value in values.items() if key in columns}
        if values:
            assignments = ",".join(key + "=?" for key in values)
            conn.execute(f"UPDATE {table} SET {assignments} WHERE " + ("id=?" if table == "users" else "user_id=?"),
                         [*values.values(), uid])

    def _erase(self, conn, user, job):
        uid = user["id"]
        # Stage private-file paths durably before erasing their references.
        receipts = [r[0] for r in conn.execute("SELECT receipt_file FROM payments WHERE user_id=? AND receipt_file IS NOT NULL", (uid,))]
        bot_ids = [str(r[0]) for r in conn.execute("SELECT id FROM bots WHERE user_id=?", (uid,))]
        ticket_files = []
        if self._columns(conn, "tickets"):
            ticket_files = [r[0] for r in conn.execute(
                "SELECT m.file FROM ticket_messages m JOIN tickets t ON t.id=m.ticket_id WHERE t.user_id=? AND m.file IS NOT NULL", (uid,))]
        with self.store.transaction() as c:
            for kind, names, delay in (("receipt", receipts, 0), ("bot", bot_ids, 30), ("ticket", ticket_files, 0)):
                for name in names:
                    c.execute("INSERT OR IGNORE INTO erasure_files VALUES(?,?,?,?)", (uid, kind, name, time.time() + delay))
        tg_ids = [r[0] for r in conn.execute("SELECT tg_id FROM support_links WHERE user_id=? UNION SELECT tg_id FROM shop_users WHERE user_id=?", (uid, uid))]
        for tg in tg_ids:
            shared = conn.execute("SELECT 1 FROM support_links WHERE tg_id=? AND user_id<>? UNION SELECT 1 FROM shop_users WHERE tg_id=? AND user_id<>?", (tg, uid, tg, uid)).fetchone()
            if not shared:
                for table in ("support_history", "support_memory", "support_codes"):
                    conn.execute(f"DELETE FROM {table} WHERE tg_id=?", (tg,))
        # The name cache is shared: never remove a player used by another
        # customer's order. Account-owned order/shop IDs are always erased.
        owned_ids = set()
        for row in conn.execute("SELECT fields_json FROM orders WHERE user_id=?", (uid,)):
            try:
                fields = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if not isinstance(fields, dict):
                continue
            for key, value in fields.items():
                if key.lower() in ("uid", "id", "player_id", "user_id", "account_id") and isinstance(value, (str, int)):
                    owned_ids.add(str(value))
        has_intents = "intent" in self._columns(conn, "payments")
        if has_intents:
            for row in conn.execute("SELECT intent FROM payments WHERE user_id=? AND intent IS NOT NULL", (uid,)):
                try:
                    payload = json.loads(row[0])
                    items = payload.get("items", []) if isinstance(payload, dict) else []
                    for item in items if isinstance(items, list) else []:
                        fields = item.get("fields", {}) if isinstance(item, dict) else {}
                        for key, value in fields.items() if isinstance(fields, dict) else []:
                            if key.lower() in ("uid", "id", "player_id", "user_id", "account_id") and isinstance(value, (str, int)):
                                owned_ids.add(str(value))
                except (ValueError, TypeError):
                    continue
        for player_id in owned_ids:
            if has_intents and conn.execute("""SELECT 1 FROM payments p,
                json_each(CASE WHEN json_valid(p.intent) THEN json_extract(p.intent,'$.items') ELSE '[]' END) item,
                json_each(CASE WHEN json_valid(item.value) THEN json_extract(item.value,'$.fields') ELSE '{}' END) f
                WHERE p.user_id<>? AND lower(f.key) IN ('uid','id','player_id','user_id','account_id')
                  AND CAST(f.value AS TEXT)=? LIMIT 1""", (uid, player_id)).fetchone():
                continue
            conn.execute("""DELETE FROM player_names WHERE uid=? AND NOT EXISTS (
                SELECT 1 FROM orders o, json_each(CASE WHEN json_valid(o.fields_json)
                  THEN o.fields_json ELSE '{}' END) f
                WHERE o.user_id<>? AND lower(f.key) IN ('uid','id','player_id','user_id','account_id')
                  AND CAST(f.value AS TEXT)=?)""", (player_id, uid, player_id))
        for tg in tg_ids:
            if not conn.execute("SELECT 1 FROM support_links WHERE tg_id=? AND user_id<>? UNION SELECT 1 FROM shop_users WHERE tg_id=? AND user_id<>?", (tg, uid, tg, uid)).fetchone():
                conn.execute("DELETE FROM support_tickets WHERE tg_id=? AND user_id IS NULL", (tg,))
        conn.execute("DELETE FROM shop_watch WHERE chat_id IN (SELECT tg_id FROM shop_users WHERE user_id=?)", (uid,))
        conn.execute("DELETE FROM payment_msgs WHERE payment_id IN (SELECT id FROM payments WHERE user_id=?)", (uid,))
        # References in referral bonuses are accounting records of a different
        # customer: redact only this account's name, keeping amounts and status.
        for reward in conn.execute("SELECT r.referrer_id,r.amount_micro,o.public_id FROM referral_rewards r JOIN orders o ON o.id=r.order_id WHERE r.referred_id=?", (uid,)):
            note = f"Реферальный бонус: заказ {reward['public_id']} клиента {user['login']}"
            conn.execute("UPDATE transactions SET note=? WHERE user_id=? AND note=?",
                         (f"Реферальный бонус: заказ {reward['public_id']} удалённого аккаунта", reward["referrer_id"], note))
            text = f"🎁 Реферальный бонус ${fmt(reward['amount_micro'])}: ваш приглашённый {user['login']} сделал заказ."
            conn.execute("UPDATE notifications SET text=? WHERE user_id=? AND text=?",
                         (f"🎁 Реферальный бонус ${fmt(reward['amount_micro'])}: приглашённый аккаунт удалён.", reward["referrer_id"], text))
        # Financial links remain valid, but contain neither player IDs nor
        # credentials, delivery codes, contact information or uploaded receipts.
        self._update(conn, "orders", uid, {"fields_json": "{}", "delivery_json": None, "error": None,
                     "supplier_order_id": None, "supplier_status": None, "client_idem_key": None,
                     "api_key_id": None, "webhook_state": "none"})
        conn.execute("UPDATE orders SET supplier_idem_key='erased-' || id WHERE user_id=?", (uid,))
        self._update(conn, "payments", uid, {"reference": None, "receipt_file": None, "receipt_ai": None,
                     "admin_note": None, "pay_url": None, "pay_address": None,
                     "intent": None, "intent_order": None})
        conn.execute("UPDATE bank_notices SET sender='',card_tail='',comment='',body='',note='' WHERE payment_id IN (SELECT id FROM payments WHERE user_id=?)", (uid,))
        self._update(conn, "transactions", uid, {"note": "Обезличенная операция удалённого аккаунта"})
        self._update(conn, "bots", uid, {"token_enc": security.seal(self.config.secret_key, ""),
                     "key_enc": None, "api_key_id": None, "username": None, "admin_ids": "",
                     "enabled": 0, "last_warn_at": None, "active_since": None, "disabled_reason": "account_deleted"})
        # Notification IDs must never regress: the existing push worker uses
        # the global high-water mark and SQLite INTEGER PRIMARY KEY can reuse
        # deleted high IDs. Keep empty rows, erasing all notification content.
        # The current site's daily cleanup deletes old read notifications.
        # Keep these empty anonymous ID reservations unread so cleanup cannot
        # recycle a push cursor's highest ID into another customer's event.
        self._update(conn, "notifications", uid, {"text": "", "link": None, "read_at": None})
        if self._columns(conn, "tickets"):
            conn.execute("DELETE FROM ticket_messages WHERE ticket_id IN (SELECT id FROM tickets WHERE user_id=?)", (uid,))
            # Keep anonymous ticket IDs reserved: old Telegram reply buttons
            # must never target a newly created ticket of another customer.
            self._update(conn, "tickets", uid, {"subject": "", "topic": "other", "order_ref": None,
                         "status": "closed", "admin_msg": None, "client_seen_at": None, "admin_seen_at": None})
        if self._columns(conn, "guest_keys"):
            conn.execute("DELETE FROM guest_keys WHERE user_id=?", (uid,))
        for table in ("logins", "api_keys", "push_subs", "support_links", "support_codes",
                      "support_link_codes", "support_tickets", "shop_users", "dcoin_ledger"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM tg_logins WHERE user_id=? OR link_user_id=?", (uid, uid))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dc_video_ack'").fetchone():
            conn.execute("DELETE FROM dc_video_ack WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM visits WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM settings WHERE key IN (?, ?, ?) OR key LIKE ?",
                     (f"tz.user.{uid}", f"bots.block.{uid}", f"bot_blocked:{uid}", f"user.{uid}.%"))
        if user["dcoin"]:
            from donatix import dcoin
            state = dcoin.state(conn)
            units = min(max(user["dcoin"], 0), state["supply"])
            if units:
                dcoin._point(conn, state["pool"], state["supply"] - units, "account_erasure", reserve=state["reserve"] + units)
        # Reserved anonymous IDs prevent an old session/worker/file job from
        # ever being mistaken for a newly registered customer or bot.
        self._update(conn, "users", uid, {"email": job["marker"] + "@deleted.invalid", "login": job["marker"],
                     "password_hash": "deleted", "phone": None, "google_sub": None, "project": None,
                     "webhook_url": None, "webhook_secret": None, "ref_code": None, "referred_by": None,
                     "last_active_at": None, "markup_override": None, "dcoin": 0, "status": "blocked"})

    def _process(self, conn, uid):
        with self.store.connect() as c:
            job = c.execute("SELECT * FROM deletions WHERE user_id=?", (uid,)).fetchone()
        if job is None:
            return []
        if job["state"] == "waiting" and time.time() < job["requested_at"] + self.grace_seconds:
            return ["Доступ отключён. Уже принятые запросы завершаются; затем данные будут очищены автоматически."]
        with self.user_lock(uid), db.tx(conn):
            user = accounts.get_user(conn, uid)
            if user and self.identity(user) != job["identity"]:
                raise ValueError("Account identity changed; erasure stopped")
            if user and user["login"] != job["marker"]:
                reasons = self.blockers(conn, uid)
                if reasons:
                    return reasons
                self._erase(conn, user, job)
        with self.store.connect() as c:
            c.execute("UPDATE deletions SET state='files' WHERE user_id=?", (uid,))
        self._mobile_cleanup(uid)
        self._poke_bots()
        self._files(conn, uid)
        with self.store.connect() as c:
            pending = c.execute("SELECT 1 FROM erasure_files WHERE user_id=?", (uid,)).fetchone()
            if not pending:
                c.execute("DELETE FROM deletions WHERE user_id=?", (uid,))
        return ["Очистка файлов выполняется в фоне и повторяется при временной ошибке доступа."] if pending else []

    def _files(self, conn, uid):
        with self.store.connect() as c:
            jobs = c.execute("SELECT * FROM erasure_files WHERE user_id=? AND after_ts<=?", (uid, time.time())).fetchall()
        for job in jobs:
            folder = {"bot": "bots", "receipt": "receipts", "ticket": "tickets"}.get(job["kind"])
            if folder is None:
                LOG.warning("Unknown erasure file kind; manual cleanup required")
                continue
            directory = Path(self.config.db_path).parent / folder
            if directory.is_symlink():
                LOG.warning("Unsafe erasure directory; manual cleanup required")
                continue
            base = directory.resolve()
            path = base / job["name"]
            # Private files must be a direct child. Never follow a symlink or
            # accept an absolute/path-traversal name from database metadata.
            if Path(job["name"]).name != job["name"] or path.is_symlink() or path.resolve().parent != base:
                LOG.warning("Unsafe erasure file name; manual cleanup required")
                continue
            try:
                if job["kind"] == "bot":
                    bot = conn.execute("SELECT user_id,enabled FROM bots WHERE id=?", (job["name"],)).fetchone()
                    if bot and (bot["user_id"] != uid or bot["enabled"]):
                        continue
                    pidfile = path / "bot.pid"
                    if pidfile.exists():
                        try:
                            pid = int(pidfile.read_text().strip())
                            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                            if b"app.main" in cmdline:
                                continue
                        except (OSError, ValueError):
                            pass
                    if path.exists():
                        shutil.rmtree(path)
                elif job["kind"] == "receipt":
                    if not conn.execute("SELECT 1 FROM payments WHERE receipt_file=?", (job["name"],)).fetchone():
                        path.unlink(missing_ok=True)
                else:
                    if not conn.execute("SELECT 1 FROM ticket_messages WHERE file=?", (job["name"],)).fetchone():
                        path.unlink(missing_ok=True)
                # A file still referenced by another account belongs to that
                # account's records; retain it, dropping only this owner's job.
            except OSError:
                continue  # Durable job remains and is retried after restart.
            with self.store.connect() as c:
                c.execute("DELETE FROM erasure_files WHERE user_id=? AND kind=? AND name=?", (uid, job["kind"], job["name"]))

    def run_once(self):
        with self.store.connect() as c:
            jobs = c.execute("SELECT user_id FROM deletions WHERE next_try<=? ORDER BY next_try,requested_at LIMIT 20", (time.time(),)).fetchall()
        if not jobs:
            return
        with self.lock(), closing(db.connect(self.config.db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            for job in jobs:
                try:
                    self._process(conn, job["user_id"])
                except Exception:
                    LOG.warning("An account erasure will retry; other queued accounts can proceed")
                finally:
                    # Waiting balances must not starve newer deletion requests.
                    with self.store.connect() as c:
                        c.execute("UPDATE deletions SET next_try=? WHERE user_id=?", (time.time() + 30, job["user_id"]))

    def start(self):
        self.thread = threading.Thread(target=self.run, name="donatix-account-erasure", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOG.warning("Account erasure will retry; no unrelated account modified", exc_info=False)
            self.stop_event.wait(30)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)


def mount(app, router, manager, user_dependency, header_csrf):
    @router.get("/account/deletion")
    def info(current=Depends(user_dependency), conn=Depends(get_conn)):
        return JSONResponse(manager.info(conn, current), headers={"Cache-Control": "no-store", "Vary": "Cookie"})

    def submit(body, request, conn, current):
        _limit(request, "login", "android-delete:" + str(current["id"]))
        if body.confirmation != "УДАЛИТЬ":
            raise ApiError("Введите УДАЛИТЬ для подтверждения.", "confirmation", 422)
        authenticate(request, conn, current, body.password)
        result = manager.request(conn, current)
        request.session.clear()
        return result

    @router.post("/account/deletion")
    def delete(body: DeleteIn, request: Request, current=Depends(user_dependency), conn=Depends(get_conn)):
        header_csrf(request)
        response = JSONResponse(submit(body, request, conn, current), headers={"Cache-Control": "no-store"})
        response.delete_cookie("dx_guest", path="/")
        return response

    public = APIRouter(include_in_schema=False)

    def page(body, status=200):
        return HTMLResponse('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Donatix — удаление аккаунта и данных</title><style>'
            'body{font:17px system-ui;color:#19172b;background:#f5f3ff;max-width:640px;margin:40px auto;padding:24px}'
            'main{background:white;padding:28px;border-radius:20px}label{display:block;margin-top:20px}'
            'input{box-sizing:border-box;width:100%;padding:14px;margin-top:8px;border:1px solid #aaa;border-radius:10px;font:inherit}'
            'button,a.action{display:inline-block;margin-top:20px;padding:14px;background:#5142e6;color:white;border:0;border-radius:10px;font:inherit}'
            '.error{color:#a82032}</style></head><body><main><h1>Удаление аккаунта Donatix и данных</h1>'
            + body + '</main></body></html>', status_code=status, headers={"Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})

    details = '<p>Удаляется: ' + escape(ERASED) + '</p><p>' + escape(RETAINED) + '</p><p>При наличии баланса или незавершённых операций сначала завершаются расчёты. Поддержка: ' + escape(manager.config.support_contact) + '</p>'

    @public.get(PUBLIC_PATH)
    def public_page():
        return page(details + '<p>Можно запросить удаление без установленного приложения. Подтвердите, что аккаунт принадлежит вам.</p><a class="action" href="' + PANEL_PATH + '">Войти и запросить удаление</a>')

    def form_page(request, conn, current, error="", status=200):
        blockers = manager.blockers(conn, current["id"])
        return page(details + '<p>Аккаунт: ' + escape(current["login"]) + '</p>'
            + (('<p class="error">' + escape(error) + '</p>') if error else '')
            + ''.join('<p>' + escape(reason) + '</p>' for reason in blockers)
            + '<form method="post"><input type="hidden" name="csrf" value="' + escape(csrf_token(request)) + '">'
            '<label>Текущий пароль (или повторный вход менее 5 минут назад)<input type="password" name="password" maxlength="512" autocomplete="current-password"></label>'
            '<label>Введите УДАЛИТЬ<input name="confirmation" maxlength="40" required autocomplete="off"></label>'
            '<p>Действие необратимо. Вход, API и боты отключатся. D-коины будут утрачены. Денежный остаток не списывается.</p>'
            '<button>Удалить аккаунт и данные</button></form>', status)

    @public.get(PANEL_PATH)
    def private_page(request: Request, conn=Depends(get_conn)):
        current = session_user(request, conn)
        if current is None:
            request.session["next"] = PANEL_PATH
            return RedirectResponse("/login", 303)
        return form_page(request, conn, current)

    @public.post(PANEL_PATH)
    async def private_submit(request: Request, conn=Depends(get_conn)):
        await check_csrf(request)
        current = session_user(request, conn)
        if current is None:
            return RedirectResponse("/login", 303)
        values = await request.form()
        try:
            body = DeleteIn(confirmation=str(values.get("confirmation", "")), password=str(values.get("password", "")))
            result = submit(body, request, conn, current)
        except ApiError as exc:
            return form_page(request, conn, current, str(exc), exc.http_status)
        except ValidationError:
            return form_page(request, conn, current, "Проверьте длину пароля и подтверждения.", 422)
        response = page('<h2>' + ("Заявка принята" if result["state"] != "completed" else "Данные удалены") + '</h2><p>'
                    + escape(result["message"]) + '</p>' + ''.join('<p>' + escape(reason) + '</p>' for reason in result["reasons"]))
        response.delete_cookie("dx_guest", path="/")
        return response

    app.include_router(public)
