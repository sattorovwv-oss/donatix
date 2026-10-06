"""Telegram-бот для админа: заявки и проблемы приходят с кнопками, ответ — одним нажатием.

Работает через long polling (getUpdates) в отдельном потоке — вебхук и открытый порт не нужны.
Слушается только чат из DONATIX_ALERT_TELEGRAM_CHAT_ID; сообщения из других чатов игнорируются."""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Callable

import httpx

from . import accounts, cashiers, db, orders, payments, supplier_queue
from .config import PAY_METHODS, TIERS, Config
from .money import fmt
from .notify import notify

log = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]  # ряды кнопок: (текст, callback_data)

MENU = [["🧾 Проверка чеков"], ["🏠 Меню", "📊 Сводка"], ["💳 Заявки", "👥 Новые партнёры"],
        ["🛒 Покупатели бота", "⚠️ Проблемные заказы"]]
MENU_EMOJI = ("🏠", "📊", "💳", "👥", "⚠️", "🧾", "🛒")
Screen = tuple[str, "Buttons"]  # экран: текст и кнопки — правится в том же сообщении


def keyboard(buttons: Buttons | None) -> dict[str, Any] | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


class TelegramApi:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._client = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/", timeout=40,
                                    transport=transport)

    def download(self, file_id: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        """Скачать присланный файл (фото иконки)."""
        info = self("getFile", file_id=file_id) or {}
        if int(info.get("file_size") or 0) > max_bytes:
            raise RuntimeError("файл слишком большой")
        resp = self._client.get(f"https://api.telegram.org/file/bot{self._token}/{info.get('file_path', '')}")
        resp.raise_for_status()
        return resp.content

    def __call__(self, method: str, **payload: Any) -> Any:
        payload = {k: v for k, v in payload.items() if v is not None}
        resp = self._client.post(method, json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data.get("result")


# ── События, которые бот присылает сам ─────────────────────────


def payment_event(conn: sqlite3.Connection, payment_id: int, config: Config | None = None) -> tuple[str, Buttons]:
    p = conn.execute("SELECT p.*, u.login, u.fraud_count, u.balance_micro FROM payments p "
                     "JOIN users u ON u.id = p.user_id WHERE p.id = ?", (payment_id,)).fetchone()
    title = payments.title_for(conn, config, p["method"]) if config else PAY_METHODS.get(p["method"], (p["method"],))[0]
    text = (f"💳 <b>Заявка на пополнение #{p['id']}</b>\n"
            f"Клиент: {_e(p['login'])}\nСпособ: {_e(title)}\n"
            f"К переводу: <b>{_e(p['pay_amount'])} {_e(p['pay_currency'])}</b> (${fmt(p['amount_micro'])})"
            + (f"\nЧек / хэш: <code>{_e(p['reference'])}</code>" if p["reference"] else ""))
    su = buyer_of(conn, p["user_id"])
    if su is not None:
        text += f"\nTelegram: {tg_who(su)}"
    if p["fraud_count"]:
        text = (f"🚨 <b>ВНИМАНИЕ: этот клиент уже присылал поддельный чек ({p['fraud_count']} раз)</b>"
                + (f"\nДолг ${fmt(-p['balance_micro'])} — спишется из этого пополнения автоматически"
                   if p["balance_micro"] < 0 else "") + "\nПроверьте поступление по выписке банка!\n\n" + text)
    return text, [[(f"✅ Зачислить ${fmt(p['amount_micro'])}", f"pay:ok:{p['id']}"),
                   ("❌ Отклонить", f"pay:no:{p['id']}")]]


def user_event(conn: sqlite3.Connection, user_id: int) -> tuple[str, Buttons]:
    u = accounts.get_user(conn, user_id)
    text = (f"🆕 <b>Новый партнёр</b>\n{_e(u['login'])} · {_e(u['email'])}"
            + (f"\nПроект: {_e(u['project'])}" if u["project"] else ""))
    return text, [[("✅ Одобрить", f"user:ok:{u['id']}"), ("⛔ Заблокировать", f"user:block:{u['id']}")]]


def order_event(conn: sqlite3.Connection, order_id: int) -> tuple[str, Buttons]:
    o = conn.execute("SELECT o.*, u.login FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
                     (order_id,)).fetchone()
    text = (f"⚠️ <b>Заказ {_e(o['public_id'])} требует внимания</b>\n"
            f"{_e(o['product_name'])} · ${fmt(o['total_micro'])} · клиент {_e(o['login'])}\n"
            f"{_e(o['error'] or 'Поставщик не подтвердил результат — проверьте заказ в панели FazerCards.')}")
    rows: Buttons = [[("💸 Вернуть деньги", f"ord:refund:{o['id']}"), ("✅ Выполнен", f"ord:done:{o['id']}")]]
    if o["supplier_order_id"]:
        rows.insert(0, [("🔄 Проверить у поставщика", f"ord:check:{o['id']}")])
    return text, rows


def summary(conn: sqlite3.Connection) -> str:
    from .worker import supplier_balance_cached

    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    today, month = orders.stats(conn, 1), orders.stats(conn, 30)
    bal = supplier_balance_cached(conn)
    owed = one("SELECT COALESCE(SUM(balance_micro), 0) FROM users WHERE role = 'client'")
    pays = one("SELECT COUNT(*) FROM payments WHERE status = 'pending'")
    users = one("SELECT COUNT(*) FROM users WHERE status = 'pending'")
    problems = one("SELECT COUNT(*) FROM orders WHERE status = 'attention'")
    return (
        "📊 <b>Сводка</b>\n"
        f"Баланс FazerCards: <b>{'$' + str(bal) if bal is not None else '—'}</b>\n"
        f"Балансы клиентов: ${fmt(owed)}\n\n"
        f"Сегодня (с 00:00): {today['orders']} заказов, прибыль ${fmt(today['profit'])}\n"
        f"30 дней: {month['orders']} заказов, выручка ${fmt(month['revenue'])}, прибыль ${fmt(month['profit'])}\n\n"
        f"Заявок на пополнение: {pays}\nНовых партнёров: {users}\nПроблемных заказов: {problems}"
        + supplier_queue.summary_line(conn)
    )


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ── Обработка входящих ──────────────────────────────────────


class AdminBot:
    def __init__(self, config: Config, api: Callable[..., Any] | None = None, supplier: Any = None):
        self.config = config
        self.supplier = supplier
        self.wizard: dict[str, Any] | None = None   # шаги добавления реквизитов в боте
        self.ask: tuple[int, int] | None = None     # ждём сумму пополнения: (Telegram ID покупателя, знак)
        self.topups: dict[int, int] = {}            # сумма, которую админ подтверждает: Telegram ID → micro
        self.finding: set[str] = set()              # нажали «🔎 Найти заявку» — следующий текст ищем
        self.chat_id = str(config.alert_telegram_chat_id).strip()
        self.api = api or TelegramApi(config.alert_telegram_token)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._admins: dict[str, tuple[float, bool]] = {}   # кто админ в группе-чате админки (кэш на 5 мин)

    # отправка
    def send(self, text: str, buttons: Buttons | None = None, **extra: Any) -> None:
        self.api("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                 reply_markup=keyboard(buttons) or extra.get("reply_markup"), disable_web_page_preview=True)

    # цикл
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="donatix-tgbot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        conn = db.connect(self.config.db_path)
        offset = int(db.get_setting(conn, "tg_offset", "0") or 0)
        try:
            self.api("setMyCommands", commands=[
                {"command": "start", "description": "Меню"}, {"command": "menu", "description": "Все разделы"},
                {"command": "stats", "description": "Сводка"},
                {"command": "queue", "description": "Проверка чеков по одному"},
                {"command": "find", "description": "Найти заявку: /find 125"},
                {"command": "buyers", "description": "Покупатели бота-магазина"},
                {"command": "topup", "description": "Пополнить: /topup @username 50"},
                {"command": "payments", "description": "Заявки на пополнение"},
                {"command": "users", "description": "Новые партнёры"},
                {"command": "orders", "description": "Проблемные заказы"},
                {"command": "client", "description": "Клиент по логину: /client login"}])
        except Exception as exc:  # бот может быть ещё не настроен — не мешаем сайту
            log.warning("telegram-бот: %s", exc)
        try:
            while not self._stop.is_set():
                try:
                    updates = self.api("getUpdates", offset=offset, timeout=25,
                                       allowed_updates=["message", "callback_query"]) or []
                except Exception as exc:
                    log.warning("telegram-бот: %s", exc)
                    self._stop.wait(10)
                    continue
                for upd in updates:
                    offset = max(offset, int(upd["update_id"]) + 1)
                    try:
                        self.handle(conn, upd)
                    except Exception:
                        log.exception("telegram-бот: обработка %s", upd.get("update_id"))
                db.set_setting(conn, "tg_offset", str(offset))
        finally:
            conn.close()

    def _trusted(self, chat: dict[str, Any], sender: dict[str, Any] | None) -> bool:
        """Чат админки — личка владельца или группа. В группе слушаем только её админов,
        иначе любой участник группы мог бы зачислять деньги и менять реквизиты."""
        if chat.get("type", "private") == "private":
            return True
        uid = str((sender or {}).get("id") or "")
        if not uid:
            return False
        now = time.monotonic()
        cached = self._admins.get(uid)
        if cached and now - cached[0] < 300:
            return cached[1]
        try:
            member = self.api("getChatMember", chat_id=self.chat_id, user_id=int(uid)) or {}
            ok = member.get("status") in ("creator", "administrator")
        except Exception as exc:  # noqa: BLE001 — не проверили — не пускаем
            log.warning("telegram-бот: проверка админа %s: %s", uid, exc)
            return False
        self._admins[uid] = (now, ok)
        return ok

    def handle(self, conn: sqlite3.Connection, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            msg = cq.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == self.chat_id and \
                    not self._trusted(msg.get("chat") or {}, cq.get("from")):
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
                return
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                cashier = self._cashier(conn, msg, cq.get("from"))
                if cashier is None:
                    self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
                    return
                self.on_cashier_button(conn, cq, cashier)
                return
            data = str(cq.get("data") or "")
            if data.startswith("q:"):
                self.on_queue(conn, cq, self.chat_id)
                return
            result = self.on_button(conn, data)
            if isinstance(result, tuple):  # экран меню — правим то же сообщение
                self.api("answerCallbackQuery", callback_query_id=cq["id"])
                text, buttons = result
                if msg.get("message_id") and "text" in msg:
                    try:
                        self.api("editMessageText", chat_id=self.chat_id, message_id=msg["message_id"],
                                 parse_mode="HTML", text=text, reply_markup=keyboard(buttons),
                                 disable_web_page_preview=True)
                    except RuntimeError as exc:
                        if "not modified" not in str(exc):
                            raise
                else:
                    self.send(text, buttons)
                return
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text=result[:190])
            if data.startswith("pay:") and cashiers.is_tracked(conn, self.chat_id, msg.get("message_id")):
                return   # чек с копиями у кассиров — все копии уже поправил settle()
            if msg.get("message_id"):
                # Кнопки убираем, а под сообщением пишем, что сделано — видно в истории чата.
                # У сообщения с чеком (фото/файл) вместо текста — подпись.
                if "text" in msg:
                    self.api("editMessageText", chat_id=self.chat_id, message_id=msg["message_id"], parse_mode="HTML",
                             text=f"{_e(msg.get('text', ''))}\n\n<b>{_e(result)}</b>", disable_web_page_preview=True)
                else:
                    self.api("editMessageCaption", chat_id=self.chat_id, message_id=msg["message_id"],
                             parse_mode="HTML",
                             caption=cashiers.safe_caption(f"{_e(msg.get('caption', ''))}\n\n<b>{_e(result)}</b>"))
            return
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            self.on_stranger(conn, msg)
            return
        if not self._trusted(msg.get("chat") or {}, msg.get("from")):
            return   # участник группы, но не админ — молчим
        text = (msg.get("text") or "").strip()
        if self.ask and text and not text.startswith("/") and text.split(" ")[0] not in MENU_EMOJI:
            tg_id, sign = self.ask
            self.ask = None
            self.send(*self.ask_topup(conn, tg_id, text, sign))
            return
        self.ask = None
        if self.chat_id in self.finding and text and not text.startswith("/") and text.split(" ")[0] not in MENU_EMOJI:
            self.finding.discard(self.chat_id)
            self.find(conn, self.chat_id, text)
            return
        self.finding.discard(self.chat_id)
        if self.wizard and not text.startswith("/") and text.split(" ")[0] not in MENU_EMOJI:
            self.send(*wizard_step(self, conn, msg))
            return
        self.wizard = None
        self.on_command(conn, text)

    def on_command(self, conn: sqlite3.Connection, text: str) -> None:
        first = text.split()[0] if text else ""
        cmd = first.split("@")[0].lower() if first.startswith("/") else first.lower()
        if cmd in ("/menu", "🏠"):
            self.send(*screen_home(conn, self.config))
        elif cmd == "/client":
            login = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            u = conn.execute("SELECT id FROM users WHERE login = ? OR email = ?", (login, login)).fetchone()
            self.send(*screen_client(conn, self.config, u["id"]) if u else ("Клиент не найден. Пример: /client shop1",
                                                                            [[("👤 Клиенты", "m:clients:0")]]))
        elif cmd == "/fake":   # /fake 123 — чек по заявке оказался поддельным
            arg = text.split()[1] if len(text.split()) > 1 else ""
            if not arg.isdigit():
                self.send("Напишите номер заявки: <code>/fake 123</code> — сумма спишется с баланса клиента.")
                return
            try:
                debt = payments.mark_fake(conn, self.config, int(arg), _admin_id(conn), who="админ (Telegram)")
            except payments.PaymentError as exc:
                self.send(_e(str(exc)))
                return
            self.send(f"🚨 Заявка #{arg}: сумма списана, клиент помечен."
                      + (f" Долг ${fmt(debt)} спишется из следующего пополнения." if debt else ""))
        elif cmd in ("/buyers", "🛒"):
            self.send(*screen_buyers(conn, self.config))
        elif cmd in ("/buyer", "/topup") or (text.startswith("@") or "t.me/" in text) or (
                text.isdigit() and len(text) >= 6 and find_buyer(conn, text)):
            parts = text.split()
            query = parts[1] if cmd in ("/buyer", "/topup") and len(parts) > 1 else parts[0] if parts else ""
            su = find_buyer(conn, query)
            if su is None:
                self.send("🛒 Покупатель не найден. Напишите <b>@username</b> или <b>Telegram ID</b>.\n"
                          "Пополнить сразу: <code>/topup @username 50</code> (сомони) или "
                          "<code>/topup 123456789 5$</code>.\n<i>@username виден, если человек заходил в "
                          "бот-магазин после обновления.</i>")
            elif cmd == "/topup" and len(parts) > 2:
                self.send(*self.ask_topup(conn, su["tg_id"], " ".join(parts[2:]), 1))
            else:
                self.send(*screen_buyer(conn, self.config, su["tg_id"]))
        elif cmd in ("/queue", "🧾"):
            current = db.get_setting(conn, self._queue_key(self.chat_id)) or ""
            self.show_queue(conn, self.chat_id, replace=int(current) if current.isdigit() else None)
        elif cmd in ("/find", "/pay") or (text and not text.startswith("/") and re.search(r"\d", text)
                                          and text.split(" ")[0] not in MENU_EMOJI):
            query = text.split(maxsplit=1)[1] if cmd in ("/find", "/pay") and len(text.split()) > 1 else text
            if cmd in ("/find", "/pay") and query == text:
                self.send("🔎 Пример: <code>/find 125</code> или просто напишите номер заявки.")
            else:
                self.find(conn, self.chat_id, query)
        elif cmd in ("/stats", "📊"):
            self.send(summary(conn))
        elif cmd in ("/payments", "💳"):
            rows = conn.execute("SELECT id FROM payments WHERE status = 'pending' ORDER BY id LIMIT 10").fetchall()
            if not rows:
                self.send("Заявок на пополнение нет.")
            for r in rows:   # запоминаем — при «Ускорить» эти копии тоже удалятся
                text_, buttons_ = payment_event(conn, r["id"], self.config)
                res = self.api("sendMessage", chat_id=self.chat_id, text=text_, parse_mode="HTML",
                               reply_markup=keyboard(buttons_), disable_web_page_preview=True) or {}
                if isinstance(res, dict) and res.get("message_id"):
                    cashiers.remember(conn, r["id"], self.chat_id, res["message_id"], text_)
        elif cmd in ("/users", "👥"):
            rows = conn.execute("SELECT id FROM users WHERE status = 'pending' ORDER BY id LIMIT 10").fetchall()
            self._list(conn, rows, user_event, "Новых партнёров нет.")
        elif cmd in ("/orders", "⚠️"):
            rows = conn.execute("SELECT id FROM orders WHERE status = 'attention' ORDER BY id LIMIT 10").fetchall()
            self._list(conn, rows, order_event, "Проблемных заказов нет.")
        else:
            self.send(f"Бот админки {_e(self.config.site_name)}. Сюда приходят заявки и проблемы — "
                      "отвечайте кнопками под сообщением. Всё остальное — в «🏠 Меню».",
                      reply_markup={"keyboard": [[{"text": t} for t in row] for row in MENU], "resize_keyboard": True})
            self.send(*screen_home(conn, self.config))

    # ── Заявки на пополнение: решение админа или кассира ──

    def resolve_payment(self, conn: sqlite3.Connection, payment_id: int, approve: bool, who: str,
                        tg_id: int | None = None) -> str:
        """Зачислить или отклонить — один раз. Итог сразу видят все: админ и кассиры."""
        admin_id = _admin_id(conn)
        label = f"кассир {who}" if tg_id is not None else "админ (Telegram)"
        if approve:
            try:
                ok = payments.confirm(conn, self.config, payment_id, admin_id, who=label)
            except payments.PaymentError as exc:
                return str(exc)[:190]   # всплывающее окно Telegram — до 200 символов
        else:
            ok = payments.reject(conn, self.config, payment_id, admin_id, "Перевод не найден", who=label)
        if not ok:
            return "Заявка уже обработана"
        if tg_id is not None:
            conn.execute("UPDATE payments SET resolved_tg = ? WHERE id = ?", (str(tg_id), payment_id))
        if approve:
            amount = conn.execute("SELECT amount_micro FROM payments WHERE id = ?", (payment_id,)).fetchone()[0]
            cashiers.settle(conn, self.config, payment_id, f"✅ Зачислено ${fmt(amount)} — {who}", api=self.api)
            return "Зачислено, клиент получил уведомление"
        cashiers.settle(conn, self.config, payment_id, f"❌ Отклонено — {who}", api=self.api)
        return "Отклонено"

    # ── Кассиры: чеки по своим банкам и свой доход ──

    def _cashier(self, conn: sqlite3.Connection, msg: dict[str, Any], sender: dict[str, Any] | None):
        """Кассир — только в личке с ботом и только сам (не пересланное, не группа)."""
        chat = msg.get("chat") or {}
        if chat.get("type", "private") != "private" or not sender:
            return None
        if str(chat.get("id")) != str(sender.get("id")):
            return None
        try:
            return cashiers.active(conn, int(sender["id"]))
        except (TypeError, ValueError):
            return None

    def send_to(self, chat_id: int | str, text: str, buttons: Buttons | None = None) -> None:
        self.api("sendMessage", chat_id=str(chat_id), text=text, parse_mode="HTML",
                 reply_markup=keyboard(buttons), disable_web_page_preview=True)

    def on_stranger(self, conn: sqlite3.Connection, msg: dict[str, Any]) -> None:
        cashier = self._cashier(conn, msg, msg.get("from"))
        if cashier is not None:
            text = (msg.get("text") or "").strip()
            waiting = str(cashier["tg_id"]) in self.finding
            if text and not text.startswith("/") and (waiting or re.search(r"\d", text)):   # номер заявки/операции
                self.finding.discard(str(cashier["tg_id"]))
                self.find(conn, cashier["tg_id"], text, cashier)
                return
            self.send_to(cashier["tg_id"], *screen_cashier_home(conn, self.config, cashier))
            return
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and (msg.get("text") or "").startswith("/start"):
            self.send_to(chat["id"], f"Это служебный бот {_e(self.config.site_name)}.\n"
                                     f"Ваш Telegram ID: <code>{chat['id']}</code>\n"
                                     "Передайте его администратору, чтобы получить доступ.")

    def on_cashier_button(self, conn: sqlite3.Connection, cq: dict[str, Any], cashier: sqlite3.Row) -> None:
        msg = cq.get("message") or {}
        chat_id = cashier["tg_id"]
        try:
            what, action, raw_id = str(cq.get("data") or "").split(":")
            obj_id = int(raw_id)
        except ValueError:
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Неизвестная кнопка")
            return
        if what == "pay" and action in ("ok", "no"):
            p = conn.execute("SELECT method, user_id FROM payments WHERE id = ?", (obj_id,)).fetchone()
            if p is None or not cashiers.handles(conn, self.config, cashier, p["method"]):
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Этот банк вам не назначен")
                return
            # Кнопка — только под настоящим сообщением с этим чеком (подделанную кнопку не принимаем)
            sent = conn.execute("SELECT 1 FROM payment_msgs WHERE payment_id = ? AND chat_id = ? AND message_id = ?",
                                (obj_id, str(chat_id), int(msg.get("message_id") or 0))).fetchone()
            if sent is None:
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
                return
            # Свою же заявку (аккаунт, привязанный к этому Telegram) кассир не решает — только админ
            own = conn.execute("SELECT 1 FROM support_links WHERE tg_id = ? AND user_id = ?",
                               (int(chat_id), p["user_id"])).fetchone()
            if own is not None:
                self.api("answerCallbackQuery", callback_query_id=cq["id"],
                         text="Это ваша заявка — её проверяет админ")
                return
            result = self.resolve_payment(conn, obj_id, action == "ok", cashier["name"], tg_id=chat_id)
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text=result[:190])
            if msg.get("message_id") and not cashiers.is_tracked(conn, chat_id, msg["message_id"]):
                self.api("editMessageReplyMarkup", chat_id=str(chat_id), message_id=msg["message_id"],
                         reply_markup={"inline_keyboard": []})
            return
        if what == "q":
            self.on_queue(conn, cq, chat_id, cashier)
            return
        if what == "cs":
            if action == "pays":
                n = self.send_cashier_payments(conn, cashier)
                self.api("answerCallbackQuery", callback_query_id=cq["id"],
                         text=f"Прислал заявок: {n}" if n else "Новых заявок по вашим банкам нет")
                return
            self.api("answerCallbackQuery", callback_query_id=cq["id"])
            text, buttons = screen_cashier_home(conn, self.config, cashier)
            if msg.get("message_id") and "text" in msg:
                try:
                    self.api("editMessageText", chat_id=str(chat_id), message_id=msg["message_id"],
                             parse_mode="HTML", text=text, reply_markup=keyboard(buttons))
                except RuntimeError as exc:
                    if "not modified" not in str(exc):
                        raise
            else:
                self.send_to(chat_id, text, buttons)
            return
        self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")

    def send_cashier_payments(self, conn: sqlite3.Connection, cashier: sqlite3.Row) -> int:
        """Ожидающие заявки с чеком по банкам кассира — заново, с кнопками."""
        rows = conn.execute("SELECT id, method FROM payments WHERE status = 'pending' AND receipt_file IS NOT NULL "
                            "ORDER BY id LIMIT 30").fetchall()
        mine = [r["id"] for r in rows if cashiers.handles(conn, self.config, cashier, r["method"])][:10]
        for pid in mine:
            send_receipt(conn, self.config, pid, only_chat=cashier["tg_id"])
        return len(mine)

    # ── Покупатели бота-магазина: пополнить вручную ──

    def ask_topup(self, conn: sqlite3.Connection, tg_id: int, raw: str, sign: int) -> Screen:
        """Сумма от админа («50», «50 с», «5$») → экран подтверждения."""
        micro = parse_amount(conn, self.config, raw)
        if micro is None or micro == 0:
            self.ask = (tg_id, sign)
            return ("Не понял сумму. Напишите число: <code>50</code> — это сомони, <code>5$</code> — доллары.",
                    [[("‹ Отмена", f"tb:view:{tg_id}")]])
        self.topups[tg_id] = abs(micro) * (-1 if sign < 0 or micro < 0 else 1)
        return screen_topup_confirm(conn, self.config, tg_id, self.topups[tg_id])

    def do_topup(self, conn: sqlite3.Connection, tg_id: int) -> Screen:
        from . import shopbot
        micro = self.topups.pop(tg_id, 0)
        su = shopbot.shop_user(conn, tg_id)
        if not micro or su is None:
            return screen_buyer(conn, self.config, tg_id, "⚠️ Сумма устарела — выберите ещё раз.")
        rate = shopbot.tjs_rate(conn, self.config)
        amount = shopbot.money(abs(micro), rate)
        note = ("Пополнение вручную" if micro > 0 else "Списание вручную") + " (админ, Telegram)"
        try:
            with db.tx(conn):
                accounts.post_ledger(conn, su["user_id"], micro, note, created_by=_admin_id(conn) or None)
        except accounts.InsufficientBalance:
            return screen_buyer(conn, self.config, tg_id, "⚠️ Столько списать нельзя — баланс уйдёт в минус.")
        notify(conn, self.config, su["user_id"],
               (f"Баланс пополнен на ${fmt(micro)}" if micro > 0 else f"С баланса списано ${fmt(-micro)}") + ".",
               "/panel/transactions")
        told = shopbot.tell_buyer(conn, self.config, tg_id, micro)
        done = ("✅ Пополнено на " if micro > 0 else "✅ Списано ") + f"<b>{amount}</b> (${fmt(abs(micro))})"
        return screen_buyer(conn, self.config, tg_id, done + (
            " — покупатель получил сообщение в боте." if told else
            " — сообщение в бот не дошло (человек мог заблокировать бота)."))

    # ── Проверка чеков по одному: «Принять / Отклонить / Следующая» ──

    def _queue_key(self, chat_id: int | str) -> str:
        return f"queue.msg.{chat_id}"

    def _drop(self, chat_id: int | str, message_id: int | None) -> None:
        if not message_id:
            return
        try:
            self.api("deleteMessage", chat_id=str(chat_id), message_id=int(message_id))
        except Exception as exc:  # noqa: BLE001 — старше 48 часов: просто убираем кнопки
            log.info("очередь чеков: не удалилось %s: %s", message_id, exc)
            try:
                self.api("editMessageReplyMarkup", chat_id=str(chat_id), message_id=int(message_id),
                         reply_markup={"inline_keyboard": []})
            except Exception:  # noqa: BLE001
                pass

    def show_queue(self, conn: sqlite3.Connection, chat_id: int | str, cashier: sqlite3.Row | None = None,
                   after: int = 0, replace: int | None = None) -> None:
        """Следующий чек после `after` (по кругу). Старую карточку убираем — на экране всегда один чек."""
        from .payments import receipts_dir
        ids = queue_ids(conn, self.config, cashier)
        self._drop(chat_id, replace)
        if not ids:
            res = self.api("sendMessage", chat_id=str(chat_id), parse_mode="HTML",
                           text="✅ <b>Все чеки проверены</b> — очередь пуста.\nНовые чеки придут сюда сами.",
                           reply_markup=keyboard([[("🔄 Проверить ещё раз", "q:start:0")]])) or {}
            db.set_setting(conn, self._queue_key(chat_id), str(res.get("message_id") or ""))
            return
        pid = next((i for i in ids if i > after), ids[0])
        p = conn.execute("SELECT receipt_file, amount_micro FROM payments WHERE id = ?", (pid,)).fetchone()
        caption = (f"🧾 <b>Проверка чеков · {ids.index(pid) + 1} из {len(ids)}</b>\n\n"
                   + receipt_text(conn, self.config, pid))
        buttons: Buttons = [[(f"✅ Принять ${fmt(p['amount_micro'])}", f"q:ok:{pid}"), ("❌ Отклонить", f"q:no:{pid}")],
                            [("⏭ Следующая" if len(ids) > 1 else "🔄 Обновить", f"q:next:{pid}")]]
        path = receipts_dir(self.config) / p["receipt_file"]
        mid = cashiers.send_file(self.config, chat_id, caption, buttons, path,
                                 photo=not p["receipt_file"].endswith(".pdf")) if path.exists() else None
        if mid is None:   # файл не дошёл — заявка всё равно видна текстом
            res = self.api("sendMessage", chat_id=str(chat_id), parse_mode="HTML", reply_markup=keyboard(buttons),
                           text=caption + "\n\n⚠️ Файл чека не открылся — посмотрите в админке сайта.") or {}
            mid = res.get("message_id")
        db.set_setting(conn, self._queue_key(chat_id), str(mid or ""))

    def on_queue(self, conn: sqlite3.Connection, cq: dict[str, Any], chat_id: int | str,
                 cashier: sqlite3.Row | None = None) -> None:
        msg = cq.get("message") or {}
        mid = msg.get("message_id")
        try:
            _, action, raw = str(cq.get("data") or "").split(":")
            pid = int(raw)
        except ValueError:
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Неизвестная кнопка")
            return
        if action == "start":
            self.api("answerCallbackQuery", callback_query_id=cq["id"])
            current = db.get_setting(conn, self._queue_key(chat_id)) or ""
            self.show_queue(conn, chat_id, cashier, replace=int(current) if current.isdigit() else None)
            return
        if action == "find":
            self.api("answerCallbackQuery", callback_query_id=cq["id"])
            self.finding.add(str(chat_id))
            self.send_to(chat_id, "🔎 <b>Поиск заявки</b>\nНапишите сюда номер заявки (например, <code>#125</code>) "
                                  "или номер операции из чека — покажу чек и чем всё закончилось.")
            return
        # Кнопки работают только на текущей карточке очереди — старую или подделанную не принимаем
        if str(mid or "") != (db.get_setting(conn, self._queue_key(chat_id)) or ""):
            self.api("answerCallbackQuery", callback_query_id=cq["id"],
                     text="Карточка устарела — откройте очередь заново")
            return
        if action in ("ok", "no"):
            if pid not in queue_ids(conn, self.config, cashier) and conn.execute(
                    "SELECT 1 FROM payments WHERE id = ? AND status = 'pending'", (pid,)).fetchone():
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Этот чек вам не назначен")
                return
            if cashier is None:
                result = self.resolve_payment(conn, pid, action == "ok", "админ")
            else:
                result = self.resolve_payment(conn, pid, action == "ok", cashier["name"], tg_id=cashier["tg_id"])
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text=result[:190])
        elif action == "next":
            self.api("answerCallbackQuery", callback_query_id=cq["id"])
        else:
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Неизвестная кнопка")
            return
        self.show_queue(conn, chat_id, cashier, after=pid, replace=mid)

    def find(self, conn: sqlite3.Connection, chat_id: int | str, query: str,
             cashier: sqlite3.Row | None = None) -> None:
        """Поиск заявки: чек, все данные и чем закончилось. Ждёт решения — с кнопками."""
        from .payments import receipts_dir
        ids = find_payments(conn, self.config, query, cashier)
        if not ids:
            self.send_to(chat_id, f"🔎 Заявку «{_e(query[:40])}» не нашёл.\n"
                                  "Напишите номер заявки, например <code>#125</code>, или номер операции из чека.",
                         [[("🔎 Искать ещё", "q:find:0")]])
            return
        for pid in ids:
            p = conn.execute("SELECT status, receipt_file FROM payments WHERE id = ?", (pid,)).fetchone()
            caption = receipt_text(conn, self.config, pid) + "\n\n" + status_line(conn, self.config, pid)
            can_decide = p["status"] == "pending" and p["receipt_file"] and (
                cashier is None or pid in queue_ids(conn, self.config, cashier))   # свой банк и не своя заявка
            buttons = payment_event(conn, pid, self.config)[1] if can_decide else None
            if p["status"] == "pending" and p["receipt_file"] and not can_decide:
                caption += "\n<i>Эту заявку решает кассир другого банка или админ.</i>"
            path = receipts_dir(self.config) / p["receipt_file"] if p["receipt_file"] else None
            mid = None
            if path is not None and path.exists():
                mid = cashiers.send_file(self.config, chat_id, caption, buttons, path,
                                         photo=not p["receipt_file"].endswith(".pdf"))
            if mid is None:
                res = self.api("sendMessage", chat_id=str(chat_id), text=caption, parse_mode="HTML",
                               reply_markup=keyboard(buttons), disable_web_page_preview=True) or {}
                mid = res.get("message_id") if isinstance(res, dict) else None
            if buttons and mid:   # решение отсюда тоже поправит все копии
                cashiers.remember(conn, pid, chat_id, mid, caption)

    def greet_cashier(self, conn: sqlite3.Connection, tg_id: int) -> bool:
        """Первое сообщение новому кассиру. False — он ещё не нажал /start в боте."""
        c = cashiers.get(conn, tg_id)
        if c is None:
            return False
        try:
            self.send_to(tg_id, f"👋 Вам открыт доступ кассира в {_e(self.config.site_name)}.\n\n"
                                "Сюда будут приходить чеки о пополнении по вашим банкам. Проверьте, что деньги "
                                "пришли, и нажмите «✅ Зачислить» или «❌ Отклонить».")
            self.send_to(tg_id, *screen_cashier_home(conn, self.config, c))
            return True
        except Exception as exc:  # noqa: BLE001 — человек не открыл бота: скажем админу
            log.info("кассир %s: %s", tg_id, exc)
            return False

    def _list(self, conn, rows, make, empty: str) -> None:
        if not rows:
            self.send(empty)
        for r in rows:
            self.send(*make(conn, r["id"]))

    def on_button(self, conn: sqlite3.Connection, data: str) -> str | Screen:
        try:
            what, action, raw_id = data.split(":")
            obj_id = int(raw_id)
        except ValueError:
            return "Неизвестная кнопка"
        if what != "pm":
            self.wizard = None  # ушли в другой раздел — незаконченное добавление реквизитов сбрасываем
        if what != "tb":
            self.ask = None
        if what in MENU_HANDLERS:
            return MENU_HANDLERS[what](self, conn, action, obj_id)
        admin_id = _admin_id(conn)
        if what == "pay":
            return self.resolve_payment(conn, obj_id, action == "ok", "админ")
        if what == "user":
            u = accounts.get_user(conn, obj_id)
            if u is None or u["role"] == "admin":
                return "Нельзя"
            status = "active" if action == "ok" else "blocked"
            accounts.update_user_admin(conn, obj_id, status=status, tier=u["tier"],
                                       markup_override=str(u["markup_override"] or ""))
            if status == "active" and u["status"] != "active":
                notify(conn, self.config, obj_id, "Аккаунт активирован — можно пополнять баланс и делать заказы.",
                       "/panel")
            return "Одобрен" if status == "active" else "Заблокирован"
        if what == "ord":
            if action == "refund":
                ok = orders.fail_and_refund(conn, obj_id, "Отменён администратором", by_admin=admin_id)
                return "Деньги возвращены клиенту" if ok else "Заказ уже закрыт"
            if action == "done":
                return "Отмечен выполненным" if orders.admin_complete(conn, obj_id, "") else "Заказ уже закрыт"
            if action == "check":
                orders.admin_recheck(conn, obj_id)
                return "Проверим у поставщика в ближайшую минуту"
        return "Неизвестная кнопка"


def _admin_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else 0


def send_receipt(conn: sqlite3.Connection, config: Config, payment_id: int,
                 only_chat: int | None = None, note: str = "") -> None:
    """Заявка с приложенным чеком — фото/файлом с кнопками «Зачислить / Отклонить»:
    админу и кассирам, которым назначен этот банк. Все копии запоминаем, чтобы после
    решения поправить каждую (only_chat — прислать заново одному кассиру)."""
    from . import cashiers
    from .payments import receipts_dir
    from .worker import notify_admin_file
    p = conn.execute("SELECT method, receipt_file, receipt_ai, pay_amount, pay_currency, created_at FROM payments "
                     "WHERE id = ?",
                     (payment_id,)).fetchone()
    if not p or not p["receipt_file"]:
        return
    _, buttons = payment_event(conn, payment_id, config)
    caption = (note + "\n\n" if note else "") + receipt_text(conn, config, payment_id)
    path = receipts_dir(config) / p["receipt_file"]
    photo = not p["receipt_file"].endswith(".pdf")
    if only_chat is None:
        mid = notify_admin_file(config, caption, buttons, path, photo=photo)
        cashiers.remember(conn, payment_id, str(config.alert_telegram_chat_id).strip(), mid, caption)
    for c in cashiers.targets(conn, config, p["method"]):
        if only_chat is not None and c["tg_id"] != only_chat:
            continue
        mid = cashiers.send_file(config, c["tg_id"], caption, buttons, path, photo=photo)
        cashiers.remember(conn, payment_id, c["tg_id"], mid, caption)


def receipt_text(conn: sqlite3.Connection, config: Config, payment_id: int) -> str:
    """Всё о заявке с чеком: клиент, сумма, способ и что прочитано в чеке."""
    from . import receipt_ai
    p = conn.execute("SELECT method, receipt_file, receipt_ai, pay_amount, pay_currency, created_at FROM payments "
                     "WHERE id = ?", (payment_id,)).fetchone()
    text = payment_event(conn, payment_id, config)[0] + ("\n🧾 Чек приложен" if p["receipt_file"] else "")
    if p["receipt_file"] and receipt_ai.enabled(config):   # что прочитал ИИ и совпадает ли сумма
        seen = json.loads(p["receipt_ai"]) if p["receipt_ai"] else None
        details = payments.settings(conn, config)["details"].get(p["method"], "")
        text += "\n" + _e(receipt_ai.summary(seen, p["pay_amount"], p["pay_currency"] or "",
                                             our_details=details, created_at=p["created_at"]))
    return text


# ── Проверка чеков по очереди и поиск заявки ───────────────

STATUS_TEXT = {"pending": "⏳ Ждёт проверки", "paid": "✅ Принята", "rejected": "❌ Отклонена",
               "fake": "🚨 Поддельный чек — сумма списана", "cancelled": "🚫 Отменена клиентом"}


def local_time(config: Config, iso: str | None) -> str:
    """Время из базы (UTC) — по-местному, коротко: «04.10 16:43»."""
    from datetime import datetime, timedelta
    if not iso:
        return ""
    try:
        return (datetime.fromisoformat(iso[:19]) + timedelta(hours=config.tz_offset)).strftime("%d.%m %H:%M")
    except ValueError:
        return iso[:16]


def status_line(conn: sqlite3.Connection, config: Config, payment_id: int) -> str:
    p = conn.execute("SELECT status, resolved_who, resolved_at, admin_note, receipt_file, created_at "
                     "FROM payments WHERE id = ?", (payment_id,)).fetchone()
    line = f"<b>{STATUS_TEXT.get(p['status'], p['status'])}</b>"
    if p["status"] == "pending":
        line += "" if p["receipt_file"] else " · чек ещё не прислан"
    else:
        line += "".join(f" · {_e(x)}" for x in (p["resolved_who"], local_time(config, p["resolved_at"])) if x)
        if p["admin_note"] and p["status"] in ("rejected", "fake"):
            line += f"\nПричина: {_e(p['admin_note'])}"
    return f"Статус: {line}\nСоздана: {local_time(config, p['created_at'])}"


def queue_ids(conn: sqlite3.Connection, config: Config, cashier: sqlite3.Row | None = None) -> list[int]:
    """Чеки, которые ждут решения: админу — все, кассиру — по его банкам и не свои."""
    rows = conn.execute("SELECT id, method, user_id FROM payments WHERE status = 'pending' "
                        "AND receipt_file IS NOT NULL ORDER BY id").fetchall()
    if cashier is None:
        return [r["id"] for r in rows]
    own = {r[0] for r in conn.execute("SELECT user_id FROM support_links WHERE tg_id = ?", (int(cashier["tg_id"]),))}
    return [r["id"] for r in rows if r["user_id"] not in own and cashiers.handles(conn, config, cashier, r["method"])]


def find_payments(conn: sqlite3.Connection, config: Config, query: str,
                  cashier: sqlite3.Row | None = None) -> list[int]:
    """Номер заявки, номер операции из чека или то, что клиент написал в «номер чека»."""
    q = query.strip().lstrip("#№ ").strip()
    if not q:
        return []
    key = re.sub(r"[^0-9A-Za-z]", "", q).upper()
    rows = conn.execute(
        "SELECT id, method FROM payments WHERE id = ? OR (receipt_txn = ? AND ? != '') OR reference = ? "
        "ORDER BY id DESC LIMIT 5", (int(q) if q.isdigit() and len(q) < 12 else -1, key, key, q)).fetchall()
    return [r["id"] for r in rows]   # смотреть может любой кассир; решать — только по своим банкам (find)


# ── Покупатели бота-магазина ───────────────────────────────


def tg_who(su: sqlite3.Row) -> str:
    """Имя — ссылка на профиль (работает и без @username), @username, Telegram ID."""
    parts = [f'<a href="tg://user?id={int(su["tg_id"])}">{_e(su["name"] or "без имени")}</a>']
    if su["username"]:
        parts.append(f'<a href="https://t.me/{_e(su["username"])}">@{_e(su["username"])}</a>')
    parts.append(f"ID <code>{int(su['tg_id'])}</code>")
    return " · ".join(parts)


def buyer_of(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM shop_users WHERE user_id = ? ORDER BY last_seen DESC LIMIT 1",
                        (user_id,)).fetchone()


def find_buyer(conn: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    """@username, ссылка t.me/username, Telegram ID или логин tg123456."""
    q = (query or "").strip()
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{3,32})", q)
    if m:
        return conn.execute("SELECT * FROM shop_users WHERE lower(username) = lower(?)", (m.group(1),)).fetchone()
    q = q.lstrip("#").lower()
    q = q[2:] if q.startswith("tg") else q
    if q.isdigit() and len(q) < 16:
        return conn.execute("SELECT * FROM shop_users WHERE tg_id = ?", (int(q),)).fetchone()
    if re.fullmatch(r"[a-z0-9_]{5,32}", q):   # написали username без @
        return conn.execute("SELECT * FROM shop_users WHERE lower(username) = ?", (q,)).fetchone()
    return None


def parse_amount(conn: sqlite3.Connection, config: Config, raw: str) -> int | None:
    """«50», «50 с», «50 сомони» — сомони по курсу; «5$», «$5», «5 usd» — доллары. Вернёт micro со знаком."""
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
    text = (raw or "").strip().lower().replace(",", ".").replace(" ", "")
    m = re.fullmatch(r"([+-]?)\$?(\d+(?:\.\d{1,2})?)(\$|usd|долл\w*|с\.?|tjs|смн|сомон\w*)?", text)
    if not m:
        return None
    try:
        value = Decimal(m.group(2))
    except InvalidOperation:
        return None
    usd = "$" in text or (m.group(3) or "").startswith(("usd", "долл"))
    if not usd:
        value = value / payments.settings(conn, config)["tjs_rate"]
    micro = int((value * 10_000).to_integral_value(ROUND_HALF_UP))
    if micro > 10_000 * 10_000:   # больше $10 000 за раз — скорее опечатка
        return None
    return -micro if m.group(1) == "-" else micro


def screen_buyers(conn: sqlite3.Connection, config: Config) -> Screen:
    from . import shopbot
    rate = shopbot.tjs_rate(conn, config)
    rows = conn.execute("SELECT s.tg_id, s.name, s.username, u.balance_micro FROM shop_users s "
                        "JOIN users u ON u.id = s.user_id ORDER BY s.last_seen DESC LIMIT 12").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM shop_users").fetchone()[0]
    text = (f"🛒 <b>Покупатели бота-магазина</b> — {total}\n"
            "Найти любого: напишите <b>@username</b> или <b>Telegram ID</b>.\n"
            "Пополнить сразу: <code>/topup @username 50</code>\n\nПоследние, кто заходил:")
    return text, [[(f"{r['name'] or 'без имени'}" + (f" @{r['username']}" if r["username"] else f" · {r['tg_id']}")
                    + f" · {shopbot.money(r['balance_micro'], rate)}", f"tb:view:{r['tg_id']}")] for r in rows] \
        + [_back()]


def screen_buyer(conn: sqlite3.Connection, config: Config, tg_id: int, note: str = "") -> Screen:
    from . import shopbot
    su = shopbot.shop_user(conn, tg_id)
    if su is None:
        return "Покупатель не найден.", [_back("tb:list:0")]
    u = accounts.get_user(conn, su["user_id"])
    rate = shopbot.tjs_rate(conn, config)
    n_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (u["id"],)).fetchone()[0]
    text = ((note + "\n\n" if note else "")
            + f"🛒 <b>Покупатель бота</b>\n{tg_who(su)}\n"
            + f"Баланс: <b>{shopbot.money(u['balance_micro'], rate)}</b> (${fmt(u['balance_micro'])})\n"
            + f"Аккаунт: {_e(u['login'])} · заказов: {n_orders}\n"
            + f"В боте с {local_time(config, su['created_at'])} · был {local_time(config, su['last_seen'])}")
    if not su["username"]:
        text += "\n<i>Нет @username — профиль открывается по ссылке на имени.</i>"
    pays = conn.execute("SELECT id, status, pay_amount, pay_currency FROM payments WHERE user_id = ? "
                        "ORDER BY id DESC LIMIT 3", (u["id"],)).fetchall()
    if pays:
        text += "\n\n💳 " + " · ".join(f"#{p['id']} {_e(p['pay_amount'])} {_e(p['pay_currency'])} "
                                       f"{STATUS_TEXT.get(p['status'], p['status']).split(' ')[0]}" for p in pays)
    return text, [
        [(f"+{n} с.", f"tb:a{n}:{tg_id}") for n in (10, 20, 50, 100)],
        [("✍️ Другая сумма", f"tb:custom:{tg_id}"), ("➖ Списать", f"tb:minus:{tg_id}")],
        [("🔄 Обновить", f"tb:view:{tg_id}"), ("‹ Покупатели", "tb:list:0")],
    ]


