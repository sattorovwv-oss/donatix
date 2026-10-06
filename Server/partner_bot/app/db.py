"""Слой доступа к данным (SQLite через aiosqlite).

Все денежные величины — целые числа в дирамах (1 сомони = 100 дирам).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any

import logging

import aiosqlite

from app.config import settings

log = logging.getLogger(__name__)

# ---- статусы заказа ----
ORDER_DELIVERING = "delivering"
ORDER_DELIVERED = "delivered"
ORDER_FAILED = "failed"
ORDER_REFUNDED = "refunded"

#: Эти подписи видит и клиент в истории, и владелец в панели. Держать их
#: на двух языках значит завести параллельный путь ради семи слов —
#: поэтому они на языке клиента, и в панели читаются так же.
ORDER_TITLES = {
    ORDER_DELIVERING: "Дар интиқол",
    ORDER_DELIVERED: "Иҷро шуд",
    ORDER_FAILED: "Санҷида мешавад",
    ORDER_REFUNDED: "Пул баргардонида шуд",
}

#: Ключ значка для каждого статуса — сами значки настраиваются в панели.
ORDER_ICONS = {
    ORDER_DELIVERING: "wait",
    ORDER_DELIVERED: "ok",
    ORDER_FAILED: "search",
    ORDER_REFUNDED: "refund",
}

# ---- статусы пополнения ----
DEP_PENDING = "pending"
DEP_APPROVED = "approved"
DEP_REJECTED = "rejected"

DEP_TITLES = {
    DEP_PENDING: "🔍 Дар санҷиш",
    DEP_APPROVED: "✅ Гузаронида шуд",
    DEP_REJECTED: "❌ Рад шуд",
}

# ---- статусы тикета ----
TICKET_OPEN = "open"
TICKET_CLOSED = "closed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    balance       INTEGER NOT NULL DEFAULT 0,   -- дирамы
    total_deposit INTEGER NOT NULL DEFAULT 0,
    referrer_id   INTEGER,
    ref_earned    INTEGER NOT NULL DEFAULT 0,
    ref_count     INTEGER NOT NULL DEFAULT 0,
    is_banned     INTEGER NOT NULL DEFAULT 0,
    source        TEXT,                          -- код Deep Link, приведшей клиента
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deposits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    method          TEXT NOT NULL,
    receipt_file_id TEXT,
    reference       TEXT,
    status          TEXT NOT NULL,
    reviewed_by     INTEGER,
    pay_chat        INTEGER,        -- где показан экран с реквизитами
    pay_msg         INTEGER,        -- и какое это сообщение
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    product_type      TEXT NOT NULL,        -- 'stars' | 'premium'
    quantity          INTEGER NOT NULL,     -- звёзд или месяцев
    recipient         TEXT NOT NULL,
    price             INTEGER NOT NULL,     -- дирамы, сколько заплатил клиент
    cost              INTEGER NOT NULL DEFAULT 0,  -- дирамы, во сколько обошлось нам
    status            TEXT NOT NULL,
    promo             TEXT,                        -- применённый промокод
    discount          INTEGER NOT NULL DEFAULT 0,  -- дирамы, размер скидки
    fragment_order_id TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    sender_id  INTEGER NOT NULL,
    is_admin   INTEGER NOT NULL,
    text       TEXT,
    file_id    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promocodes (
    code       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'bonus',  -- 'bonus' на баланс | 'discount' скидка
    amount     INTEGER NOT NULL,               -- дирамы, для bonus
    percent    INTEGER NOT NULL DEFAULT 0,     -- проценты, для discount
    max_uses   INTEGER NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_uses (
    code       TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, user_id)
);

CREATE TABLE IF NOT EXISTS adjustments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL,
    amount     INTEGER NOT NULL,        -- дирамы, со знаком: минус = списание
    reason     TEXT,
    created_at TEXT NOT NULL
);

-- Отзывы: один завершённый заказ = один отзыв (UNIQUE на order_id).
-- Поставщик может прислать одно и то же событие дважды — в его
-- документации это сказано прямо. Первичный ключ по event_id и есть
-- защита: повтор не вставится, и обработка второй раз не запустится.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT '',
    order_id   TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Проверять это в коде мало: две кнопки, нажатые подряд, успели бы
-- проскочить обе.
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL UNIQUE,
    user_id     INTEGER NOT NULL,
    rating      INTEGER NOT NULL,          -- 1..5
    text        TEXT,
    status      TEXT NOT NULL,             -- pending | published | deleted
    channel_msg INTEGER,                   -- id сообщения в канале
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Кому уже предлагали оставить отзыв. Без этой отметки повторное нажатие
-- кнопки в панели дёргало бы одних и тех же людей снова и снова.
-- Партнёры: деньги приходят на одну карту, поэтому делим не деньги,
-- а учёт. Доля прибыли считается из заказов, взносы и выплаты пишутся
-- отдельно — тогда «сколько чьё» не зависит от чьей-либо памяти.
CREATE TABLE IF NOT EXISTS partners (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    tg_id      INTEGER,                      -- необязательно
    share      INTEGER NOT NULL DEFAULT 0,   -- доля в процентах
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Движение денег партнёра: со знаком. Плюс — внёс в оборот,
-- минус — забрал себе.
-- Какой товар чей. Прибыль с товара идёт его владельцу; что никому
-- не отдано — делится по долям.
-- Игры, которые владелец открыл к продаже. Каждая игра — отдельный
-- товар: её можно закрепить за партнёром и увидеть в отчётах отдельно.
CREATE TABLE IF NOT EXISTS games (
    category_id TEXT PRIMARY KEY,   -- как называет игру сервис выдачи
    title       TEXT NOT NULL,      -- как показываем клиенту
    field       TEXT NOT NULL DEFAULT 'user_id',   -- поля ID через запятую
    checker     TEXT NOT NULL DEFAULT '',          -- код игры у проверки ID
    emoji       TEXT NOT NULL DEFAULT '',          -- ID премиум-эмодзи
    region      TEXT NOT NULL DEFAULT '',          -- подсказка для поиска ника
    margin      INTEGER NOT NULL DEFAULT 0,        -- своя наценка, % (0 — общая)
    enabled     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Своя цена пакета. Обычно цена считается из себестоимости и наценки,
-- но иногда её нужно поставить руками — ровной суммой или под конкурента.
-- Что владелец поменял в пакете поставщика: цену, название, видимость.
-- Цена -1 означает «своей цены нет, считать по наценке»: строка при этом
-- может остаться ради названия или скрытия.
CREATE TABLE IF NOT EXISTS game_prices (
    category_id TEXT NOT NULL,
    offer_id    TEXT NOT NULL,
    price       INTEGER NOT NULL,      -- дирамы, -1 — своей цены нет
    title       TEXT NOT NULL DEFAULT '',
    hidden      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (category_id, offer_id)
);

CREATE TABLE IF NOT EXISTS product_owners (
    product_type TEXT PRIMARY KEY,
    partner_id   INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partner_moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    note       TEXT,
    admin_id   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_asks (
    order_id   INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,     -- то, что стоит после ?start=
    created_at TEXT NOT NULL
);

-- Один запуск бота по ссылке. Отсюда все три числа: переходы (все строки),
-- уникальные (разные user_id), новые (is_new = 1).
CREATE TABLE IF NOT EXISTS link_hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    is_new     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ──────────────────────────── зачисления от банковского бота ──
-- Каждое банковское уведомление записывается ДО всякой обработки.
-- Два уникальных индекса ниже и есть защита от двойного зачисления:
-- повтор упирается в них и до денег не доходит.
CREATE TABLE IF NOT EXISTS bank_payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,              -- откуда: юзернейм или id бота
    message_id INTEGER NOT NULL,           -- id сообщения в Telegram
    op_code    TEXT,                       -- Kod банка, если он был
    amount     INTEGER NOT NULL,           -- дирамы, фактически зачислено
    sender     TEXT NOT NULL DEFAULT '',   -- отправитель, уже замаскирован
    card_tail  TEXT NOT NULL DEFAULT '',   -- последние 4 знака карты
    bank_time  TEXT NOT NULL DEFAULT '',   -- время из самого уведомления
    seen_at    TEXT NOT NULL,              -- когда его увидел юзербот
    status     TEXT NOT NULL,              -- matched|ambiguous|unknown|failed
    deposit_id INTEGER,                    -- какая заявка закрыта
    note       TEXT NOT NULL DEFAULT '',
    comment    TEXT NOT NULL DEFAULT '',   -- приписка банка: «Сбербанк» и пр.
    body       TEXT NOT NULL DEFAULT ''    -- текст уведомления, без карты
);

-- ────────────────────────────── API для сторонних разработчиков ──
-- Клиент API — обычный клиент бота: тот же users.id, тот же баланс.
-- Отдельной таблицы пользователей нет намеренно: два списка людей
-- с двумя балансами разошлись бы в первый же день.

CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    prefix       TEXT NOT NULL,                 -- видимое начало ключа
    tail         TEXT NOT NULL DEFAULT '',      -- видимый хвост, 4 знака
    key_hash     TEXT NOT NULL,                 -- сам ключ не хранится нигде
    enabled      INTEGER NOT NULL DEFAULT 1,
    revoked_at   TEXT,
    last_used_at TEXT,
    last_ip      TEXT,
    requests     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    key_id     INTEGER,
    user_id    INTEGER,
    method     TEXT NOT NULL,
    path       TEXT NOT NULL,
    status     INTEGER NOT NULL,
    error      TEXT,
    ip         TEXT,
    user_agent TEXT,
    ms         INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Заказ по API — обычный заказ из orders. Здесь только то, чего у
-- обычного заказа нет: публичный номер, чей ключ, ключ идемпотентности
-- и какой статус уже отправлен вебхуком.
CREATE TABLE IF NOT EXISTS api_orders (
    ref        TEXT PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    key_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    customer   TEXT NOT NULL DEFAULT '',
    idem_key   TEXT,
    hook_state TEXT NOT NULL DEFAULT '',
    hook_tries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Просьба разработчика вернуть деньги за выполненный заказ. Именно
-- просьба, а не возврат: решает владелец. Автоматический возврат по
-- слову покупающей стороны — это «заказал, получил, вернул деньги».
CREATE TABLE IF NOT EXISTS api_refunds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ref        TEXT NOT NULL,      -- наш номер заказа, ORD-...
    user_id    INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|denied
    answer     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

-- Как разработчик хочет узнавать о своих заказах. Отдельно от вебхука:
-- вебхук получает его программа, а это — он сам, в Telegram.
CREATE TABLE IF NOT EXISTS api_prefs (
    user_id     INTEGER PRIMARY KEY,
    notify      TEXT NOT NULL DEFAULT 'problems',  -- off | problems | all
    digest      INTEGER NOT NULL DEFAULT 0,        -- сводка раз в сутки
    digest_on   TEXT NOT NULL DEFAULT ''           -- за какой день уже слали
);

-- Пропуск в кабинет по ссылке из бота. Ключ для этого не годится: он
-- умеет тратить деньги, а ссылка живёт в адресной строке, в истории
-- браузера и в пересланном сообщении. Поэтому пропуск — отдельная
-- строка, и она даёт только смотреть.
CREATE TABLE IF NOT EXISTS api_passes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    prefix       TEXT NOT NULL,     -- видимое начало: по нему ищем кандидатов
    token_hash   TEXT NOT NULL,     -- сам пропуск не храним нигде
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS api_transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id          TEXT NOT NULL,
    user_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,     -- deposit | charge | refund | adjust
    amount         INTEGER NOT NULL,  -- со знаком, дирамы
    balance_before INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    order_ref      TEXT,
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

-- Чей это отправитель. Банк в каждом уведомлении называет плательщика,
-- и он у человека не меняется. Один раз связав его с клиентом, дальше
-- узнаём платежи этого клиента без всяких чеков — и не путаем с чужими,
-- даже когда суммы совпали до копейки.
CREATE TABLE IF NOT EXISTS bank_senders (
    sender    TEXT PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    payments  INTEGER NOT NULL DEFAULT 0,
    bound_at  TEXT NOT NULL,
    last_at   TEXT NOT NULL DEFAULT ''
);

-- Резерв ключа идемпотентности. Занимается ДО списания денег: если
-- повторный запрос успеет прийти, пока первый ещё считает, он упрётся
-- в первичный ключ и не создаст второй заказ.
CREATE TABLE IF NOT EXISTS api_idempotency (
    key_id     INTEGER NOT NULL,
    idem_key   TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (key_id, idem_key)
);

CREATE TABLE IF NOT EXISTS api_webhooks (
    user_id    INTEGER PRIMARY KEY,
    url        TEXT NOT NULL DEFAULT '',
    secret     TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_ok_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    fails      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_user    ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_deposits_created ON deposits(created_at);
CREATE INDEX IF NOT EXISTS idx_deposits_user  ON deposits(user_id);
CREATE INDEX IF NOT EXISTS idx_deposits_stat  ON deposits(status);
CREATE INDEX IF NOT EXISTS idx_tickets_user   ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tmsg_ticket    ON ticket_messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_adj_user      ON adjustments(user_id);
CREATE INDEX IF NOT EXISTS idx_adj_created   ON adjustments(created_at);
CREATE INDEX IF NOT EXISTS idx_pmoves_partner ON partner_moves(partner_id);
CREATE INDEX IF NOT EXISTS idx_rev_status    ON reviews(status);
CREATE INDEX IF NOT EXISTS idx_rev_user      ON reviews(user_id);
CREATE INDEX IF NOT EXISTS idx_hits_link     ON link_hits(link_id);
CREATE INDEX IF NOT EXISTS idx_hits_user     ON link_hits(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_msg
    ON bank_payments(source, message_id);
-- Код операции у банка свой и уникальный: одно и то же зачисление могло
-- прийти двумя разными сообщениями, и тогда message_id не спасёт.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_code
    ON bank_payments(op_code) WHERE op_code IS NOT NULL AND op_code != '';
CREATE INDEX IF NOT EXISTS idx_bank_status  ON bank_payments(status);
CREATE INDEX IF NOT EXISTS idx_bsender_user ON bank_senders(user_id);
CREATE INDEX IF NOT EXISTS idx_akeys_user    ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_akeys_prefix  ON api_keys(prefix);
CREATE INDEX IF NOT EXISTS idx_areq_key      ON api_requests(key_id);
CREATE INDEX IF NOT EXISTS idx_areq_created  ON api_requests(created_at);
-- Одна заявка на заказ: повторные просьбы не должны плодить строки,
-- между которыми владельцу потом выбирать.
CREATE UNIQUE INDEX IF NOT EXISTS idx_arefund_ref ON api_refunds(ref);
CREATE INDEX IF NOT EXISTS idx_arefund_status ON api_refunds(status);
CREATE INDEX IF NOT EXISTS idx_pass_prefix  ON api_passes(prefix);
CREATE INDEX IF NOT EXISTS idx_pass_user    ON api_passes(user_id);
CREATE INDEX IF NOT EXISTS idx_atx_user      ON api_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_atx_created   ON api_transactions(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_atx_id ON api_transactions(tx_id);
CREATE INDEX IF NOT EXISTS idx_aord_user     ON api_orders(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_aord_order ON api_orders(order_id);
-- Идемпотентность держится индексом, а не проверкой в коде: два
-- одновременных запроса с одним ключом иначе создали бы два заказа.
CREATE UNIQUE INDEX IF NOT EXISTS idx_aord_idem
    ON api_orders(key_id, idem_key) WHERE idem_key IS NOT NULL;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _from_row(cls, row: aiosqlite.Row):
    return cls(**{f.name: row[f.name] for f in fields(cls)})


@dataclass
class User:
    id: int
    username: str | None
    first_name: str | None
    balance: int
    total_deposit: int
    referrer_id: int | None
    ref_earned: int
    ref_count: int
    is_banned: int
    created_at: str
    source: str | None = None      # код Deep Link, по которой пришёл


#: Товары бота: код -> как называть в отчётах.
PRODUCT_TITLES = {
    "stars": "⭐ Звёзды",
    "premium": "👑 Telegram Premium",
    "steam": "🎮 Steam",
}


#: Названия игр подставляются на лету — их список живёт в базе.
GAME_TITLES: dict[str, str] = {}


def product_title(code: str) -> str:
    if code.startswith("game:"):
        return GAME_TITLES.get(code, "🎮 " + code.split(":", 1)[1])
    return PRODUCT_TITLES.get(code, code)


async def load_game_titles(conn: aiosqlite.Connection) -> None:
    """Подтянуть названия игр в память — отчёты собираются синхронно."""
    GAME_TITLES.clear()
    for game in await list_games(conn):
        GAME_TITLES[game.product_type] = game.title


REVIEW_PENDING = "pending"
REVIEW_PUBLISHED = "published"
REVIEW_DELETED = "deleted"


@dataclass
class Review:
    id: int
    order_id: int
    user_id: int
    rating: int
    text: str | None
    status: str
    channel_msg: int | None
    created_at: str
    updated_at: str

    @property
    def stars(self) -> str:
        return "⭐️" * self.rating


@dataclass
class Game:
    category_id: str
    title: str
    field: str
    region: str
    margin: int
    enabled: int
    created_at: str
    checker: str = ""
    emoji: str = ""

    @property
    def product_type(self) -> str:
        """Игра живёт в заказах как обычный товар."""
        return f"game:{self.category_id}"

    @property
    def field_names(self) -> list[str]:
        """Какие поля спрашивать у клиента.

        Части игр мало одного ID: Magic Chess и Mobile Legends требуют ещё
        и номер сервера. Поэтому в одной колонке лежит список через
        запятую — старые записи с одним полем читаются как прежде.
        """
        names = [part.strip() for part in (self.field or "").split(",")]
        return [name for name in names if name] or ["user_id"]


@dataclass
class Partner:
    id: int
    name: str
    tg_id: int | None
    share: int
    active: int
    created_at: str


@dataclass
class Link:
    id: int
    code: str
    created_at: str


@dataclass
class Deposit:
    id: int
    user_id: int
    amount: int
    method: str
    receipt_file_id: str | None
    reference: str | None
    status: str
    reviewed_by: int | None
    created_at: str
    updated_at: str
    pay_chat: int | None = None
    pay_msg: int | None = None

    @property
    def status_title(self) -> str:
        return DEP_TITLES.get(self.status, self.status)


@dataclass
class Order:
    id: int
    user_id: int
    product_type: str
    quantity: int
    recipient: str
    price: int
    cost: int
    status: str
    fragment_order_id: str | None
    error: str | None
    created_at: str
    updated_at: str
    promo: str | None = None       # применённый промокод
    discount: int = 0              # дирамы, на сколько сбили цену

    @property
    def title(self) -> str:
        if self.product_type == "stars":
            return f"⭐ {self.quantity} ситора"
        if self.product_type == "steam":
            from app import runtime

            return f"🎮 Steam {self.quantity} {runtime.steam_currency()}"
        if self.product_type.startswith("game:"):
            return f"{product_title(self.product_type)} × {self.quantity}"
        return f"👑 Premium {self.quantity} моҳ"

    @property
    def status_title(self) -> str:
        from app.emoji import em

        icon = em(ORDER_ICONS.get(self.status, "receipt"))
        return f"{icon} {ORDER_TITLES.get(self.status, self.status)}"

    @property
    def is_refunded(self) -> bool:
        return self.status == ORDER_REFUNDED

    @property
    def profit(self) -> int:
        """Прибыль по заказу. 0, если себестоимость не была известна."""
        return self.price - self.cost if self.cost else 0


@dataclass
class Ticket:
    id: int
    user_id: int
    subject: str
    status: str
    created_at: str
    updated_at: str


async def connect() -> aiosqlite.Connection:
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(settings.db_file)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # Писать в базу могут разом бот, API и юзербот. При WAL два писателя
    # не встают в очередь сами — второй получает «database is locked».
    # Пятнадцати секунд хватает любой нашей записи, а падение на занятой
    # базе посреди оплаты стоит дороже любого ожидания.
    await conn.execute("PRAGMA busy_timeout=15000")
    # При WAL база по умолчанию ждёт подтверждения диска после каждой
    # записи. На обычном диске это миллисекунды простоя на каждой
    # операции, и они складываются в ту самую очередь. NORMAL при WAL
    # не рвёт базу: самое плохое, что бывает при выключении питания, —
    # потеря последних секунд, а не порча файла.
    await conn.execute("PRAGMA synchronous=NORMAL")
    return conn


async def release(conn: aiosqlite.Connection) -> bool:
    """Снять транзакцию, которую кто-то открыл и не закрыл.

    Первая INSERT/UPDATE/DELETE открывает транзакцию и берёт замок на
    запись. Если после неё функция вернулась, упала или бросила
    исключение раньше commit — замок остаётся висеть на соединении до
    следующего commit. Всё это время остальные соединения ждут свои
    пятнадцать секунд и получают «database is locked».

    Зовётся на выходе из каждого обработчика бота, каждого запроса API
    и каждого уведомления юзербота. True — было что снимать: это
    ошибка в коде, и о ней стоит написать в журнал.
    """
    if not conn.in_transaction:
        return False
    await conn.rollback()
    return True


async def _undo(conn: aiosqlite.Connection) -> None:
    """Откатить упавшую вставку, чтобы не унести замок с собой."""
    if conn.in_transaction:
        await conn.rollback()


async def compact(conn: aiosqlite.Connection) -> None:
    """Свернуть журнал WAL в базу и обнулить его.

    Тихо: если кто-то держит базу (второй процесс ещё не умер), журнал
    просто останется как был — это не повод не запускаться.
    """
    try:
        async with conn.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cur:
            busy, pages, moved = await cur.fetchone()
        if busy:
            log.info("Журнал базы занят другим процессом, свернём в другой раз")
        elif pages:
            log.info("Журнал базы свёрнут: %d страниц", moved)
    except Exception as exc:  # noqa: BLE001 — уборка не важнее запуска
        log.warning("Журнал базы свернуть не вышло: %s", exc)


#: Колонки, добавленные после первого выпуска. Ключ — таблица.
MIGRATIONS: dict[str, dict[str, str]] = {
    "orders": {
        "cost": "INTEGER NOT NULL DEFAULT 0",
        "promo": "TEXT",
        "discount": "INTEGER NOT NULL DEFAULT 0",
    },
    "promocodes": {
        "kind": "TEXT NOT NULL DEFAULT 'bonus'",
        "percent": "INTEGER NOT NULL DEFAULT 0",
    },
    # Где висит экран с реквизитами. Когда деньги придут, номер карты
    # надо убрать с глаз — платить по нему больше нечего.
    "deposits": {
        "reference": "TEXT",
        "pay_chat": "INTEGER",
        "pay_msg": "INTEGER",
    },
    "users": {"source": "TEXT"},
    # Приписка банка. У переводов из России там написано «Сбербанк» —
    # по ней видно, откуда деньги, ещё до всякого сопоставления.
    "bank_payments": {"comment": "TEXT NOT NULL DEFAULT ''"},
    # Код этой же игры у сервиса проверки ID — он свой, не как у
    # поставщика выдачи.
    "games": {
        "checker": "TEXT NOT NULL DEFAULT ''",
        # ID премиум-эмодзи для кнопки этой игры.
        "emoji": "TEXT NOT NULL DEFAULT ''",
    },
    "game_prices": {
        "title": "TEXT NOT NULL DEFAULT ''",
        "hidden": "INTEGER NOT NULL DEFAULT 0",
    },
}


async def init(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await _migrate(conn)
    await conn.commit()


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Дописать недостающие колонки в уже существующую базу.

    Без этого обновление бота на работающем сервере падало бы: таблица
    создана по старой схеме, а код ждёт новых полей.
    """
    for table, columns in MIGRATIONS.items():
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                log.info("База: в таблицу %s добавлена колонка %s", table, name)


# ------------------------------------------------------------------ users


async def upsert_user(
    conn: aiosqlite.Connection,
    user_id: int,
    username: str | None,
    first_name: str | None,
    referrer_id: int | None = None,
) -> bool:
    """Создать или обновить пользователя. True — если пользователь новый."""
    async with conn.execute(
        "SELECT username, first_name FROM users WHERE id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()

    if row is not None:
        # Эта проверка идёт на каждое нажатие каждой кнопки — самая
        # частая запись во всей системе. А менять почти всегда нечего:
        # имя и юзернейм те же, что секунду назад. Писать вхолостую
        # нельзя: SQLite пускает к записи по одному, и пока тысяча
        # человек переписывает свои же имена, юзербот не может
        # зачислить оплату и получает «database is locked».
        if row["username"] == username and row["first_name"] == first_name:
            return False
        await conn.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE id = ?",
            (username, first_name, user_id),
        )
        await conn.commit()
        return False

    # На себя рефералку не начисляем, и на несуществующего пригласителя тоже.
    if referrer_id == user_id:
        referrer_id = None
    if referrer_id is not None:
        async with conn.execute("SELECT 1 FROM users WHERE id = ?", (referrer_id,)) as cur:
            if await cur.fetchone() is None:
                referrer_id = None

    await conn.execute(
        """INSERT INTO users (id, username, first_name, referrer_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, username, first_name, referrer_id, _now()),
    )
    if referrer_id is not None:
        await conn.execute(
            "UPDATE users SET ref_count = ref_count + 1 WHERE id = ?", (referrer_id,)
        )
    await conn.commit()
    return True


async def get_user(conn: aiosqlite.Connection, user_id: int) -> User | None:
    async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(User, row) if row else None


async def set_banned(conn: aiosqlite.Connection, user_id: int, banned: bool) -> None:
    await conn.execute("UPDATE users SET is_banned = ? WHERE id = ?", (int(banned), user_id))
    await conn.commit()


async def find_user(conn: aiosqlite.Connection, query: str) -> User | None:
    """Найти клиента по ID или юзернейму — под рукой бывает любое из двух."""
    query = query.strip().lstrip("@")
    if query.isdigit():
        found = await get_user(conn, int(query))
        if found:
            return found
    async with conn.execute(
        "SELECT * FROM users WHERE lower(username) = lower(?) LIMIT 1", (query,)
    ) as cur:
        row = await cur.fetchone()
    return _from_row(User, row) if row else None


async def search_users(conn: aiosqlite.Connection, query: str, limit: int = 10) -> list[User]:
    """Клиенты по ID, @нику или имени — сначала точные совпадения, потом похожие."""
    q = (query or "").strip().lstrip("@")
    if not q:
        return []
    found: list[User] = []
    if q.isdigit():
        user = await get_user(conn, int(q))
        if user:
            found.append(user)
    like = f"%{q.lower()}%"
    # SQLite lower() не знает кириллицу — «алиджон» не нашёл бы «Алиджон»
    await conn.create_function("ulower", 1, lambda v: (v or "").lower(), deterministic=True)
    async with conn.execute(
        """SELECT * FROM users
           WHERE ulower(username) LIKE ? OR ulower(first_name) LIKE ?
                 OR CAST(id AS TEXT) LIKE ?
           ORDER BY (ulower(username) = ?) DESC, total_deposit DESC LIMIT ?""",
        (like, like, f"{q}%", q.lower(), limit),
    ) as cur:
        for row in await cur.fetchall():
            user = _from_row(User, row)
            if all(u.id != user.id for u in found):
                found.append(user)
    return found[:limit]


async def user_timeline(conn: aiosqlite.Connection, user_id: int, limit: int = 10,
                        offset: int = 0) -> tuple[list[dict], int]:
    """Всё по клиенту одной лентой, новое сверху: пополнения, покупки, ручные правки баланса."""
    sql = """
        SELECT 'deposit' AS kind, id, amount AS amount, method AS what, '' AS extra, status, created_at
          FROM deposits WHERE user_id = :u
        UNION ALL
        SELECT 'order', id, price, product_type, recipient || '|' || quantity, status, created_at
          FROM orders WHERE user_id = :u
        UNION ALL
        SELECT 'adjust', id, amount, coalesce(reason, ''), '', '', created_at
          FROM adjustments WHERE user_id = :u
    """
    async with conn.execute(f"SELECT COUNT(*) FROM ({sql})", {"u": user_id}) as cur:
        total = (await cur.fetchone())[0]
    async with conn.execute(f"{sql} ORDER BY created_at DESC, id DESC LIMIT :l OFFSET :o",
                            {"u": user_id, "l": limit, "o": offset}) as cur:
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in await cur.fetchall()]
    return rows, total


async def user_spent(conn: aiosqlite.Connection, user_id: int) -> tuple[int, str]:
    """Сколько потратил на выполненные заказы и когда была последняя покупка."""
    async with conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN status = ? THEN price END), 0), MAX(created_at) FROM orders "
        "WHERE user_id = ?", (ORDER_DELIVERED, user_id)) as cur:
        spent, last = await cur.fetchone()
    return int(spent or 0), last or ""


@dataclass
class Adjustment:
    id: int
    user_id: int
    admin_id: int
    amount: int
    reason: str | None
    created_at: str


async def add_adjustment(
    conn: aiosqlite.Connection, *, user_id: int, admin_id: int,
    amount: int, reason: str = "",
) -> None:
    """Записать ручную правку баланса.

    Без записи такие деньги появлялись бы из ниоткуда, и сойти отчёты
    уже не могли бы.
    """
    await conn.execute(
        """INSERT INTO adjustments (user_id, admin_id, amount, reason, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, admin_id, amount, reason or None, _now()),
    )
    await conn.commit()