def screen_topup_confirm(conn: sqlite3.Connection, config: Config, tg_id: int, micro: int) -> Screen:
    from . import shopbot
    su = shopbot.shop_user(conn, tg_id)
    if su is None:
        return "Покупатель не найден.", [_back("tb:list:0")]
    rate = shopbot.tjs_rate(conn, config)
    bal = conn.execute("SELECT balance_micro FROM users WHERE id = ?", (su["user_id"],)).fetchone()[0]
    verb = "Пополнить" if micro > 0 else "Списать"
    return (f"❓ <b>{verb} {shopbot.money(abs(micro), rate)}</b> (${fmt(abs(micro))})\n{tg_who(su)}\n\n"
            f"Баланс сейчас: {shopbot.money(bal, rate)}\nСтанет: <b>{shopbot.money(bal + micro, rate)}</b>",
            [[(f"✅ Да, {verb.lower()}", f"tb:ok:{tg_id}"), ("❌ Отмена", f"tb:view:{tg_id}")]])


def _buyers(bot: AdminBot, conn: sqlite3.Connection, action: str, tg_id: int) -> str | Screen:
    if action == "list":
        return screen_buyers(conn, bot.config)
    if action == "view":
        bot.topups.pop(tg_id, None)
        return screen_buyer(conn, bot.config, tg_id)
    if action.startswith("a") and action[1:].isdigit():
        return bot.ask_topup(conn, tg_id, action[1:], 1)
    if action in ("custom", "minus"):
        bot.ask = (tg_id, -1 if action == "minus" else 1)
        return ("✍️ Напишите сумму " + ("списания" if action == "minus" else "пополнения")
                + ":\n<code>50</code> — сомони, <code>5$</code> — доллары.", [[("‹ Отмена", f"tb:view:{tg_id}")]])
    if action == "ok":
        return bot.do_topup(conn, tg_id)
    return "Неизвестная кнопка"


# ── Меню админа: все разделы сайта кнопками ────────────────


def _dot(b: dict) -> str:
    return "🟢 " if b.get("running") else ("🟡 " if b["enabled"] else "⚪ ")


def _back(to: str = "m:home:0") -> list[tuple[str, str]]:
    return [("‹ Назад", to)]


def screen_home(conn: sqlite3.Connection, config: Config) -> Screen:
    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]
    pays = one("SELECT COUNT(*) FROM payments WHERE status = 'pending'")
    checks = one("SELECT COUNT(*) FROM payments WHERE status = 'pending' AND receipt_file IS NOT NULL")
    users = one("SELECT COUNT(*) FROM users WHERE status = 'pending'")
    probs = one("SELECT COUNT(*) FROM orders WHERE status = 'attention'")
    return (f"🏠 <b>Админка {_e(config.site_name)}</b>\nВсё управление сайтом и ботами — здесь.", [
        [(f"🧾 Проверка чеков · {checks}", "q:start:0"), ("🔎 Найти заявку", "q:find:0")],
        [("📊 Сводка", "m:stats:0"), (f"💳 Заявки · {pays}", "m:pays:0")],
        [(f"👥 Новые · {users}", "m:users:0"), (f"⚠️ Проблемы · {probs}", "m:orders:0")],
        [("👤 Клиенты", "m:clients:0"), ("🤖 Боты клиентов", "m:bots:0")],
        [("💱 Курс", "m:rate:0"), ("🔄 Каталог", "m:cat:0")],
        [("💳 Реквизиты", "pm:list:0"), ("⚙️ Настройки", "m:set:0")],
        [(f"🧾 Кассиры · {len(cashiers.listing(conn))}", "ks:list:0"), ("🛒 Покупатели бота", "tb:list:0")],
    ])