async def list_adjustments(
    conn: aiosqlite.Connection, *, user_id: int | None = None, limit: int = 10
) -> list[Adjustment]:
    sql = "SELECT * FROM adjustments"
    params: list[Any] = []
    if user_id is not None:
        sql += " WHERE user_id = ?"
        params.append(user_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        return [_from_row(Adjustment, row) for row in await cur.fetchall()]


async def adjustments_total(
    conn: aiosqlite.Connection, since: str, until: str
) -> tuple[int, int]:
    """Сколько начислено и списано вручную за период."""
    async with conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS added,
                  COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS taken
           FROM adjustments WHERE created_at >= ? AND created_at < ?""",
        (since, until),
    ) as cur:
        row = await cur.fetchone()
    return row["added"], row["taken"]


async def all_user_ids(conn: aiosqlite.Connection) -> list[int]:
    async with conn.execute("SELECT id FROM users WHERE is_banned = 0") as cur:
        return [row["id"] for row in await cur.fetchall()]


#: Готовый топ и когда он посчитан. Это публичная кнопка: жмут её все,
#: а запрос складывает все выданные заказы разом — на сотне тысяч это
#: десятая доля секунды. Соединение с базой одно на весь бот, поэтому
#: десяток нажатий в секунду остановил бы бота для всех сразу. Рейтинг
#: за пять минут не портится, а нагрузка от него падает до нуля.
_top_cache: dict[str, tuple[float, list]] = {}
TOP_TTL = 5 * 60


def forget_top() -> None:
    _top_cache.clear()


async def top_clients(
    conn: aiosqlite.Connection, limit: int = 10, by: str = "purchases",
    cached: bool = True,
) -> list[tuple[User, int]]:
    """Топ клиентов: пары (клиент, сумма) по убыванию суммы.

    by="purchases" — сумма выданных заказов: отменённые и возвращённые
    в неё не попадают, поэтому рейтинг показывает реальных покупателей.
    by="deposits"  — сумма пополнений за всё время.

    cached=False — посчитать заново, не заглядывая в память.
    """
    import time as _time

    key = f"{by}:{limit}"
    if cached:
        ready = _top_cache.get(key)
        if ready and _time.time() - ready[0] < TOP_TTL:
            return ready[1]

    if by == "deposits":
        query = """SELECT *, total_deposit AS amount FROM users
                   WHERE total_deposit > 0
                   ORDER BY amount DESC, id LIMIT ?"""
        params: tuple = (limit,)
    else:
        query = """SELECT u.*, SUM(o.price) AS amount
                   FROM users u JOIN orders o ON o.user_id = u.id
                   WHERE o.status = ?
                   GROUP BY u.id
                   ORDER BY amount DESC, u.id LIMIT ?"""
        params = (ORDER_DELIVERED, limit)
    async with conn.execute(query, params) as cur:
        rows = [(_from_row(User, row), row["amount"])
                for row in await cur.fetchall()]
    _top_cache[key] = (_time.time(), rows)
    return rows


# -------------------------------------------------------------- партнёры


async def create_partner(
    conn: aiosqlite.Connection, name: str, share: int = 0, tg_id: int | None = None,
) -> Partner:
    cur = await conn.execute(
        "INSERT INTO partners (name, tg_id, share, created_at) VALUES (?, ?, ?, ?)",
        (name, tg_id, share, _now()),
    )
    await conn.commit()
    partner = await get_partner(conn, cur.lastrowid)
    assert partner is not None
    return partner


async def get_partner(conn: aiosqlite.Connection, partner_id: int) -> Partner | None:
    async with conn.execute("SELECT * FROM partners WHERE id = ?", (partner_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(Partner, row) if row else None


async def list_partners(conn: aiosqlite.Connection) -> list[Partner]:
    async with conn.execute(
        "SELECT * FROM partners WHERE active = 1 ORDER BY id"
    ) as cur:
        return [_from_row(Partner, row) for row in await cur.fetchall()]


async def update_partner(conn: aiosqlite.Connection, partner_id: int, **fields_) -> None:
    if not fields_:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields_)
    await conn.execute(
        f"UPDATE partners SET {assignments} WHERE id = ?",
        (*fields_.values(), partner_id),
    )
    await conn.commit()


async def add_partner_move(
    conn: aiosqlite.Connection, *, partner_id: int, amount: int,
    admin_id: int, note: str | None = None,
) -> None:
    """amount со знаком: плюс — внёс в оборот, минус — забрал себе."""
    await conn.execute(
        """INSERT INTO partner_moves (partner_id, amount, note, admin_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (partner_id, amount, note, admin_id, _now()),
    )
    await conn.commit()


async def partner_moves(
    conn: aiosqlite.Connection, partner_id: int, limit: int = 10,
) -> list[aiosqlite.Row]:
    async with conn.execute(
        "SELECT * FROM partner_moves WHERE partner_id = ? ORDER BY id DESC LIMIT ?",
        (partner_id, limit),
    ) as cur:
        return list(await cur.fetchall())


async def partner_totals(conn: aiosqlite.Connection, partner_id: int) -> dict[str, int]:
    """Сколько партнёр внёс и сколько забрал — обе суммы положительные."""
    async with conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0)  AS put_in,
                  COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS took_out
           FROM partner_moves WHERE partner_id = ?""",
        (partner_id,),
    ) as cur:
        row = await cur.fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


async def add_game(
    conn: aiosqlite.Connection, *, category_id: str, title: str,
    field: str = "user_id", region: str = "",
) -> Game:
    await conn.execute(
        """INSERT INTO games (category_id, title, field, region, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(category_id) DO UPDATE SET title = excluded.title,
                                                  field = excluded.field,
                                                  region = excluded.region""",
        (category_id, title, field, region, _now()),
    )
    await conn.commit()
    game = await get_game(conn, category_id)
    assert game is not None
    return game


async def get_game(conn: aiosqlite.Connection, category_id: str) -> Game | None:
    async with conn.execute(
        "SELECT * FROM games WHERE category_id = ?", (category_id,)
    ) as cur:
        row = await cur.fetchone()
    return _from_row(Game, row) if row else None


async def list_games(
    conn: aiosqlite.Connection, only_enabled: bool = False
) -> list[Game]:
    sql = "SELECT * FROM games"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY title"
    async with conn.execute(sql) as cur:
        return [_from_row(Game, row) for row in await cur.fetchall()]


async def update_game(conn: aiosqlite.Connection, category_id: str, **fields_) -> None:
    if not fields_:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields_)
    await conn.execute(
        f"UPDATE games SET {assignments} WHERE category_id = ?",
        (*fields_.values(), category_id),
    )
    await conn.commit()


async def move_game(
    conn: aiosqlite.Connection, old_code: str, new_code: str, *, field: str = "",
) -> None:
    """Переставить игру на другой код категории.

    Настройки привязаны к коду: наценка, свои цены пакетов, владелец-
    партнёр и записанные заказы. Поэтому переносим их вместе с игрой, а
    не заводим игру заново.
    """
    updates = {"category_id": new_code}
    if field:
        updates["field"] = field
    assignments = ", ".join(f"{key} = ?" for key in updates)
    await conn.execute(
        f"UPDATE games SET {assignments} WHERE category_id = ?",
        (*updates.values(), old_code),
    )
    await conn.execute(
        "UPDATE game_prices SET category_id = ? WHERE category_id = ?",
        (new_code, old_code),
    )
    await conn.execute(
        "UPDATE product_owners SET product_type = ? WHERE product_type = ?",
        (f"game:{new_code}", f"game:{old_code}"),
    )
    await conn.commit()


async def delete_game(conn: aiosqlite.Connection, category_id: str) -> None:
    await conn.execute("DELETE FROM games WHERE category_id = ?", (category_id,))
    await conn.execute(
        "DELETE FROM game_prices WHERE category_id = ?", (category_id,)
    )
    await conn.execute(
        "DELETE FROM product_owners WHERE product_type = ?", (f"game:{category_id}",)
    )
    await conn.commit()


#: Цена, которой нет: считать по наценке. Отдельной строкой, потому что
#: строка настроек может жить ради названия или скрытия.
NO_PRICE = -1


async def _offer_row(
    conn: aiosqlite.Connection, category_id: str, offer_id: str, **fields_,
) -> None:
    """Записать настройку пакета, не затирая остальные."""
    await conn.execute(
        """INSERT OR IGNORE INTO game_prices
               (category_id, offer_id, price, created_at)
           VALUES (?, ?, ?, ?)""",
        (category_id, offer_id, NO_PRICE, _now()),
    )
    assignments = ", ".join(f"{key} = ?" for key in fields_)
    await conn.execute(
        f"UPDATE game_prices SET {assignments} "
        "WHERE category_id = ? AND offer_id = ?",
        (*fields_.values(), category_id, offer_id),
    )
    # Пустую настройку не держим: иначе таблица копит строки ни о чём.
    await conn.execute(
        """DELETE FROM game_prices
           WHERE category_id = ? AND offer_id = ?
             AND price = ? AND title = '' AND hidden = 0""",
        (category_id, offer_id, NO_PRICE),
    )
    await conn.commit()


async def set_game_price(
    conn: aiosqlite.Connection, category_id: str, offer_id: str, price: int | None,
) -> None:
    """Задать свою цену пакета. None — вернуть расчёт по наценке."""
    await _offer_row(conn, category_id, offer_id,
                     price=NO_PRICE if price is None else price)


async def set_game_offer_title(
    conn: aiosqlite.Connection, category_id: str, offer_id: str, title: str,
) -> None:
    """Своё название пакета. Пусто — вернуть название поставщика."""
    await _offer_row(conn, category_id, offer_id, title=title.strip())


async def set_game_offer_hidden(
    conn: aiosqlite.Connection, category_id: str, offer_id: str, hidden: bool,
) -> None:
    """Убрать пакет из продажи или вернуть обратно."""
    await _offer_row(conn, category_id, offer_id, hidden=int(hidden))


async def game_offers_setup(
    conn: aiosqlite.Connection, category_id: str,
) -> dict[str, dict]:
    """Всё, что владелец поменял в пакетах этой игры."""
    async with conn.execute(
        "SELECT * FROM game_prices WHERE category_id = ?", (category_id,)
    ) as cur:
        return {
            row["offer_id"]: {
                "price": None if row["price"] == NO_PRICE else row["price"],
                "title": row["title"] or "",
                "hidden": bool(row["hidden"]),
            }
            for row in await cur.fetchall()
        }


async def game_prices(conn: aiosqlite.Connection, category_id: str) -> dict[str, int]:
    """Свои цены пакетов этой игры: offer_id -> дирамы."""
    async with conn.execute(
        "SELECT offer_id, price FROM game_prices "
        "WHERE category_id = ? AND price >= 0",
        (category_id,),
    ) as cur:
        return {row["offer_id"]: row["price"] for row in await cur.fetchall()}


async def set_product_owner(
    conn: aiosqlite.Connection, product_type: str, partner_id: int | None,
) -> None:
    """Отдать товар партнёру. None — снять владельца."""
    if partner_id is None:
        await conn.execute(
            "DELETE FROM product_owners WHERE product_type = ?", (product_type,)
        )
    else:
        await conn.execute(
            """INSERT INTO product_owners (product_type, partner_id, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(product_type) DO UPDATE SET partner_id = excluded.partner_id""",
            (product_type, partner_id, _now()),
        )
    await conn.commit()


async def product_owners(conn: aiosqlite.Connection) -> dict[str, int]:
    async with conn.execute("SELECT * FROM product_owners") as cur:
        return {row["product_type"]: row["partner_id"] for row in await cur.fetchall()}


async def sales_by_product(
    conn: aiosqlite.Connection, since: str | None = None, until: str | None = None,
) -> list[dict[str, Any]]:
    """Что продано по каждому товару: штук, на сколько, во что обошлось."""
    sql = """SELECT product_type,
                    COUNT(*)                  AS orders,
                    COALESCE(SUM(quantity),0) AS quantity,
                    COALESCE(SUM(price), 0)   AS revenue,
                    COALESCE(SUM(cost), 0)    AS cost
             FROM orders WHERE status = ?"""
    params: list = [ORDER_DELIVERED]
    if since and until:
        sql += " AND created_at >= ? AND created_at < ?"
        params += [since, until]
    sql += " GROUP BY product_type ORDER BY revenue DESC"

    async with conn.execute(sql, params) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    for row in rows:
        row["profit"] = row["revenue"] - row["cost"]
        row["title"] = product_title(row["product_type"])
    return rows


async def deposits_total(conn: aiosqlite.Connection) -> dict[str, int]:
    """Сколько денег пришло на реквизиты за всё время."""
    async with conn.execute(
        """SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total
           FROM deposits WHERE status = ?""",
        (DEP_APPROVED,),
    ) as cur:
        row = await cur.fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


async def total_profit(conn: aiosqlite.Connection) -> dict[str, int]:
    """Выручка, себестоимость и прибыль за всё время по выданным заказам."""
    async with conn.execute(
        """SELECT COALESCE(SUM(price), 0) AS revenue,
                  COALESCE(SUM(cost), 0)  AS cost,
                  COUNT(*)                AS orders
           FROM orders WHERE status = ?""",
        (ORDER_DELIVERED,),
    ) as cur:
        row = await cur.fetchone()
    data = {key: (row[key] or 0) for key in row.keys()}
    data["profit"] = data["revenue"] - data["cost"]
    return data


# ---------------------------------------------------------------- отзывы


async def create_review(
    conn: aiosqlite.Connection, *, order_id: int, user_id: int, rating: int,
) -> Review | None:
    """Завести отзыв на заказ. None — если отзыв на него уже есть."""
    now = _now()
    try:
        cur = await conn.execute(
            """INSERT INTO reviews (order_id, user_id, rating, status,
                                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, user_id, rating, REVIEW_PENDING, now, now),
        )
    except sqlite3.IntegrityError:
        # Упавшая вставка уже взяла замок на запись — без отката он
        # останется висеть до следующего commit на этом соединении.
        await _undo(conn)
        return None
    await conn.commit()
    return await get_review(conn, cur.lastrowid)


async def get_review(conn: aiosqlite.Connection, review_id: int) -> Review | None:
    async with conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(Review, row) if row else None


async def review_of_order(conn: aiosqlite.Connection, order_id: int) -> Review | None:
    async with conn.execute(
        "SELECT * FROM reviews WHERE order_id = ?", (order_id,)
    ) as cur:
        row = await cur.fetchone()
    return _from_row(Review, row) if row else None


async def set_review_text(conn: aiosqlite.Connection, review_id: int, text: str) -> None:
    await conn.execute(
        "UPDATE reviews SET text = ?, updated_at = ? WHERE id = ?",
        (text, _now(), review_id),
    )
    await conn.commit()


async def moderate_review(
    conn: aiosqlite.Connection, review_id: int, new: str, channel_msg: int | None = None,
) -> bool:
    """Опубликовать или удалить отзыв. False — если его уже разобрали.

    Условие `status = pending` не даёт двум нажатиям подряд опубликовать
    отзыв дважды.
    """
    cur = await conn.execute(
        """UPDATE reviews SET status = ?, channel_msg = ?, updated_at = ?
           WHERE id = ? AND status = ?""",
        (new, channel_msg, _now(), review_id, REVIEW_PENDING),
    )
    await conn.commit()
    return cur.rowcount > 0


async def list_reviews(
    conn: aiosqlite.Connection, status: str | None = None, limit: int = 20,
) -> list[Review]:
    sql = "SELECT * FROM reviews"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        return [_from_row(Review, row) for row in await cur.fetchall()]


async def review_targets(
    conn: aiosqlite.Connection, limit: int = 500,
) -> list[Order]:
    """Кого ещё можно попросить об отзыве.

    По одному заказу на человека — самому свежему выданному. Пропускаем тех,
    кто уже оставил отзыв, кого уже спрашивали и кто в бане: рассылка не
    должна выглядеть навязчивой.
    """
    async with conn.execute(
        """SELECT o.* FROM orders o
           JOIN users u ON u.id = o.user_id
           WHERE o.status = ?
             AND u.is_banned = 0
             AND o.id NOT IN (SELECT order_id FROM reviews)
             AND o.id NOT IN (SELECT order_id FROM review_asks)
             AND o.id = (SELECT MAX(id) FROM orders x
                         WHERE x.user_id = o.user_id AND x.status = ?)
           ORDER BY o.id DESC LIMIT ?""",
        (ORDER_DELIVERED, ORDER_DELIVERED, limit),
    ) as cur:
        return [_from_row(Order, row) for row in await cur.fetchall()]


async def mark_review_asked(
    conn: aiosqlite.Connection, order_id: int, user_id: int,
) -> None:
    await conn.execute(
        """INSERT OR IGNORE INTO review_asks (order_id, user_id, created_at)
           VALUES (?, ?, ?)""",
        (order_id, user_id, _now()),
    )
    await conn.commit()


async def review_stats(conn: aiosqlite.Connection) -> dict[str, int]:
    async with conn.execute(
        """SELECT COUNT(*)                        AS total,
                  SUM(status = ?)                 AS pending,
                  SUM(status = ?)                 AS published,
                  COALESCE(SUM(rating), 0)        AS rating_sum
           FROM reviews WHERE status != ?""",
        (REVIEW_PENDING, REVIEW_PUBLISHED, REVIEW_DELETED),
    ) as cur:
        row = await cur.fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


# ------------------------------------------------------------ Deep Links


async def create_link(conn: aiosqlite.Connection, code: str) -> Link | None:
    """Завести рекламную ссылку. None — если такая уже есть."""
    try:
        cur = await conn.execute(
            "INSERT INTO links (code, created_at) VALUES (?, ?)", (code, _now()),
        )
    except sqlite3.IntegrityError:
        await _undo(conn)
        return None
    await conn.commit()
    return await get_link(conn, cur.lastrowid)


async def get_link(conn: aiosqlite.Connection, link_id: int) -> Link | None:
    async with conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(Link, row) if row else None


async def get_link_by_code(conn: aiosqlite.Connection, code: str) -> Link | None:
    async with conn.execute("SELECT * FROM links WHERE code = ?", (code,)) as cur:
        row = await cur.fetchone()
    return _from_row(Link, row) if row else None


async def delete_link(conn: aiosqlite.Connection, link_id: int) -> bool:
    """Убрать ссылку вместе с её переходами. Метка source у клиентов остаётся."""
    cur = await conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    await conn.execute("DELETE FROM link_hits WHERE link_id = ?", (link_id,))
    await conn.commit()
    return cur.rowcount > 0


async def record_link_hit(
    conn: aiosqlite.Connection, code: str, user_id: int, is_new: bool,
) -> bool:
    """Отметить запуск бота по ссылке.

    Считаем только заведённые в панели коды: чужой ?start=что-угодно не должен
    плодить ссылки. Метку source ставим один раз — засчитываем первый источник,
    иначе последняя реклама воровала бы себе чужого клиента.
    """
    link = await get_link_by_code(conn, code)
    if link is None:
        return False
    await conn.execute(
        "INSERT INTO link_hits (link_id, user_id, is_new, created_at) VALUES (?, ?, ?, ?)",
        (link.id, user_id, int(is_new), _now()),
    )
    await conn.execute(
        "UPDATE users SET source = ? WHERE id = ? AND (source IS NULL OR source = '')",
        (code, user_id),
    )
    await conn.commit()
    return True


async def link_stats(conn: aiosqlite.Connection, link_id: int) -> dict[str, int]:
    """Переходы, уникальные, новые, а также покупатели и выручка с этой ссылки."""
    async with conn.execute(
        """SELECT COUNT(*)                 AS hits,
                  COUNT(DISTINCT user_id)  AS people,
                  COALESCE(SUM(is_new), 0) AS fresh
           FROM link_hits WHERE link_id = ?""",
        (link_id,),
    ) as cur:
        row = await cur.fetchone()
    stats = {key: (row[key] or 0) for key in row.keys()}

    async with conn.execute(
        """SELECT COUNT(DISTINCT o.user_id)   AS buyers,
                  COALESCE(SUM(o.price), 0)   AS revenue
           FROM orders o
           JOIN users u ON u.id = o.user_id
           JOIN links l ON l.code = u.source
           WHERE l.id = ? AND o.status = ?""",
        (link_id, ORDER_DELIVERED),
    ) as cur:
        row = await cur.fetchone()
    stats.update({key: (row[key] or 0) for key in row.keys()})
    return stats


async def list_links(conn: aiosqlite.Connection) -> list[tuple[Link, dict[str, int]]]:
    """Все ссылки, свежие сверху, каждая со своей статистикой."""
    async with conn.execute("SELECT * FROM links ORDER BY id DESC") as cur:
        links = [_from_row(Link, row) for row in await cur.fetchall()]
    return [(link, await link_stats(conn, link.id)) for link in links]


# ---------------------------------------------------------------- деньги


async def import_balance(
    conn: aiosqlite.Connection, user_id: int, amount: int, username: str = "",
) -> bool:
    """Перенести баланс со старого бота. True — клиента завели заново.

    Баланс именно УСТАНАВЛИВАЕТСЯ, а не добавляется: перенос делают
    один раз, но запустить его дважды легко, и сложение удвоило бы
    людям деньги. Повторный перенос того же списка теперь ничего не
    меняет.

    Клиента заводим, даже если он ещё не писал боту: он нажмёт «старт»
    и сразу увидит свой баланс, а не ноль.
    """
    async with conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)) as cur:
        fresh = await cur.fetchone() is None

    if fresh:
        await conn.execute(
            """INSERT INTO users (id, username, first_name, balance, created_at)
               VALUES (?, ?, NULL, ?, ?)""",
            (user_id, username or None, amount, _now()),
        )
    elif username:
        # Имя из выгрузки лучше пустого, но живое из Telegram важнее:
        # его бот обновляет сам при каждом «старте».
        await conn.execute(
            """UPDATE users SET balance = ?,
                   username = COALESCE(username, ?) WHERE id = ?""",
            (amount, username, user_id),
        )
    else:
        await conn.execute(
            "UPDATE users SET balance = ? WHERE id = ?", (amount, user_id)
        )
    await conn.commit()
    return fresh