def screen_client(conn: sqlite3.Connection, config: Config, user_id: int) -> Screen:
    u = accounts.get_user(conn, user_id)
    if u is None:
        return "Клиент не найден.", [_back("m:clients:0")]
    n_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)).fetchone()[0]
    n_bots = conn.execute("SELECT COUNT(*) FROM bots WHERE user_id = ?", (user_id,)).fetchone()[0]
    status = {"active": "✅ активен", "pending": "⏳ на проверке", "blocked": "⛔ заблокирован"}[u["status"]]
    markup = accounts.markup_for(u, config)
    text = (f"👤 <b>{_e(u['login'])}</b> · {_e(u['email'])}\n"
            + (f"Проект: {_e(u['project'])}\n" if u["project"] else "")
            + f"Статус: {status}\nБаланс: <b>${fmt(u['balance_micro'])}</b>\n"
            f"Уровень: {u['tier']} · наценка {markup}%\nЗаказов: {n_orders} · ботов: {n_bots}")
    su = buyer_of(conn, user_id)
    if su is not None:
        text += f"\nTelegram: {tg_who(su)}"
    pays = conn.execute("SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,)).fetchall()
    if pays:
        mark = {"pending": "⏳", "paid": "✅", "rejected": "❌", "cancelled": "↩️"}
        text += "\n\n💳 <b>Пополнения</b>\n" + "\n".join(
            f"{mark[p['status']]} #{p['id']} · ${fmt(p['amount_micro'])} · "
            f"{_e(payments.title_for(conn, config, p['method']))}"
            + (f" — {_e(payments.who_label(p))}" if p["status"] != "pending" else " — ждёт проверки")
            for p in pays)
    rows: Buttons = []
    if u["role"] != "admin":
        rows.append([("⛔ Заблокировать", f"cl:block:{user_id}") if u["status"] == "active"
                     else ("✅ Активировать", f"cl:ok:{user_id}")])
        rows.append([(("• " if u["tier"] == t else "") + t, f"cl:{t}:{user_id}") for t in TIERS])
    if su is not None:
        rows.append([("🛒 Пополнить как покупателя бота", f"tb:view:{su['tg_id']}")])
    rows.append(_back("m:clients:0"))
    return text, rows


def screen_bot(conn: sqlite3.Connection, bot_id: int) -> Screen:
    from . import bots
    b = next((x for x in bots.listing(conn) if x["id"] == bot_id), None)
    if b is None:
        return "Бот удалён.", [_back("m:bots:0")]
    state = "🟢 работает" if b.get("running") else ("🟡 запускается" if b["enabled"] else "⚪ остановлен")
    text = (f"🤖 <b>@{_e(b['username'])}</b>\n{state}\nКлиент: {_e(b['login'])} · баланс ${fmt(b['balance_micro'])}\n"
            f"Админы бота: <code>{_e(b['admin_ids'])}</code>"
            + ("\n⚠️ Токен запущен ещё где-то — бот отвечает дважды" if b.get("conflict") else ""))
    rows: Buttons = [[("🔁 Перезапустить", f"bot:restart:{bot_id}"), ("⏸ Остановить", f"bot:stop:{bot_id}")]
                     if b["enabled"] else [("▶️ Запустить", f"bot:start:{bot_id}")]]
    rows.append(_back("m:bots:0"))
    return text, rows


def screen_rate(conn: sqlite3.Connection, config: Config) -> Screen:
    from . import rates
    st = rates.status(conn, config)
    conf = payments.settings(conn, config)
    age = f"{st['age_seconds'] // 60} мин назад" if st["age_seconds"] is not None else "ещё не обновлялся"
    text = (f"💱 <b>Курс: 1 $ = {conf['tjs_rate']} сомони</b>\n"
            + (f"Рынок {st['market']} + запас {st['margin_pct']}% · {_e(st['source'])} · {age}\n"
               if st["auto"] and st["market"] else "")
            + ("Автообновление: ✅ каждые 5 минут" if st["auto"] else "Автообновление: ⛔ курс вручную (в веб-админке)")
            + (f"\n⚠️ {_e(st['error'][:200])}" if st["auto"] and st["error"] else ""))
    return text, [
        [("🔄 Обновить сейчас", "rate:refresh:0"), ("⛔ Выключить авто" if st["auto"] else "✅ Включить авто",
                                                  "rate:auto:0")],
        [("Запас −0.5%", "rate:margin:-5"), ("Запас +0.5%", "rate:margin:5")],
        _back(),
    ]


def screen_settings(conn: sqlite3.Connection, config: Config) -> Screen:
    from . import sitecfg
    v = sitecfg.view(conn, config)
    on = {True: "✅", False: "⛔"}
    text = ("⚙️ <b>Настройки</b>\n"
            "Наценка: " + " · ".join(f"{t} {v['markups'][t]}%" for t in TIERS) + "\n"
            + "⭐ Stars: " + (f"{v['kind_markups']['telegram_stars']}%" if v["kind_markups"]["telegram_stars"]
                             is not None else "как по уровню")
            + " · 👑 Premium: " + (f"{v['kind_markups']['telegram_premium']}%"
                                  if v["kind_markups"]["telegram_premium"] is not None else "как по уровню") + "\n"
            f"Регистрация: {on[v['reg_open']]} · проверка новых: {on[v['require_approval']]}\n"
            f"Конструктор для клиентов: {on[v['client_bots']]} · до {v['max_bots']} ботов\n"
            f"Поддержка: {_e(v['support'] or '—')}")
    rows: Buttons = [[(f"{t} −0.5%", f"mk:{t}:-5"), (f"{t} +0.5%", f"mk:{t}:5")] for t in TIERS]
    for kind, short in (("telegram_stars", "⭐ Stars"), ("telegram_premium", "👑 Premium")):
        rows.append([(f"{short} −0.5%", f"mk:{kind}:-5"), (f"{short} +0.5%", f"mk:{kind}:5")])
    rows += [
        [(f"{on[v['reg_open']]} Регистрация", "set:reg_open:0"),
         (f"{on[v['require_approval']]} Проверка новых", "set:require_approval:0")],
        [(f"{on[v['client_bots']]} Конструктор клиентам", "set:client_bots:0")],
        _back(),
    ]
    return text, rows


def _menu(bot: AdminBot, conn: sqlite3.Connection, action: str, _: int) -> str | Screen:
    cfg = bot.config
    if action == "home":
        return screen_home(conn, cfg)
    if action == "stats":
        return summary(conn), [_back()]
    if action in ("pays", "users", "orders"):
        bot.on_command(conn, {"pays": "/payments", "users": "/users", "orders": "/orders"}[action])
        return screen_home(conn, cfg)
    if action == "clients":
        rows = conn.execute("SELECT id, login, balance_micro, status FROM users WHERE role = 'client' "
                            "ORDER BY balance_micro DESC, id DESC LIMIT 12").fetchall()
        mark = {"active": "", "pending": "⏳ ", "blocked": "⛔ "}
        return ("👤 <b>Клиенты</b> — по балансу. Найти любого: <code>/client логин</code>",
                [[(f"{mark[r['status']]}{r['login']} · ${fmt(r['balance_micro'])}", f"cl:view:{r['id']}")]
                 for r in rows] + [_back()])
    if action == "bots":
        from . import bots
        items = bots.listing(conn)
        return (f"🤖 <b>Боты клиентов</b> — {len(items)}" if items else "🤖 Ботов пока нет.",
                [[(_dot(b) + f"@{b['username']} · {b['login']}", f"bot:view:{b['id']}")] for b in items[:15]]
                + [_back()])
    if action == "rate":
        return screen_rate(conn, cfg)
    if action == "cat":
        from . import catalog_job
        st = catalog_job.status(conn)
        synced = db.get_setting(conn, "catalog_synced_at") or "—"
        n = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0]
        state = ("⏳ идёт загрузка…" if st.get("running") else
                 f"последняя: {_e(synced[:16].replace('T', ' '))} UTC")
        return (f"🔄 <b>Каталог</b>\nТоваров: {n}\nЗагрузка: {state}",
                [[("🔄 Обновить каталог", "cat:sync:0"), ("🖼 + картинки", "cat:all:0")], _back()])
    if action == "set":
        return screen_settings(conn, cfg)
    return "Неизвестная кнопка"


def _client(bot: AdminBot, conn: sqlite3.Connection, action: str, user_id: int) -> str | Screen:
    u = accounts.get_user(conn, user_id)
    if u is None:
        return "Клиент не найден"
    if action != "view" and u["role"] != "admin":
        status, tier = u["status"], u["tier"]
        if action in ("ok", "block"):
            status = "active" if action == "ok" else "blocked"
        elif action in TIERS:
            tier = action
        accounts.update_user_admin(conn, user_id, status=status, tier=tier,
                                   markup_override=str(u["markup_override"] or ""))
        if status == "active" and u["status"] != "active":
            notify(conn, bot.config, user_id, "Аккаунт активирован — можно пополнять баланс и делать заказы.", "/panel")
    return screen_client(conn, bot.config, user_id)


def _bot(bot: AdminBot, conn: sqlite3.Connection, action: str, bot_id: int) -> str | Screen:
    from . import bots
    if action in ("stop", "start", "restart"):
        bots.set_enabled(conn, bot_id, action != "stop", by_admin=True)
        if bots.RUNNER:
            bots.RUNNER.poke()
    return screen_bot(conn, bot_id)


def _rate(bot: AdminBot, conn: sqlite3.Connection, action: str, value: int) -> str | Screen:
    from decimal import Decimal

    from . import rates
    cfg = bot.config
    if action == "refresh":
        if not rates.auto_enabled(conn, cfg):
            return "Сначала включите автообновление"
        rates.reset()
        rates.refresh(conn, cfg, force=True)
    elif action == "auto":
        db.set_setting(conn, "pay.rate_auto", "0" if rates.auto_enabled(conn, cfg) else "1")
        if rates.auto_enabled(conn, cfg):
            rates.reset()
            rates.refresh(conn, cfg, force=True)
    elif action == "margin":
        margin = max(Decimal("-5"), min(Decimal("20"), rates.margin_pct(conn, cfg) + Decimal(value) / 10))
        db.set_setting(conn, "pay.rate_margin_pct", str(margin))
        market = db.get_setting(conn, "pay.rate_market")
        if rates.auto_enabled(conn, cfg) and market:
            db.set_setting(conn, "pay.tjs_rate", str(rates.apply_margin(Decimal(market), margin)))
    return screen_rate(conn, cfg)