async def charge(conn: aiosqlite.Connection, user_id: int, amount: int) -> bool:
    """Списать amount с баланса. False — если денег не хватило.

    Условие `balance >= ?` внутри UPDATE делает проверку и списание одной
    операцией, поэтому два одновременных заказа не уведут баланс в минус.
    """
    cur = await conn.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    await conn.commit()
    return cur.rowcount > 0


async def credit(
    conn: aiosqlite.Connection, user_id: int, amount: int, *, as_deposit: bool = False
) -> None:
    """Начислить amount на баланс. as_deposit — учесть в общем депозите."""
    if as_deposit:
        await conn.execute(
            "UPDATE users SET balance = balance + ?, total_deposit = total_deposit + ? WHERE id = ?",
            (amount, amount, user_id),
        )
    else:
        await conn.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id)
        )
    await conn.commit()


async def add_ref_earning(conn: aiosqlite.Connection, user_id: int, amount: int) -> None:
    await conn.execute(
        """UPDATE users SET balance = balance + ?, ref_earned = ref_earned + ?
           WHERE id = ?""",
        (amount, amount, user_id),
    )
    await conn.commit()


# ------------------------------------------------------------- пополнения


async def create_deposit(
    conn: aiosqlite.Connection, *, user_id: int, amount: int, method: str,
    receipt_file_id: str, reference: str | None = None,
) -> Deposit:
    now = _now()
    cur = await conn.execute(
        """INSERT INTO deposits (user_id, amount, method, receipt_file_id,
                                 reference, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, amount, method, receipt_file_id, reference, DEP_PENDING, now, now),
    )
    await conn.commit()
    deposit = await get_deposit(conn, cur.lastrowid)
    assert deposit is not None
    return deposit


async def open_deposit_of(
    conn: aiosqlite.Connection, user_id: int
) -> Deposit | None:
    """Незавершённая заявка клиента, если она есть.

    Одна заявка на человека — не придирка, а условие, на котором держится
    весь автоматический приём. Узнаём платёж по сумме с копейками: две
    открытые заявки одного человека дают две суммы, и пришедшие деньги
    подходят к обеим. Выбрать наугад нельзя, и платёж уходит владельцу
    разбирать руками — ровно то, от чего мы уходили.
    """
    async with conn.execute(
        "SELECT * FROM deposits WHERE user_id = ? AND status = ? "
        "ORDER BY id DESC LIMIT 1",
        (user_id, DEP_PENDING),
    ) as cur:
        row = await cur.fetchone()
    return _from_row(Deposit, row) if row else None


async def cancel_deposit(
    conn: aiosqlite.Connection, deposit_id: int, user_id: int
) -> bool:
    """Отменить свою заявку. False — она уже закрыта кем-то другим.

    Проверяем владельца прямо в UPDATE: чужой номер заявки не должен
    закрывать её, даже если его подобрали.
    """
    cur = await conn.execute(
        "UPDATE deposits SET status = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND status = ?",
        (DEP_REJECTED, _now(), deposit_id, user_id, DEP_PENDING),
    )
    await conn.commit()
    return bool(cur.rowcount)


async def receipt_seen(
    conn: aiosqlite.Connection, user_id: int, file_id: str
) -> bool:
    """Присылал ли этот человек такой чек раньше.

    Один и тот же чек шлют по многу раз — иногда от беспокойства, иногда
    чтобы получить второе зачисление за один перевод. Сравниваем по
    отпечатку файла у Telegram: пересланная картинка сохраняет его.
    """
    if not file_id:
        return False
    async with conn.execute(
        "SELECT 1 FROM deposits WHERE user_id = ? AND receipt_file_id = ? "
        "LIMIT 1",
        (user_id, file_id),
    ) as cur:
        return await cur.fetchone() is not None


async def set_deposit_reference(
    conn: aiosqlite.Connection, deposit_id: int, reference: str,
) -> None:
    await conn.execute(
        "UPDATE deposits SET reference = ?, updated_at = ? WHERE id = ?",
        (reference, _now(), deposit_id),
    )
    await conn.commit()


async def get_deposit(conn: aiosqlite.Connection, deposit_id: int) -> Deposit | None:
    async with conn.execute("SELECT * FROM deposits WHERE id = ?", (deposit_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(Deposit, row) if row else None


async def list_deposits(
    conn: aiosqlite.Connection, *, user_id: int | None = None, status: str | None = None,
    limit: int = 15,
) -> list[Deposit]:
    sql = "SELECT * FROM deposits WHERE 1 = 1"
    params: list[Any] = []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        return [_from_row(Deposit, row) for row in await cur.fetchall()]


async def resolve_deposit(
    conn: aiosqlite.Connection, deposit_id: int, *, approved: bool, admin_id: int
) -> bool:
    """Перевести пополнение из pending в approved/rejected.

    False — если кто-то уже обработал заявку (защита от двойного зачисления).
    """
    cur = await conn.execute(
        """UPDATE deposits SET status = ?, reviewed_by = ?, updated_at = ?
           WHERE id = ? AND status = ?""",
        (DEP_APPROVED if approved else DEP_REJECTED, admin_id, _now(), deposit_id, DEP_PENDING),
    )
    await conn.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------- заказы


async def create_order(
    conn: aiosqlite.Connection, *, user_id: int, product_type: str, quantity: int,
    recipient: str, price: int, cost: int = 0,
    promo: str | None = None, discount: int = 0,
) -> Order:
    now = _now()
    cur = await conn.execute(
        """INSERT INTO orders (user_id, product_type, quantity, recipient, price,
                               cost, status, promo, discount, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, product_type, quantity, recipient, price, cost,
         ORDER_DELIVERING, promo, discount, now, now),
    )
    await conn.commit()
    order = await get_order(conn, cur.lastrowid)
    assert order is not None
    return order


async def get_order(conn: aiosqlite.Connection, order_id: int) -> Order | None:
    async with conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(Order, row) if row else None


async def list_orders(
    conn: aiosqlite.Connection, *, user_id: int | None = None, status: str | None = None,
    limit: int = 15,
) -> list[Order]:
    sql = "SELECT * FROM orders WHERE 1 = 1"
    params: list[Any] = []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        return [_from_row(Order, row) for row in await cur.fetchall()]


async def update_order(conn: aiosqlite.Connection, order_id: int, **fields_: Any) -> None:
    if not fields_:
        return
    fields_["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in fields_)
    await conn.execute(
        f"UPDATE orders SET {assignments} WHERE id = ?", (*fields_.values(), order_id)
    )
    await conn.commit()


async def transition_order(
    conn: aiosqlite.Connection, order_id: int, *, expected: str, new: str, **fields_: Any
) -> bool:
    fields_["status"] = new
    fields_["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in fields_)
    cur = await conn.execute(
        f"UPDATE orders SET {assignments} WHERE id = ? AND status = ?",
        (*fields_.values(), order_id, expected),
    )
    await conn.commit()
    if cur.rowcount == 0:
        return False

    # Активацию промокода списываем здесь, а не при вводе кода: заказ мог
    # сорваться и деньги вернуться — тогда активация не потрачена. Условие
    # `status = expected` выше пропускает только один переход, поэтому
    # дважды один и тот же заказ активацию не съест.
    if new == ORDER_DELIVERED:
        order = await get_order(conn, order_id)
        if order and order.promo:
            await use_promo(conn, order.promo, order.user_id)
    return True


async def refunded_game_orders(
    conn: aiosqlite.Connection, marker: str, since: str, limit: int = 50,
) -> list[Order]:
    """Заказы, возвращённые по таймауту, за которыми ещё стоит присмотреть."""
    async with conn.execute(
        """SELECT * FROM orders
           WHERE status = ? AND product_type LIKE 'game:%'
             AND fragment_order_id IS NOT NULL
             AND error LIKE ? AND updated_at >= ?
           ORDER BY id DESC LIMIT ?""",
        (ORDER_REFUNDED, f"{marker}%", since, limit),
    ) as cur:
        return [_from_row(Order, row) for row in await cur.fetchall()]


async def last_game_orders(
    conn: aiosqlite.Connection, limit: int = 5,
) -> list[Order]:
    """Последние игровые заказы — чем бы они ни кончились."""
    async with conn.execute(
        "SELECT * FROM orders WHERE product_type LIKE 'game:%' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cur:
        return [_from_row(Order, row) for row in await cur.fetchall()]


async def unfinished_game_orders(
    conn: aiosqlite.Connection, limit: int = 100,
) -> list[Order]:
    """Игровые заказы, ещё не дошедшие до конца."""
    async with conn.execute(
        """SELECT * FROM orders
           WHERE status IN (?, ?) AND product_type LIKE 'game:%'
           ORDER BY id LIMIT ?""",
        (ORDER_DELIVERING, ORDER_FAILED, limit),
    ) as cur:
        return [_from_row(Order, row) for row in await cur.fetchall()]


async def user_order_stats(conn: aiosqlite.Connection, user_id: int) -> dict[str, int]:
    async with conn.execute(
        """SELECT
             COUNT(*)                                                        AS total,
             SUM(status = ?)                                                 AS done,
             SUM(status = ?)                                                 AS active,
             COALESCE(SUM(CASE WHEN product_type = 'stars' AND status = ?
                               THEN quantity END), 0)                        AS stars,
             COALESCE(SUM(CASE WHEN product_type = 'stars' AND status = ?
                               THEN price END), 0)                           AS stars_spent,
             COALESCE(SUM(CASE WHEN product_type = 'premium' AND status = ?
                               THEN 1 END), 0)                               AS premium,
             COALESCE(SUM(CASE WHEN product_type = 'premium' AND status = ?
                               THEN price END), 0)                           AS premium_spent
           FROM orders WHERE user_id = ?""",
        (ORDER_DELIVERED, ORDER_DELIVERING, ORDER_DELIVERED, ORDER_DELIVERED,
         ORDER_DELIVERED, ORDER_DELIVERED, user_id),
    ) as cur:
        row = await cur.fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


# ----------------------------------------------------------------- тикеты


async def create_ticket(conn: aiosqlite.Connection, user_id: int, subject: str) -> Ticket:
    now = _now()
    cur = await conn.execute(
        "INSERT INTO tickets (user_id, subject, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, subject, TICKET_OPEN, now, now),
    )
    await conn.commit()
    ticket = await get_ticket(conn, cur.lastrowid)
    assert ticket is not None
    return ticket


async def get_ticket(conn: aiosqlite.Connection, ticket_id: int) -> Ticket | None:
    async with conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(Ticket, row) if row else None


async def list_tickets(
    conn: aiosqlite.Connection, *, user_id: int | None = None, status: str | None = None,
    limit: int = 15,
) -> list[Ticket]:
    sql = "SELECT * FROM tickets WHERE 1 = 1"
    params: list[Any] = []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        return [_from_row(Ticket, row) for row in await cur.fetchall()]


async def add_ticket_message(
    conn: aiosqlite.Connection, ticket_id: int, sender_id: int, *, is_admin: bool,
    text: str | None = None, file_id: str | None = None,
) -> None:
    now = _now()
    await conn.execute(
        """INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, text, file_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ticket_id, sender_id, int(is_admin), text, file_id, now),
    )
    await conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
    await conn.commit()


async def close_ticket(conn: aiosqlite.Connection, ticket_id: int) -> bool:
    cur = await conn.execute(
        "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (TICKET_CLOSED, _now(), ticket_id, TICKET_OPEN),
    )
    await conn.commit()
    return cur.rowcount > 0


async def count_open_tickets(conn: aiosqlite.Connection, user_id: int) -> int:
    async with conn.execute(
        "SELECT COUNT(*) AS cnt FROM tickets WHERE user_id = ? AND status = ?",
        (user_id, TICKET_OPEN),
    ) as cur:
        return (await cur.fetchone())["cnt"]


# ------------------------------------------------------------- промокоды


async def create_promo(
    conn: aiosqlite.Connection, code: str, amount: int, max_uses: int,
    kind: str = "bonus", percent: int = 0,
) -> bool:
    """Завести промокод. kind='bonus' — деньги на баланс, 'discount' — скидка."""
    try:
        await conn.execute(
            """INSERT INTO promocodes (code, kind, amount, percent, max_uses, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code.upper(), kind, amount, percent, max_uses, _now()),
        )
    except aiosqlite.IntegrityError:
        await _undo(conn)
        return False
    await conn.commit()
    return True


async def get_promo(conn: aiosqlite.Connection, code: str) -> aiosqlite.Row | None:
    async with conn.execute(
        "SELECT * FROM promocodes WHERE code = ?", (code.upper().strip(),)
    ) as cur:
        return await cur.fetchone()


async def promo_reserved(conn: aiosqlite.Connection, code: str) -> int:
    """Сколько активаций держат незавершённые заказы с этим кодом.

    Активация списывается только после выдачи, поэтому без такого резерва
    сотню заказов можно было бы оформить одновременно на код со лимитом 10.
    """
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE promo = ? AND status IN (?, ?)",
        (code.upper().strip(), ORDER_DELIVERING, ORDER_FAILED),
    ) as cur:
        row = await cur.fetchone()
    return row["n"] or 0


async def check_discount(
    conn: aiosqlite.Connection, code: str, user_id: int
) -> aiosqlite.Row | str:
    """Можно ли применить код к заказу. Строка — причина отказа."""
    promo = await get_promo(conn, code)
    if promo is None:
        return "not_found"
    if promo["kind"] != "discount" or promo["percent"] <= 0:
        return "not_for_order"

    async with conn.execute(
        "SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?",
        (promo["code"], user_id),
    ) as cur:
        if await cur.fetchone():
            return "already_used"

    left = promo["max_uses"] - promo["used_count"] - await promo_reserved(conn, promo["code"])
    if left <= 0:
        return "exhausted"
    return promo


async def use_promo(conn: aiosqlite.Connection, code: str, user_id: int) -> bool:
    """Списать одну активацию. False — если этот клиент код уже отмечал."""
    code = code.upper().strip()
    cur = await conn.execute(
        "INSERT OR IGNORE INTO promo_uses (code, user_id, created_at) VALUES (?, ?, ?)",
        (code, user_id, _now()),
    )
    if cur.rowcount == 0:
        await conn.commit()
        return False
    # Счётчик не должен перевалить за лимит, даже если заказы шли внахлёст.
    await conn.execute(
        "UPDATE promocodes SET used_count = used_count + 1 "
        "WHERE code = ? AND used_count < max_uses",
        (code,),
    )
    await conn.commit()
    return True


async def delete_promo(conn: aiosqlite.Connection, code: str) -> bool:
    cur = await conn.execute("DELETE FROM promocodes WHERE code = ?", (code.upper(),))
    await conn.commit()
    return cur.rowcount > 0


async def redeem_promo(conn: aiosqlite.Connection, code: str, user_id: int) -> int | str:
    """Активировать промокод. Возвращает сумму в дирамах или строку с причиной отказа."""
    code = code.upper().strip()
    async with conn.execute("SELECT * FROM promocodes WHERE code = ?", (code,)) as cur:
        promo = await cur.fetchone()
    if promo is None:
        return "not_found"
    if promo["kind"] == "discount":
        # Код на скидку вводят при покупке, на баланс он ничего не кладёт.
        return "not_for_balance"

    async with conn.execute(
        "SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?", (code, user_id)
    ) as cur:
        if await cur.fetchone():
            return "already_used"

    # Счётчик увеличиваем условием used_count < max_uses — так последний
    # оставшийся код не достанется двоим сразу.
    cur = await conn.execute(
        "UPDATE promocodes SET used_count = used_count + 1 WHERE code = ? AND used_count < max_uses",
        (code,),
    )
    if cur.rowcount == 0:
        await conn.commit()
        return "exhausted"

    try:
        await conn.execute(
            "INSERT INTO promo_uses (code, user_id, created_at) VALUES (?, ?, ?)",
            (code, user_id, _now()),
        )
    except aiosqlite.IntegrityError:
        await conn.execute(
            "UPDATE promocodes SET used_count = used_count - 1 WHERE code = ?", (code,)
        )
        await conn.commit()
        return "already_used"

    await conn.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?", (promo["amount"], user_id)
    )
    await conn.commit()
    return int(promo["amount"])


async def list_promos(conn: aiosqlite.Connection, limit: int = 20) -> list[aiosqlite.Row]:
    async with conn.execute(
        "SELECT * FROM promocodes ORDER BY rowid DESC LIMIT ?", (limit,)
    ) as cur:
        return list(await cur.fetchall())


# --------------------------------------------------------------- сводка


async def global_stats(conn: aiosqlite.Connection) -> dict[str, Any]:
    async with conn.execute("SELECT COUNT(*) AS c FROM users") as cur:
        users = (await cur.fetchone())["c"]
    async with conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM deposits WHERE status = ?", (DEP_APPROVED,)
    ) as cur:
        deposits = (await cur.fetchone())["s"]
    async with conn.execute(
        """SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s
           FROM orders WHERE status = ?""", (ORDER_DELIVERED,)
    ) as cur:
        row = await cur.fetchone()
    async with conn.execute(
        "SELECT COUNT(*) AS c FROM deposits WHERE status = ?", (DEP_PENDING,)
    ) as cur:
        pending = (await cur.fetchone())["c"]
    async with conn.execute(
        "SELECT COUNT(*) AS c FROM tickets WHERE status = ?", (TICKET_OPEN,)
    ) as cur:
        open_tickets = (await cur.fetchone())["c"]
    async with conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE status = ?", (ORDER_FAILED,)
    ) as cur:
        failed = (await cur.fetchone())["c"]
    async with conn.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users") as cur:
        held = (await cur.fetchone())["s"]
    return {
        "users": users, "deposits": deposits, "orders": row["c"], "revenue": row["s"],
        "pending_deposits": pending, "open_tickets": open_tickets,
        "failed_orders": failed, "held_balance": held,
    }