def _catalog(bot: AdminBot, conn: sqlite3.Connection, action: str, _: int) -> str | Screen:
    from . import catalog_job
    if bot.supplier is None:
        return "Каталог обновляется только из веб-админки"
    started = catalog_job.start(bot.config, bot.supplier, sync=True, images=action == "all")
    text, rows = _menu(bot, conn, "cat", 0)
    return (text + ("\n\n✅ Запустил. Статус — снова кнопкой «🔄 Каталог»." if started else "\n\nУже идёт.")), rows


def _setting(bot: AdminBot, conn: sqlite3.Connection, key: str, _: int) -> str | Screen:
    from . import sitecfg
    if key in ("reg_open", "require_approval", "client_bots"):
        sitecfg.toggle(conn, bot.config, key)
    return screen_settings(conn, bot.config)


def _markup(bot: AdminBot, conn: sqlite3.Connection, tier: str, delta: int) -> str | Screen:
    from decimal import Decimal

    from . import sitecfg
    if tier in TIERS:
        sitecfg.bump_markup(conn, bot.config, tier, Decimal(delta) / 10)
    elif tier in ("telegram_stars", "telegram_premium"):
        base = bot.config.kind_markups.get(tier, bot.config.markups["bronze"])
        value = max(Decimal("0"), min(Decimal("100"), base + Decimal(delta) / 10))
        sitecfg.save(conn, bot.config, {f"markup_{tier}": str(value)})
    return screen_settings(conn, bot.config)


# ── Кассиры: кабинет кассира и управление ими в админ-боте ──


def screen_cashier_home(conn: sqlite3.Connection, config: Config, c: sqlite3.Row) -> Screen:
    n = len(queue_ids(conn, config, c))
    text = (f"🧾 <b>Кассир {_e(c['name'])}</b>\n"
            f"Ваши банки: {_e(cashiers.banks_label(conn, config, c))}\n"
            f"Ваша доля: <b>{c['percent']}%</b> прибыли с пополнений через ваши банки\n\n"
            + cashiers.income_text(conn, config, c["tg_id"])
            + "\n\n<i>Чеки приходят сюда сами. Решение сразу видит администратор — второй раз заявку не примут.</i>"
            + "\n🔎 Найти любую заявку — кнопка ниже.")
    return text, [[(f"🧾 Проверка чеков · {n}", "q:start:0")], [("🔎 Найти заявку", "q:find:0")],
                  [(f"💳 Все заявки · {n}", "cs:pays:0"), ("🔄 Обновить", "cs:home:0")]]


def screen_cashiers(conn: sqlite3.Connection, config: Config) -> Screen:
    rows = cashiers.listing(conn)
    text = ("🧾 <b>Кассиры</b>\nПроверяют чеки по своим банкам и получают долю прибыли с них. "
            "Остальное — вам.\n\nЧтобы добавить: человек пишет боту /start, бот покажет ему ID — "
            "он присылает ID вам.")
    buttons: Buttons = [[(("🟢 " if r["active"] else "⏸ ") + f"{r['name']} · {r['percent']}%",
                          f"ks:view:{r['tg_id']}")] for r in rows]
    buttons += [[("➕ Добавить кассира", "ks:new:0")], _back()]
    return text, buttons


def screen_cashier(conn: sqlite3.Connection, config: Config, tg_id: int, note: str = "") -> Screen:
    c = cashiers.get(conn, tg_id)
    if c is None:
        return screen_cashiers(conn, config)
    text = (note + f"🧾 <b>{_e(c['name'])}</b> · ID <code>{c['tg_id']}</code>\n"
            f"Статус: {'🟢 работает' if c['active'] else '⏸ отключён — чеки не приходят'}\n"
            f"Банки: {_e(cashiers.banks_label(conn, config, c))}\n"
            f"Доля: <b>{c['percent']}%</b> · вам {100 - c['percent']}%\n\n"
            + cashiers.income_text(conn, config, tg_id))
    return text, [
        [("🏦 Банки", f"ks:banks:{tg_id}")],
        [("−5%", f"ks:pdn:{tg_id}"), (f"{c['percent']}%", f"ks:view:{tg_id}"), ("+5%", f"ks:pup:{tg_id}")],
        [("⏸ Отключить" if c["active"] else "▶️ Включить", f"ks:tog:{tg_id}"), ("🗑 Удалить", f"ks:del:{tg_id}")],
        _back("ks:list:0"),
    ]


def screen_cashier_banks(conn: sqlite3.Connection, config: Config, tg_id: int) -> Screen:
    c = cashiers.get(conn, tg_id)
    if c is None:
        return screen_cashiers(conn, config)
    codes = cashiers.codes_of(c)
    rows: Buttons = [[(("✅ " if codes is None else "▫️ ") + "Все банки (и новые тоже)", f"ks:all:{tg_id}")]]
    for i, m in enumerate(cashiers.bank_methods(conn, config)):
        on = codes is None or m["code"] in codes
        rows.append([(("✅ " if on else "▫️ ") + m["title"], f"ks:b{i}:{tg_id}")])
    rows.append(_back(f"ks:view:{tg_id}"))
    return (f"🏦 <b>Банки кассира {_e(c['name'])}</b>\nЧеки по отмеченным банкам приходят ему. "
            "Вы получаете все чеки в любом случае."), rows


def _cashiers(bot: AdminBot, conn: sqlite3.Connection, action: str, tg_id: int) -> str | Screen:
    cfg = bot.config
    if action == "list":
        return screen_cashiers(conn, cfg)
    if action == "new":
        bot.wizard = {"step": "cashier_id"}
        return ("➕ <b>Новый кассир</b> · шаг 1 из 2\n\nПришлите его <b>Telegram ID</b> (только цифры) "
                "или перешлите сюда любое его сообщение.\n<i>ID он увидит, если напишет этому боту /start.</i>",
                [[("Отмена", "ks:list:0")]])
    if cashiers.get(conn, tg_id) is None:
        return screen_cashiers(conn, cfg)
    if action in ("pup", "pdn"):
        cashiers.bump_percent(conn, tg_id, 5 if action == "pup" else -5)
    elif action == "tog":
        c = cashiers.get(conn, tg_id)
        cashiers.set_active(conn, tg_id, not c["active"])
    elif action == "del":
        return ("Удалить кассира? Чеки ему приходить перестанут. Уже принятые заявки останутся как есть.",
                [[("🗑 Да, удалить", f"ks:delok:{tg_id}"), ("Нет", f"ks:view:{tg_id}")]])
    elif action == "delok":
        cashiers.delete(conn, tg_id)
        return screen_cashiers(conn, cfg)
    elif action == "banks":
        return screen_cashier_banks(conn, cfg, tg_id)
    elif action == "all":
        cashiers.toggle_all(conn, tg_id)
        return screen_cashier_banks(conn, cfg, tg_id)
    elif action.startswith("b") and action[1:].isdigit():
        ms = cashiers.bank_methods(conn, cfg)
        i = int(action[1:])
        if 0 <= i < len(ms):
            cashiers.toggle_method(conn, cfg, tg_id, ms[i]["code"])
        return screen_cashier_banks(conn, cfg, tg_id)
    return screen_cashier(conn, cfg, tg_id)


def cashier_wizard_step(bot: AdminBot, conn: sqlite3.Connection, msg: dict[str, Any]) -> Screen:
    w = bot.wizard or {}
    text = (msg.get("text") or "").strip()
    cancel = [[("Отмена", "ks:list:0")]]
    try:
        if w.get("step") == "cashier_id":
            fwd = (msg.get("forward_from") or {}).get("id")
            tg_id = int(fwd) if fwd else cashiers.parse_id(text)
            if cashiers.get(conn, tg_id):
                raise cashiers.CashierError("Этот человек уже в списке кассиров.")
            if str(tg_id) == bot.chat_id:
                raise cashiers.CashierError("Это ваш собственный ID — вы и так получаете все чеки.")
            name = " ".join(filter(None, [(msg.get("forward_from") or {}).get("first_name")]))
            w.update(step="cashier_name", tg_id=tg_id)
            return (f"➕ ID <code>{tg_id}</code> · шаг 2 из 2\n\nКак его назвать? Имя видно вам в отчётах "
                    "и на чеках: «✅ Зачислено — Али»." + (f"\nНапример: <i>{_e(name)}</i>" if name else ""), cancel)
        if w.get("step") == "cashier_name":
            cashiers.add(conn, w["tg_id"], text, bot.chat_id)
            bot.wizard = None
            greeted = bot.greet_cashier(conn, w["tg_id"])
            note = ("✅ Кассир добавлен. Сейчас он получает чеки по всем банкам — поменять можно в «🏦 Банки».\n"
                    + ("" if greeted else "⚠️ Бот не смог ему написать: пусть откроет этого бота и нажмёт "
                                          "Start — иначе чеки до него не дойдут.\n") + "\n")
            return screen_cashier(conn, bot.config, w["tg_id"], note)
    except cashiers.CashierError as exc:
        return f"⚠️ {_e(str(exc))}", cancel
    bot.wizard = None
    return screen_cashiers(conn, bot.config)