# ══════════════════════════════════════════════════════════════ отчёты


async def report(
    conn: aiosqlite.Connection, since: str, until: str
) -> dict[str, Any]:
    """Сводка за период [since, until) по времени UTC в ISO-формате.

    Границы сравниваются строками: ISO-даты сортируются так же, как время,
    поэтому индекс по created_at работает и без разбора дат.
    """
    async with conn.execute(
        """SELECT
             COUNT(*)                                              AS orders,
             COALESCE(SUM(status = ?), 0)                          AS done,
             COALESCE(SUM(status = ?), 0)                          AS refunded,
             COALESCE(SUM(status = ?), 0)                          AS failed,
             COALESCE(SUM(CASE WHEN status = ? THEN price END), 0) AS revenue,
             COALESCE(SUM(CASE WHEN status = ? THEN cost  END), 0) AS cost,
             COALESCE(SUM(CASE WHEN status = ? AND product_type = 'stars'
                               THEN quantity END), 0)              AS stars,
             COALESCE(SUM(CASE WHEN status = ? AND product_type = 'premium'
                               THEN quantity END), 0)              AS premium_months,
             COALESCE(SUM(CASE WHEN status = ? THEN price END), 0) AS refunded_sum
           FROM orders WHERE created_at >= ? AND created_at < ?""",
        (ORDER_DELIVERED, ORDER_REFUNDED, ORDER_FAILED, ORDER_DELIVERED,
         ORDER_DELIVERED, ORDER_DELIVERED, ORDER_DELIVERED, ORDER_REFUNDED,
         since, until),
    ) as cur:
        row = await cur.fetchone()
    data = {key: (row[key] or 0) for key in row.keys()}

    async with conn.execute(
        """SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
           FROM deposits
           WHERE status = ? AND created_at >= ? AND created_at < ?""",
        (DEP_APPROVED, since, until),
    ) as cur:
        dep = await cur.fetchone()
    data["deposits"] = dep["cnt"]
    data["deposits_sum"] = dep["total"]

    async with conn.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE created_at >= ? AND created_at < ?",
        (since, until),
    ) as cur:
        data["new_users"] = (await cur.fetchone())["cnt"]

    async with conn.execute(
        """SELECT COUNT(DISTINCT user_id) AS cnt FROM orders
           WHERE status = ? AND created_at >= ? AND created_at < ?""",
        (ORDER_DELIVERED, since, until),
    ) as cur:
        data["buyers"] = (await cur.fetchone())["cnt"]

    added, taken = await adjustments_total(conn, since, until)
    data["adjust_added"] = added
    data["adjust_taken"] = taken

    data["profit"] = data["revenue"] - data["cost"]
    return data


async def report_by_product(
    conn: aiosqlite.Connection, since: str, until: str
) -> list[dict[str, Any]]:
    """Что продано за период по каждому направлению.

    Общая цифра «продано на 186 сомони» не отвечает на главный вопрос:
    это звёзды или игры. Возвраты считаем здесь же — пятнадцать возвратов
    на тринадцать выдач видно только в разбивке, и сразу понятно, какое
    направление их делает.
    """
    async with conn.execute(
        """SELECT product_type,
                  COALESCE(SUM(status = ?), 0)                          AS orders,
                  COALESCE(SUM(CASE WHEN status = ? THEN quantity END), 0) AS quantity,
                  COALESCE(SUM(CASE WHEN status = ? THEN price END), 0) AS revenue,
                  COALESCE(SUM(CASE WHEN status = ? THEN cost  END), 0) AS cost,
                  COALESCE(SUM(status = ?), 0)                          AS refunds,
                  COALESCE(SUM(CASE WHEN status = ? THEN price END), 0) AS refunded_sum
           FROM orders
           WHERE created_at >= ? AND created_at < ?
           GROUP BY product_type""",
        (ORDER_DELIVERED, ORDER_DELIVERED, ORDER_DELIVERED, ORDER_DELIVERED,
         ORDER_REFUNDED, ORDER_REFUNDED, since, until),
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]

    for row in rows:
        row["profit"] = row["revenue"] - row["cost"]
        row["title"] = product_title(row["product_type"])
        row["is_game"] = row["product_type"].startswith("game:")

    # Пустые направления в отчёте не нужны: строка «Premium — 0» ничего
    # не говорит, а места занимает столько же, сколько настоящая продажа.
    rows = [r for r in rows if r["orders"] or r["refunds"]]
    rows.sort(key=lambda r: (-r["revenue"], -r["orders"], r["title"]))
    return rows


async def daily_series(
    conn: aiosqlite.Connection, since: str, until: str, tz_hours: int
) -> list[tuple[str, int, int, int]]:
    """По дням: дата, выполнено, выручка, прибыль.

    Дата берётся с поправкой на часовой пояс владельца, иначе вечерние
    заказы попадали бы во «вчера».
    """
    shift = f"{tz_hours:+d} hours"
    async with conn.execute(
        f"""SELECT date(created_at, '{shift}') AS day,
                   COUNT(*) AS done,
                   COALESCE(SUM(price), 0) AS revenue,
                   COALESCE(SUM(price - cost), 0) AS profit
            FROM orders
            WHERE status = ? AND created_at >= ? AND created_at < ?
            GROUP BY day ORDER BY day""",
        (ORDER_DELIVERED, since, until),
    ) as cur:
        return [
            (row["day"], row["done"], row["revenue"], row["profit"])
            for row in await cur.fetchall()
        ]


async def remember_event(
    conn: aiosqlite.Connection, event_id: str, *,
    kind: str = "", order_id: str = "", status: str = "",
) -> bool:
    """Записать событие вебхука. False — такое уже приходило.

    Вся защита от повторов держится на первичном ключе: проверка «а нет ли
    уже» отдельным запросом пропустила бы два события, пришедших разом.
    """
    cur = await conn.execute(
        """INSERT OR IGNORE INTO webhook_events
               (event_id, kind, order_id, status, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (str(event_id), kind, str(order_id), status, _now()),
    )
    await conn.commit()
    return cur.rowcount > 0


async def events_seen(conn: aiosqlite.Connection) -> tuple[int, str]:
    """Сколько событий приняли и когда было последнее."""
    async with conn.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM webhook_events"
    ) as cur:
        row = await cur.fetchone()
    return (row["n"] or 0), (row["last"] or "")


async def find_order_by_external(
    conn: aiosqlite.Connection, external_id: str
) -> Order | None:
    """Найти заказ по номеру на стороне сервиса выдачи."""
    async with conn.execute(
        "SELECT * FROM orders WHERE fragment_order_id = ? ORDER BY id DESC LIMIT 1",
        (str(external_id),),
    ) as cur:
        row = await cur.fetchone()
    return _from_row(Order, row) if row else None


# ═════════════════════════════════════════════════ API для разработчиков


@dataclass
class ApiKey:
    id: int
    user_id: int
    label: str
    prefix: str
    tail: str
    key_hash: str
    enabled: int
    revoked_at: str | None
    last_used_at: str | None
    last_ip: str | None
    requests: int
    created_at: str

    @property
    def masked(self) -> str:
        """Как ключ показывается после создания: начало, звёзды, хвост."""
        return f"{self.prefix}…{self.tail}" if self.tail else f"{self.prefix}…"

    @property
    def live(self) -> bool:
        return bool(self.enabled) and not self.revoked_at


async def add_api_key(
    conn: aiosqlite.Connection, *, user_id: int, label: str,
    prefix: str, tail: str, key_hash: str,
) -> ApiKey:
    cur = await conn.execute(
        """INSERT INTO api_keys (user_id, label, prefix, tail, key_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, label[:64], prefix, tail, key_hash, _now()),
    )
    await conn.commit()
    key = await get_api_key(conn, cur.lastrowid)
    assert key is not None
    return key


async def get_api_key(conn: aiosqlite.Connection, key_id: int) -> ApiKey | None:
    async with conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)) as cur:
        row = await cur.fetchone()
    return _from_row(ApiKey, row) if row else None


async def api_keys_by_prefix(
    conn: aiosqlite.Connection, prefix: str
) -> list[ApiKey]:
    """Кандидаты на проверку. Видимое начало ключа не секрет — оно и
    служит указателем, чтобы не сверять хеш со всей таблицей."""
    async with conn.execute(
        "SELECT * FROM api_keys WHERE prefix = ?", (prefix,)
    ) as cur:
        return [_from_row(ApiKey, row) for row in await cur.fetchall()]


async def api_keys_of(conn: aiosqlite.Connection, user_id: int) -> list[ApiKey]:
    async with conn.execute(
        "SELECT * FROM api_keys WHERE user_id = ? AND revoked_at IS NULL "
        "ORDER BY id DESC", (user_id,)
    ) as cur:
        return [_from_row(ApiKey, row) for row in await cur.fetchall()]