MENU_HANDLERS: dict[str, Callable[..., str | Screen]] = {
    "pm": lambda bot, conn, action, i: _paymethods(bot, conn, action, i),
    "m": _menu, "cl": _client, "bot": _bot, "rate": _rate, "cat": _catalog, "set": _setting, "mk": _markup,
    "ks": _cashiers, "tb": _buyers,
}


# ── Реквизиты в админ-боте: список, вкл/выкл, добавление по шагам с иконкой ──


def _methods(conn: sqlite3.Connection, config: Config) -> list[dict[str, Any]]:
    return payments.settings(conn, config)["all_methods"]


def _as_rows(methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Как хранить обратно: валюта вместе с сетью (USDT:TRC20)."""
    return [{**m, "currency": m.get("choice") or m["currency"]} for m in methods]


def screen_pay_list(conn: sqlite3.Connection, config: Config) -> Screen:
    ms = _methods(conn, config)
    rows: Buttons = [[(("✅ " if m.get("enabled") else "🚫 ") + ("🖼 " if m.get("icon") else "") + m["title"]
                       + f" · {m['currency']}" + (f" {m['network']}" if m.get("network") else ""),
                       f"pm:view:{i}")] for i, m in enumerate(ms)]
    rows.append([("➕ Добавить способ оплаты", "pm:new:0")])
    rows.append(_back())
    return ("💳 <b>Реквизиты для пополнения</b>\nЭто видят клиенты на сайте и владельцы ботов. "
            "✅ — показывается, 🚫 — скрыт, 🖼 — есть иконка." if ms else
            "💳 <b>Реквизиты</b>\nСпособов оплаты пока нет — добавьте первый."), rows


def screen_pay_view(conn: sqlite3.Connection, config: Config, i: int) -> Screen:
    ms = _methods(conn, config)
    if not 0 <= i < len(ms):
        return screen_pay_list(conn, config)
    m = ms[i]
    text = (f"💳 <b>{_e(m['title'])}</b> · {m['currency']}" + (f" · сеть {m['network']}" if m.get("network") else "")
            + f"\n<code>{_e(m['details'] or '— реквизиты не заданы —')}</code>\n\n"
            + ("✅ Показывается клиентам" if m.get("enabled") else "🚫 Скрыт")
            + (" · 🖼 иконка есть" if m.get("icon") else " · без иконки"))
    return text, [
        [("🚫 Скрыть" if m.get("enabled") else "✅ Показывать", f"pm:tog:{i}")],
        [("✏️ Реквизиты", f"pm:det:{i}"), ("🖼 Иконка", f"pm:icon:{i}")],
        [("🗑 Удалить", f"pm:del:{i}")],
        _back("pm:list:0"),
    ]


def _paymethods(bot: AdminBot, conn: sqlite3.Connection, action: str, i: int) -> str | Screen:
    cfg = bot.config
    ms = _methods(conn, cfg)
    if action == "list":
        bot.wizard = None
        return screen_pay_list(conn, cfg)
    if action == "view":
        bot.wizard = None
        return screen_pay_view(conn, cfg, i)
    if action == "new":
        bot.wizard = {"step": "title", "data": {"enabled": True}}
        return ("➕ <b>Новый способ оплаты</b> · шаг 1 из 4\n\nНапишите <b>название</b>, как увидит клиент: "
                "<i>Душанбе Сити</i>, <i>Алиф</i>, <i>USDT TRC20</i>…", [[("Отмена", "pm:list:0")]])
    if action == "cur" and bot.wizard and bot.wizard["step"] == "currency":
        choices = payments.CURRENCY_CHOICES
        if 0 <= i < len(choices):
            bot.wizard["data"]["currency"] = choices[i][0]
            bot.wizard["step"] = "details"
            usdt = choices[i][0].startswith("USDT:")
            return (f"➕ <b>{_e(bot.wizard['data']['title'])}</b> · {_e(choices[i][1])} · шаг 3 из 4\n\n"
                    + ("Пришлите <b>адрес кошелька</b> в этой сети." if usdt else
                       "Пришлите <b>реквизиты</b>: номер карты или телефона и имя получателя.\n"
                       "<i>Например: 5058 2700 1234 5678 · Алиджон М.</i>"), [[("Отмена", "pm:list:0")]])
    if action == "skip" and bot.wizard and bot.wizard["step"] == "icon":
        return _pay_preview(bot)
    if action == "save" and bot.wizard and bot.wizard["step"] == "confirm":
        data = bot.wizard["data"]
        payments.save_methods(conn, _as_rows(ms) + [data])
        bot.wizard = None
        text, rows = screen_pay_list(conn, cfg)
        return "✅ Способ оплаты добавлен — клиенты уже видят его.\n\n" + text, rows
    if not 0 <= i < len(ms):
        return screen_pay_list(conn, cfg)
    if action == "tog":
        ms[i]["enabled"] = not ms[i].get("enabled")
        payments.save_methods(conn, _as_rows(ms))
        return screen_pay_view(conn, cfg, i)
    if action == "det":
        bot.wizard = {"step": "edit_details", "index": i}
        return f"✏️ Пришлите новые реквизиты для <b>{_e(ms[i]['title'])}</b>.", [[("Отмена", f"pm:view:{i}")]]
    if action == "icon":
        bot.wizard = {"step": "edit_icon", "index": i}
        return (f"🖼 Пришлите <b>фото иконки</b> для <b>{_e(ms[i]['title'])}</b> (квадратная картинка, логотип банка).",
                [[("Убрать иконку", f"pm:noicon:{i}")], [("Отмена", f"pm:view:{i}")]])
    if action == "noicon":
        ms[i]["icon"] = ""
        payments.save_methods(conn, _as_rows(ms))
        bot.wizard = None
        return screen_pay_view(conn, cfg, i)
    if action == "del":
        return (f"Удалить способ <b>{_e(ms[i]['title'])}</b>? Клиенты перестанут его видеть.",
                [[("🗑 Да, удалить", f"pm:delok:{i}"), ("Нет", f"pm:view:{i}")]])
    if action == "delok":
        payments.save_methods(conn, _as_rows(ms[:i] + ms[i + 1:]))
        return screen_pay_list(conn, cfg)
    return screen_pay_list(conn, cfg)


def _pay_preview(bot: AdminBot) -> Screen:
    d = bot.wizard["data"]
    bot.wizard["step"] = "confirm"
    label = dict(payments.CURRENCY_CHOICES).get(d.get("currency", "TJS"), d.get("currency"))
    return (f"👀 <b>Проверьте</b>\n\n💳 <b>{_e(d['title'])}</b> · {_e(label)}\n<code>{_e(d['details'])}</code>\n"
            + ("🖼 Иконка загружена" if d.get("icon") else "Без иконки — будет стандартный значок"),
            [[("✅ Сохранить", "pm:save:0")], [("Отмена", "pm:list:0")]])


def _photo_bytes(bot: AdminBot, msg: dict[str, Any]) -> tuple[bytes, str] | None:
    """Фото или картинка файлом → (байты, тип)."""
    if msg.get("photo"):
        return bot.api.download(msg["photo"][-1]["file_id"]), "image/jpeg"
    doc = msg.get("document") or {}
    if doc.get("mime_type") in payments.ICON_TYPES:
        return bot.api.download(doc["file_id"]), doc["mime_type"]
    return None


def wizard_step(bot: AdminBot, conn: sqlite3.Connection, msg: dict[str, Any]) -> Screen:
    w = bot.wizard or {}
    if str(w.get("step", "")).startswith("cashier_"):
        return cashier_wizard_step(bot, conn, msg)
    text = (msg.get("text") or "").strip()
    step = w.get("step")
    try:
        if step == "title":
            if not 2 <= len(text) <= 60:
                return "Название — от 2 до 60 символов. Напишите ещё раз.", [[("Отмена", "pm:list:0")]]
            w["data"]["title"] = text
            w["step"] = "currency"
            return (f"➕ <b>{_e(text)}</b> · шаг 2 из 4\n\nВыберите <b>валюту</b> (для USDT — сеть):",
                    [[(label, f"pm:cur:{i}")] for i, (_, label) in enumerate(payments.CURRENCY_CHOICES)]
                    + [[("Отмена", "pm:list:0")]])
        if step in ("details", "edit_details"):
            if not 4 <= len(text) <= 1000:
                return "Пришлите реквизиты текстом (номер, имя или адрес кошелька).", [[("Отмена", "pm:list:0")]]
            if step == "edit_details":
                ms = _methods(conn, bot.config)
                ms[w["index"]]["details"] = text
                payments.save_methods(conn, _as_rows(ms))
                bot.wizard = None
                return screen_pay_view(conn, bot.config, w["index"])
            w["data"]["details"] = text
            w["step"] = "icon"
            return ("🖼 Шаг 4 из 4 — пришлите <b>фото иконки</b> (логотип банка, квадрат). "
                    "Можно пропустить.", [[("Пропустить", "pm:skip:0")], [("Отмена", "pm:list:0")]])
        if step in ("icon", "edit_icon"):
            got = _photo_bytes(bot, msg)
            if got is None:
                return "Нужна картинка: пришлите фото или файл PNG/JPG.", [[("Отмена", "pm:list:0")]]
            name = payments.save_icon(bot.config, got[0], got[1])
            if step == "edit_icon":
                ms = _methods(conn, bot.config)
                ms[w["index"]]["icon"] = name
                payments.save_methods(conn, _as_rows(ms))
                bot.wizard = None
                return screen_pay_view(conn, bot.config, w["index"])
            w["data"]["icon"] = name
            return _pay_preview(bot)
    except payments.PaymentError as exc:
        return f"⚠️ {_e(str(exc))}", [[("Отмена", "pm:list:0")]]
    except RuntimeError as exc:
        return f"⚠️ Не удалось скачать файл: {_e(str(exc))}", [[("Отмена", "pm:list:0")]]
    bot.wizard = None
    return screen_pay_list(conn, bot.config)