async def all_api_keys(conn: aiosqlite.Connection, limit: int = 50) -> list[ApiKey]:
    async with conn.execute(
        "SELECT * FROM api_keys WHERE revoked_at IS NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cur:
        return [_from_row(ApiKey, row) for row in await cur.fetchall()]


async def set_api_key_enabled(
    conn: aiosqlite.Connection, key_id: int, enabled: bool
) -> None:
    await conn.execute(
        "UPDATE api_keys SET enabled = ? WHERE id = ?", (int(enabled), key_id)
    )
    await conn.commit()


async def revoke_api_key(conn: aiosqlite.Connection, key_id: int) -> None:
    """Отозвать ключ. Строку не удаляем: по ней ещё читаются заказы и
    журнал запросов, а «удалённый» ключ без следов — дыра в учёте."""
    await conn.execute(
        "UPDATE api_keys SET enabled = 0, revoked_at = ? WHERE id = ?",
        (_now(), key_id),
    )
    await conn.commit()


async def note_api_use(
    conn: aiosqlite.Connection, key_id: int, ip: str = ""
) -> None:
    await conn.execute(
        "UPDATE api_keys SET last_used_at = ?, last_ip = ?, "
        "requests = requests + 1 WHERE id = ?",
        (_now(), ip[:45] or None, key_id),
    )
    await conn.commit()


async def log_api_request(
    conn: aiosqlite.Connection, *, request_id: str, key_id: int | None,
    user_id: int | None, method: str, path: str, status: int,
    error: str = "", ip: str = "", user_agent: str = "", ms: int = 0,
) -> None:
    """Журнал запросов. Ни ключа, ни подписи здесь не бывает: путь и
    заголовки пишутся отдельными полями, а тело не пишется вовсе."""
    await conn.execute(
        """INSERT INTO api_requests (request_id, key_id, user_id, method, path,
                                     status, error, ip, user_agent, ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, key_id, user_id, method, path[:200], status,
         (error or "")[:300] or None, ip[:45] or None, user_agent[:200] or None,
         ms, _now()),
    )
    await conn.commit()


async def api_requests_of(
    conn: aiosqlite.Connection, user_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT * FROM api_requests WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def api_stats(
    conn: aiosqlite.Connection, user_id: int, since: str = ""
) -> dict[str, int]:
    """Сводка по запросам: сколько было, сколько прошло, сколько ошибок."""
    sql = ("SELECT COUNT(*) AS total, "
           "COALESCE(SUM(status < 400), 0) AS ok, "
           "COALESCE(SUM(status >= 400), 0) AS errors "
           "FROM api_requests WHERE user_id = ?")
    params: list = [user_id]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


# ---- заказы по API -------------------------------------------------


async def next_api_ref(conn: aiosqlite.Connection) -> str:
    """Публичный номер заказа. Считаем от id заказа в orders, чтобы номер
    был один на обе системы и не разъезжался при переносе базы."""
    async with conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nxt FROM orders") as cur:
        return f"ORD-{(await cur.fetchone())['nxt']:06d}"


async def link_api_order(
    conn: aiosqlite.Connection, *, ref: str, order_id: int, key_id: int,
    user_id: int, product_id: str, customer: str = "", idem_key: str = "",
) -> None:
    await conn.execute(
        """INSERT INTO api_orders (ref, order_id, key_id, user_id, product_id,
                                   customer, idem_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ref, order_id, key_id, user_id, product_id, customer[:190],
         idem_key[:190] or None, _now()),
    )
    await conn.commit()


async def api_order_by_ref(
    conn: aiosqlite.Connection, ref: str
) -> dict[str, Any] | None:
    async with conn.execute("SELECT * FROM api_orders WHERE ref = ?", (ref,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def api_order_of(
    conn: aiosqlite.Connection, order_id: int
) -> dict[str, Any] | None:
    """Заказ пришёл по API? Нужно, чтобы не писать роботу в Telegram."""
    async with conn.execute(
        "SELECT * FROM api_orders WHERE order_id = ?", (order_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def api_order_by_idem(
    conn: aiosqlite.Connection, key_id: int, idem_key: str
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM api_orders WHERE key_id = ? AND idem_key = ?",
        (key_id, idem_key),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


def _span_sql(column: str, since: str, until: str) -> tuple[str, list]:
    """Кусок WHERE для отрезка времени и его значения.

    Границы сравниваются строками: ISO-даты сортируются так же, как время,
    поэтому индекс по created_at работает и без разбора дат. Пустая
    граница означает «без ограничения» — так «за всё время» не требует
    отдельного запроса.
    """
    sql, params = "", []
    if since:
        sql += f" AND {column} >= ?"
        params.append(since)
    if until:
        sql += f" AND {column} < ?"
        params.append(until)
    return sql, params


async def api_orders_of(
    conn: aiosqlite.Connection, user_id: int, limit: int = 20, offset: int = 0,
    since: str = "", until: str = "",
) -> list[dict[str, Any]]:
    """Заказы клиента вместе со статусом из основной таблицы."""
    where, params = _span_sql("a.created_at", since, until)
    async with conn.execute(
        f"""SELECT a.*, o.status, o.price, o.quantity, o.recipient, o.error,
                   o.updated_at
            FROM api_orders a JOIN orders o ON o.id = a.order_id
            WHERE a.user_id = ?{where}
            ORDER BY a.rowid DESC LIMIT ? OFFSET ?""",
        (user_id, *params, limit, offset),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def api_orders_count(
    conn: aiosqlite.Connection, user_id: int, since: str = "", until: str = ""
) -> int:
    where, params = _span_sql("created_at", since, until)
    async with conn.execute(
        f"SELECT COUNT(*) FROM api_orders WHERE user_id = ?{where}",
        (user_id, *params),
    ) as cur:
        return int((await cur.fetchone())[0] or 0)


async def api_summary(
    conn: aiosqlite.Connection, user_id: int, since: str = "", until: str = ""
) -> dict[str, int]:
    """Сводка клиента за отрезок: сколько заказов и куда ушли деньги.

    Считаем в базе, а не перебором заказов: за месяц их могут быть
    тысячи, и тянуть их в память ради четырёх чисел незачем.
    """
    where, params = _span_sql("a.created_at", since, until)
    async with conn.execute(
        f"""SELECT COUNT(*)                                            AS orders,
                   COALESCE(SUM(o.status = ?), 0)                      AS done,
                   COALESCE(SUM(o.status = ?), 0)                      AS refunded,
                   COALESCE(SUM(o.status IN (?, ?)), 0)                AS working,
                   COALESCE(SUM(CASE WHEN o.status = ? THEN o.price END), 0)
                                                                       AS spent,
                   COALESCE(SUM(CASE WHEN o.status = ? THEN o.price END), 0)
                                                                       AS returned
            FROM api_orders a JOIN orders o ON o.id = a.order_id
            WHERE a.user_id = ?{where}""",
        (ORDER_DELIVERED, ORDER_REFUNDED, ORDER_DELIVERING, ORDER_FAILED,
         ORDER_DELIVERED, ORDER_REFUNDED, user_id, *params),
    ) as cur:
        row = await cur.fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


async def api_orders_to_notify(
    conn: aiosqlite.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    """Заказы, о которых клиенту ещё не сообщили вебхуком.

    Сравниваем сохранённое состояние с настоящим статусом заказа —
    так ни один путь смены статуса не приходится помнить отдельно.
    """
    async with conn.execute(
        """SELECT a.*, o.status, o.price, o.quantity, o.recipient, o.error
           FROM api_orders a JOIN orders o ON o.id = a.order_id
           WHERE a.hook_state != o.status AND a.hook_tries < 8
           ORDER BY a.rowid LIMIT ?""",
        (limit,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def note_api_hook(
    conn: aiosqlite.Connection, ref: str, *, sent: str = "", failed: bool = False
) -> None:
    if failed:
        await conn.execute(
            "UPDATE api_orders SET hook_tries = hook_tries + 1 WHERE ref = ?",
            (ref,),
        )
    else:
        await conn.execute(
            "UPDATE api_orders SET hook_state = ?, hook_tries = 0 WHERE ref = ?",
            (sent, ref),
        )
    await conn.commit()


# ---- журнал движения денег -----------------------------------------


async def add_api_tx(
    conn: aiosqlite.Connection, *, user_id: int, kind: str, amount: int,
    balance_before: int, balance_after: int, order_ref: str = "", note: str = "",
) -> str:
    """Запись в журнал: что, на сколько, и каким стал баланс.

    Баланс до и после храним явно: пересчитывать его сложением задним
    числом нельзя — ручные правки владельца journal обошли бы стороной.
    """
    import secrets as _secrets

    tx_id = f"TX-{_secrets.token_hex(6).upper()}"
    await conn.execute(
        """INSERT INTO api_transactions (tx_id, user_id, kind, amount,
               balance_before, balance_after, order_ref, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tx_id, user_id, kind, amount, balance_before, balance_after,
         order_ref or None, note[:190], _now()),
    )
    await conn.commit()
    return tx_id


# ---- заявки на возврат ----------------------------------------------


async def ask_refund(
    conn: aiosqlite.Connection, *, ref: str, user_id: int, reason: str
) -> dict[str, Any]:
    """Оставить заявку. Повторная просьба возвращает прежнюю, не плодя новых."""
    await conn.execute(
        """INSERT INTO api_refunds (ref, user_id, reason, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(ref) DO NOTHING""",
        (ref, user_id, reason[:500], _now()),
    )
    await conn.commit()
    return await refund_request(conn, ref)


async def refund_request(conn: aiosqlite.Connection, ref: str) -> dict[str, Any]:
    async with conn.execute(
        "SELECT * FROM api_refunds WHERE ref = ?", (ref,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}


async def refund_requests(
    conn: aiosqlite.Connection, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    async with conn.execute(
        """SELECT r.*, o.price, o.status AS order_status, a.product_id
           FROM api_refunds r
           JOIN api_orders a ON a.ref = r.ref
           JOIN orders o ON o.id = a.order_id
           WHERE r.status = ? ORDER BY r.id DESC LIMIT ?""",
        (status, limit),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def decide_refund(
    conn: aiosqlite.Connection, ref: str, *, approved: bool, answer: str = ""
) -> bool:
    """Отметить решение владельца. False — заявку уже закрыли до нас."""
    cur = await conn.execute(
        """UPDATE api_refunds SET status = ?, answer = ?, decided_at = ?
           WHERE ref = ? AND status = 'pending'""",
        ("approved" if approved else "denied", answer[:300], _now(), ref),
    )
    await conn.commit()
    return bool(cur.rowcount)


# ---- как уведомлять разработчика ------------------------------------

#: Что шлём в Telegram владельцу ключа. По умолчанию только проблемы:
#: «заказ выполнен» на сотне заказов — это сто сообщений подряд, после
#: которых их перестают читать вовсе, включая важные.
NOTIFY_MODES = ("off", "problems", "all")


async def api_prefs(conn: aiosqlite.Connection, user_id: int) -> dict[str, Any]:
    async with conn.execute(
        "SELECT * FROM api_prefs WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {"user_id": user_id, "notify": "problems", "digest": 0,
                "digest_on": ""}
    return dict(row)


async def set_api_prefs(conn: aiosqlite.Connection, user_id: int, **fields) -> None:
    keep = {k: v for k, v in fields.items()
            if k in ("notify", "digest", "digest_on")}
    if not keep:
        return
    columns = ", ".join(keep)
    marks = ", ".join("?" for _ in keep)
    updates = ", ".join(f"{k} = excluded.{k}" for k in keep)
    await conn.execute(
        f"""INSERT INTO api_prefs (user_id, {columns}) VALUES (?, {marks})
            ON CONFLICT(user_id) DO UPDATE SET {updates}""",
        (user_id, *keep.values()),
    )
    await conn.commit()


async def api_digest_targets(conn: aiosqlite.Connection, today: str) -> list[int]:
    """Кому сегодня ещё не слали сводку."""
    async with conn.execute(
        "SELECT user_id FROM api_prefs WHERE digest = 1 AND digest_on != ?",
        (today,),
    ) as cur:
        return [row[0] for row in await cur.fetchall()]


# ---- пропуск в кабинет ----------------------------------------------

#: Сколько пропусков держим одному человеку: по одному на устройство,
#: с запасом. Дальше самый старый вытесняется — иначе забытые пропуски
#: копились бы годами, и каждый оставался бы рабочим.
MAX_PASSES = 5


async def add_api_pass(
    conn: aiosqlite.Connection, *, user_id: int, prefix: str, token_hash: str
) -> None:
    await conn.execute(
        """INSERT INTO api_passes (user_id, prefix, token_hash, created_at)
           VALUES (?, ?, ?, ?)""",
        (user_id, prefix, token_hash, _now()),
    )
    await conn.execute(
        """DELETE FROM api_passes WHERE user_id = ? AND id NOT IN (
               SELECT id FROM api_passes WHERE user_id = ?
               ORDER BY id DESC LIMIT ?)""",
        (user_id, user_id, MAX_PASSES),
    )
    await conn.commit()


async def api_passes_by_prefix(
    conn: aiosqlite.Connection, prefix: str
) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT * FROM api_passes WHERE prefix = ?", (prefix,)
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def api_pass_used(conn: aiosqlite.Connection, pass_id: int) -> None:
    await conn.execute("UPDATE api_passes SET last_used_at = ? WHERE id = ?",
                       (_now(), pass_id))
    await conn.commit()


async def drop_api_passes(conn: aiosqlite.Connection, user_id: int) -> int:
    """Отозвать все пропуски человека. Возвращает сколько убрали."""
    cur = await conn.execute("DELETE FROM api_passes WHERE user_id = ?",
                             (user_id,))
    await conn.commit()
    return cur.rowcount or 0


async def api_txs_of(
    conn: aiosqlite.Connection, user_id: int, limit: int = 20, offset: int = 0,
    since: str = "", until: str = "",
) -> list[dict[str, Any]]:
    where, params = _span_sql("created_at", since, until)
    async with conn.execute(
        f"SELECT * FROM api_transactions WHERE user_id = ?{where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, *params, limit, offset),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def api_txs_count(
    conn: aiosqlite.Connection, user_id: int, since: str = "", until: str = ""
) -> int:
    where, params = _span_sql("created_at", since, until)
    async with conn.execute(
        f"SELECT COUNT(*) FROM api_transactions WHERE user_id = ?{where}",
        (user_id, *params),
    ) as cur:
        return int((await cur.fetchone())[0] or 0)


# ---- вебхук клиента -------------------------------------------------


async def get_api_hook(
    conn: aiosqlite.Connection, user_id: int
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM api_webhooks WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_api_hook(
    conn: aiosqlite.Connection, user_id: int, url: str, secret: str = ""
) -> None:
    now = _now()
    await conn.execute(
        """INSERT INTO api_webhooks (user_id, url, secret, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET url = excluded.url,
               secret = CASE WHEN excluded.secret != '' THEN excluded.secret
                             ELSE api_webhooks.secret END,
               enabled = 1, fails = 0, last_error = ''""",
        (user_id, url[:400], secret, now),
    )
    await conn.commit()


async def note_api_hook_result(
    conn: aiosqlite.Connection, user_id: int, ok: bool, error: str = ""
) -> None:
    if ok:
        await conn.execute(
            "UPDATE api_webhooks SET last_ok_at = ?, fails = 0, last_error = '' "
            "WHERE user_id = ?", (_now(), user_id),
        )
    else:
        await conn.execute(
            "UPDATE api_webhooks SET fails = fails + 1, last_error = ? "
            "WHERE user_id = ?", (error[:190], user_id),
        )
    await conn.commit()


async def charge_logged(
    conn: aiosqlite.Connection, user_id: int, amount: int, *,
    order_ref: str = "", note: str = "",
) -> tuple[bool, str]:
    """Списать с баланса и записать это в журнал API.

    Баланс после списания берём из самого UPDATE (RETURNING), а не
    отдельным запросом: между списанием и чтением успевает пройти
    другой заказ, и в журнале оказался бы чужой остаток.
    """
    async with conn.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ? "
        "RETURNING balance",
        (amount, user_id, amount),
    ) as cur:
        row = await cur.fetchone()
    await conn.commit()
    if row is None:
        return False, ""

    after = row["balance"]
    tx_id = await add_api_tx(
        conn, user_id=user_id, kind="charge", amount=-amount,
        balance_before=after + amount, balance_after=after,
        order_ref=order_ref, note=note,
    )
    return True, tx_id


async def credit_logged(
    conn: aiosqlite.Connection, user_id: int, amount: int, *, kind: str = "refund",
    order_ref: str = "", note: str = "", as_deposit: bool = False,
) -> str:
    """Начислить на баланс и записать это в журнал API."""
    if as_deposit:
        sql = ("UPDATE users SET balance = balance + ?, "
               "total_deposit = total_deposit + ? WHERE id = ? RETURNING balance")
        params: tuple = (amount, amount, user_id)
    else:
        sql = "UPDATE users SET balance = balance + ? WHERE id = ? RETURNING balance"
        params = (amount, user_id)

    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    await conn.commit()
    if row is None:
        return ""

    after = row["balance"]
    return await add_api_tx(
        conn, user_id=user_id, kind=kind, amount=amount,
        balance_before=after - amount, balance_after=after,
        order_ref=order_ref, note=note,
    )


async def reserve_idem(
    conn: aiosqlite.Connection, key_id: int, idem_key: str
) -> tuple[bool, str]:
    """Занять ключ идемпотентности.

    (True, "")     — заняли, можно создавать заказ;
    (False, ref)   — такой запрос уже был, вот его заказ;
    (False, "")    — первый запрос ещё в работе, ответа пока нет.
    """
    try:
        await conn.execute(
            "INSERT INTO api_idempotency (key_id, idem_key, created_at) "
            "VALUES (?, ?, ?)",
            (key_id, idem_key, _now()),
        )
        await conn.commit()
        return True, ""
    except sqlite3.IntegrityError:
        # Повторный запрос с тем же ключом — обычное дело у разработчиков.
        # Каждый такой повтор без отката вешал на соединение API замок
        # на запись до следующего заказа, и весь бот ждал его.
        await _undo(conn)
        async with conn.execute(
            "SELECT ref FROM api_idempotency WHERE key_id = ? AND idem_key = ?",
            (key_id, idem_key),
        ) as cur:
            row = await cur.fetchone()
        return False, (row["ref"] if row else "")


async def finish_idem(
    conn: aiosqlite.Connection, key_id: int, idem_key: str, ref: str
) -> None:
    await conn.execute(
        "UPDATE api_idempotency SET ref = ? WHERE key_id = ? AND idem_key = ?",
        (ref, key_id, idem_key),
    )
    await conn.commit()


async def release_idem(
    conn: aiosqlite.Connection, key_id: int, idem_key: str
) -> None:
    """Отпустить резерв — заказ создать не вышло, и повтор должен пройти."""
    await conn.execute(
        "DELETE FROM api_idempotency WHERE key_id = ? AND idem_key = ? AND ref = ''",
        (key_id, idem_key),
    )
    await conn.commit()


# ═══════════════════════════════════ зачисления от банковского бота


#: Что случилось с банковским уведомлением.
BANK_MATCHED = "matched"       # нашлась ровно одна заявка, деньги зачислены
BANK_HOLD = "hold"             # деньги пришли, ждём чек от клиента
BANK_AMBIGUOUS = "ambiguous"   # подходящих заявок несколько — решает владелец
BANK_UNKNOWN = "unknown"       # заявки на такую сумму нет
BANK_FAILED = "failed"         # уведомление не разобралось

BANK_TITLES = {
    BANK_MATCHED: "✅ Зачислено",
    BANK_HOLD: "📸 Ждём чек",
    BANK_AMBIGUOUS: "⚠️ Несколько заявок",
    BANK_UNKNOWN: "❔ Заявка не найдена",
    BANK_FAILED: "🚫 Не разобрал",
}


@dataclass
class BankPayment:
    id: int
    source: str
    message_id: int
    op_code: str | None
    amount: int
    sender: str
    card_tail: str
    bank_time: str
    seen_at: str
    status: str
    deposit_id: int | None
    note: str
    body: str
    comment: str = ""


async def claim_bank_payment(
    conn: aiosqlite.Connection, *, source: str, message_id: int,
    op_code: str = "", amount: int = 0, sender: str = "", card_tail: str = "",
    bank_time: str = "", status: str = BANK_FAILED, note: str = "",
    body: str = "", comment: str = "",
) -> BankPayment | None:
    """Записать уведомление. None — такое уже обрабатывали.

    Запись идёт ДО всякой работы с деньгами и падает на уникальном
    индексе, если это повтор. Проверять отдельным SELECT нельзя: два
    сообщения подряд успевают проскочить между проверкой и вставкой.
    """
    try:
        cur = await conn.execute(
            """INSERT INTO bank_payments (source, message_id, op_code, amount,
                   sender, card_tail, bank_time, seen_at, status, note, body,
                   comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, message_id, op_code or None, amount, sender[:64],
             card_tail[:8], bank_time[:32], _now(), status, note[:190],
             body[:2000], comment[:64]),
        )
        await conn.commit()
    except sqlite3.IntegrityError:
        await _undo(conn)
        return None
    return await get_bank_payment(conn, cur.lastrowid)


async def get_bank_payment(
    conn: aiosqlite.Connection, payment_id: int
) -> BankPayment | None:
    async with conn.execute(
        "SELECT * FROM bank_payments WHERE id = ?", (payment_id,)
    ) as cur:
        row = await cur.fetchone()
    return _from_row(BankPayment, row) if row else None


async def close_bank_payment(
    conn: aiosqlite.Connection, payment_id: int, *, status: str,
    deposit_id: int | None = None, note: str = "",
) -> None:
    await conn.execute(
        "UPDATE bank_payments SET status = ?, deposit_id = ?, note = ? WHERE id = ?",
        (status, deposit_id, note[:190], payment_id),
    )
    await conn.commit()


async def list_bank_payments(
    conn: aiosqlite.Connection, *, status: str = "", limit: int = 20
) -> list[BankPayment]:
    sql = "SELECT * FROM bank_payments"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        return [_from_row(BankPayment, row) for row in await cur.fetchall()]


async def bank_payment_stats(conn: aiosqlite.Connection) -> dict[str, int]:
    async with conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM bank_payments GROUP BY status"
    ) as cur:
        return {row["status"]: row["cnt"] for row in await cur.fetchall()}


async def pending_deposits_for(
    conn: aiosqlite.Connection, amount: int, hours: int = 6
) -> list[Deposit]:
    """Свежие заявки на проверке ровно на эту сумму.

    Сравнение целыми числами в дирамах: у дробных чисел 617.00 и 617.0000001
    оказались бы разными, и платёж повис бы без причины.

    Старые заявки в расчёт не берём. Клиент мог ввести сумму и передумать
    платить — такая заявка висит в ожидании вечно. Через неделю их
    накопится столько, что каждая настоящая оплата станет «спорной», и
    автоматика перестанет работать вовсе. Окно в несколько часов покрывает
    любой реальный перевод: банк присылает уведомление за секунды.
    """
    edge = (datetime.now(timezone.utc)
            - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
    async with conn.execute(
        "SELECT * FROM deposits WHERE status = ? AND amount = ? "
        "AND created_at >= ? ORDER BY id",
        (DEP_PENDING, amount, edge),
    ) as cur:
        return [_from_row(Deposit, row) for row in await cur.fetchall()]


async def attach_receipt(
    conn: aiosqlite.Connection, deposit_id: int, file_id: str
) -> None:
    """Прикрепить чек к уже созданной заявке.

    Статус не трогаем: заявку мог уже закрыть юзербот, пока клиент искал
    скриншот, и возвращать её в ожидание значило бы зачислить дважды.
    """
    await conn.execute(
        "UPDATE deposits SET receipt_file_id = ?, updated_at = ? WHERE id = ?",
        (file_id, _now(), deposit_id),
    )
    await conn.commit()


async def bank_payment_for_deposit(
    conn: aiosqlite.Connection, deposit_id: int
) -> BankPayment | None:
    """Каким банковским зачислением закрыта заявка.

    Нужно, чтобы рядом с чеком клиента показать, что банк подтвердил на
    самом деле. Чек рисуется за минуту, выписка банка — нет.
    """
    async with conn.execute(
        "SELECT * FROM bank_payments WHERE deposit_id = ? ORDER BY id DESC LIMIT 1",
        (deposit_id,),
    ) as cur:
        row = await cur.fetchone()
    return _from_row(BankPayment, row) if row else None


async def unclaimed_bank_payments(
    conn: aiosqlite.Connection, amount: int, hours: int = 6
) -> list[BankPayment]:
    """Уведомления банка на такую сумму, которые ещё ни к чему не привязаны.

    Нужны для оплаты из России: там деньги приходят ДО того, как клиент
    пришёл в бота. Уведомление уже лежит со статусом «заявки нет», и
    когда клиент называет сумму из чека — оно находится.
    """
    since = (datetime.now(timezone.utc)
             - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
    async with conn.execute(
        """SELECT * FROM bank_payments
           WHERE amount = ? AND deposit_id IS NULL AND status = ?
             AND seen_at >= ? ORDER BY id DESC""",
        (amount, BANK_UNKNOWN, since),
    ) as cur:
        return [_from_row(BankPayment, row) for row in await cur.fetchall()]


async def held_bank_payment(
    conn: aiosqlite.Connection, deposit_id: int
) -> BankPayment | None:
    """Зачисление, которое ждёт чек по этой заявке.

    Деньги от банка уже пришли и сумма сошлась, но клиент ещё не прислал
    чек. Пока чека нет, баланс не трогаем: совпадения одной суммы мало,
    чтобы решить, чьи это деньги.
    """
    async with conn.execute(
        "SELECT * FROM bank_payments WHERE deposit_id = ? AND status = ? "
        "ORDER BY id DESC LIMIT 1",
        (deposit_id, BANK_HOLD),
    ) as cur:
        row = await cur.fetchone()
    return _from_row(BankPayment, row) if row else None


@dataclass
class BankSender:
    sender: str
    user_id: int
    payments: int
    bound_at: str
    last_at: str


async def sender_owner(
    conn: aiosqlite.Connection, sender: str
) -> BankSender | None:
    """Чей это плательщик. None — видим его впервые."""
    if not sender.strip():
        return None
    async with conn.execute(
        "SELECT * FROM bank_senders WHERE sender = ?", (sender.strip(),)
    ) as cur:
        row = await cur.fetchone()
    return _from_row(BankSender, row) if row else None


async def bind_sender(
    conn: aiosqlite.Connection, sender: str, user_id: int
) -> None:
    """Закрепить плательщика за клиентом.

    Второй раз тот же плательщик придёт уже узнанным, и чек у клиента
    спрашивать будет незачем.
    """
    if not sender.strip():
        return
    now = _now()
    await conn.execute(
        """INSERT INTO bank_senders (sender, user_id, payments, bound_at, last_at)
           VALUES (?, ?, 1, ?, ?)
           ON CONFLICT(sender) DO UPDATE SET
               payments = bank_senders.payments + 1, last_at = excluded.last_at""",
        (sender.strip(), user_id, now, now),
    )
    await conn.commit()


async def unbind_sender(conn: aiosqlite.Connection, sender: str) -> None:
    await conn.execute("DELETE FROM bank_senders WHERE sender = ?", (sender,))
    await conn.commit()


async def senders_of(
    conn: aiosqlite.Connection, user_id: int
) -> list[BankSender]:
    async with conn.execute(
        "SELECT * FROM bank_senders WHERE user_id = ? ORDER BY payments DESC",
        (user_id,),
    ) as cur:
        return [_from_row(BankSender, row) for row in await cur.fetchall()]


async def list_senders(
    conn: aiosqlite.Connection, limit: int = 20
) -> list[BankSender]:
    async with conn.execute(
        "SELECT * FROM bank_senders ORDER BY last_at DESC LIMIT ?", (limit,)
    ) as cur:
        return [_from_row(BankSender, row) for row in await cur.fetchall()]


#: Сколько копеек добавляем к сумме, чтобы платёж стал узнаваемым.
#: Десяти хватает: столкнуться могут только заявки, оказавшиеся в работе
#: одновременно, а не все за историю.
KOPECK_MIN, KOPECK_MAX = 1, 10

#: Если и эти заняты — расширяемся. До тупика дело дойти не должно.
KOPECK_WIDE = 99


async def busy_amounts(conn: aiosqlite.Connection, hours: int = 6) -> set[int]:
    """Суммы, которые прямо сейчас кого-то ждут.

    Только живые заявки. Закрытые и просроченные в расчёт не идут: их
    копейки снова свободны, иначе через месяц свободных не осталось бы.
    """
    edge = (datetime.now(timezone.utc)
            - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
    async with conn.execute(
        "SELECT amount FROM deposits WHERE status = ? AND created_at >= ?",
        (DEP_PENDING, edge),
    ) as cur:
        return {row["amount"] for row in await cur.fetchall()}


async def free_amount(
    conn: aiosqlite.Connection, base: int, hours: int = 6
) -> int:
    """Сумма с уникальным хвостом: base плюс свободные копейки.

    Копейки не случайные, а выбранные из свободных. Случайные могли бы
    совпасть у двоих, и тогда пришлось бы гадать, чей это перевод — ровно
    то, от чего хвост и придуман.

    Сравниваем с ВСЕМИ живыми заявками, а не только с такой же круглой
    суммой: 10.07 столкнулось бы и с базой 10.00, и с базой 10.06.
    """
    busy = await busy_amounts(conn, hours)
    for tail in range(KOPECK_MIN, KOPECK_MAX + 1):
        if base + tail not in busy:
            return base + tail
    for tail in range(KOPECK_MAX + 1, KOPECK_WIDE + 1):
        if base + tail not in busy:
            return base + tail
    # Сто заявок на одну сумму одновременно — такого не бывает, но
    # оставить человека без суммы нельзя: берём как есть.
    return base + KOPECK_MIN


async def pending_near(
    conn: aiosqlite.Connection, amount: int, spread: int = KOPECK_MAX,
    hours: int = 6,
) -> list[Deposit]:
    """Заявки, чья сумма отличается от пришедшей не больше чем на spread.

    Нужно для тех, кто заплатил круглую сумму вместо названной: бот
    просил 10.04, человек по привычке отправил 10.00. Если рядом ждёт
    ровно одна заявка — это она и есть. Если несколько, гадать не станем.
    """
    edge = (datetime.now(timezone.utc)
            - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
    async with conn.execute(
        "SELECT * FROM deposits WHERE status = ? AND created_at >= ? "
        "AND amount BETWEEN ? AND ? ORDER BY id",
        (DEP_PENDING, edge, amount - spread, amount + spread),
    ) as cur:
        return [_from_row(Deposit, row) for row in await cur.fetchall()]


async def set_deposit_screen(
    conn: aiosqlite.Connection, deposit_id: int, chat_id: int, message_id: int
) -> None:
    """Запомнить, где показан экран с реквизитами."""
    await conn.execute(
        "UPDATE deposits SET pay_chat = ?, pay_msg = ? WHERE id = ?",
        (chat_id, message_id, deposit_id),
    )
    await conn.commit()


async def set_deposit_amount(
    conn: aiosqlite.Connection, deposit_id: int, amount: int
) -> bool:
    """Поправить сумму заявки под то, что правда пришло от банка.

    Меняем только у заявки на проверке: у закрытой это переписало бы
    историю, а зачислено там уже другое.
    """
    cur = await conn.execute(
        "UPDATE deposits SET amount = ?, updated_at = ? WHERE id = ? AND status = ?",
        (amount, _now(), deposit_id, DEP_PENDING),
    )
    await conn.commit()
    return cur.rowcount > 0
