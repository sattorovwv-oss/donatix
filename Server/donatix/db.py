"""SQLite: схема и подключение. Каждый запрос/поток открывает своё соединение.

Деньги — целые числа в «микро» (см. money.py). Все изменения баланса идут
в одной транзакции с записью в журнал transactions."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    login           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'client',   -- client | admin
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | active | blocked
    tier            TEXT NOT NULL DEFAULT 'bronze',   -- bronze | silver | gold
    markup_override TEXT,                             -- своя наценка в %, если задана
    balance_micro   INTEGER NOT NULL DEFAULT 0,
    webhook_url     TEXT,
    webhook_secret  TEXT,
    project         TEXT,             -- о проекте клиента, из заявки
    created_at      TEXT NOT NULL,
    last_active_at  TEXT
);

CREATE TABLE IF NOT EXISTS bots (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),   -- чей баланс в Donatix тратит бот
    token_enc    TEXT NOT NULL,                           -- токен бота, зашифрован ключом сайта
    username     TEXT,
    admin_ids    TEXT NOT NULL,                           -- Telegram ID админов бота через запятую
    api_key_id   INTEGER REFERENCES api_keys(id),
    key_enc      TEXT,                                    -- API-ключ Donatix для бота, зашифрован
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logins (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    ip         TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logins_user ON logins(user_id, id);
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    key_enc      TEXT,              -- ключ, зашифрованный ключом сайта (чтобы показать клиенту)
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,     -- telegram_stars | telegram_premium | topup | gift_card
    category_id       TEXT NOT NULL,
    category_name     TEXT NOT NULL,
    name              TEXT NOT NULL,
    base_price        TEXT NOT NULL,     -- закупка за единицу, Decimal строкой
    unit              TEXT NOT NULL DEFAULT 'item',
    min_qty           INTEGER NOT NULL DEFAULT 1,
    max_qty           INTEGER NOT NULL DEFAULT 1,
    stock             INTEGER,           -- NULL — без ограничения
    image_url         TEXT,              -- обложка из каталога поставщика
    region            TEXT,              -- код региона (TR, GLOBAL…), если есть
    fields_json       TEXT NOT NULL DEFAULT '[]',
    supplier_ref_json TEXT NOT NULL DEFAULT '{}',
    active            INTEGER NOT NULL DEFAULT 1,
    hidden            INTEGER NOT NULL DEFAULT 0,  -- скрыт админом
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS products_kind ON products(kind, active);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY,
    public_id         TEXT UNIQUE,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    product_id        TEXT NOT NULL,
    kind              TEXT NOT NULL,
    product_name      TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    fields_json       TEXT NOT NULL DEFAULT '{}',
    unit_price        TEXT NOT NULL,
    total_micro       INTEGER NOT NULL,
    cost_micro        INTEGER NOT NULL,
    status            TEXT NOT NULL,     -- processing | completed | failed | attention
    supplier_idem_key TEXT NOT NULL UNIQUE,
    supplier_order_id TEXT,
    supplier_status   TEXT,
    supplier_attempts INTEGER NOT NULL DEFAULT 0,
    idempotent_supply INTEGER NOT NULL DEFAULT 1,
    delivery_json     TEXT,
    error             TEXT,
    client_idem_key   TEXT,
    source            TEXT NOT NULL DEFAULT 'api',   -- api | panel
    webhook_state     TEXT NOT NULL DEFAULT 'none',  -- none | pending | sent | failed
    webhook_attempts  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    UNIQUE (user_id, client_idem_key)
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, id DESC);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    type                TEXT NOT NULL,     -- credit | debit
    amount_micro        INTEGER NOT NULL,  -- со знаком
    balance_before      INTEGER NOT NULL,
    balance_after       INTEGER NOT NULL,
    note                TEXT NOT NULL,
    order_id            INTEGER REFERENCES orders(id),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tx_user ON transactions(user_id, id DESC);

CREATE TABLE IF NOT EXISTS steam_gift_games (
    appid   INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    name_lc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    method        TEXT NOT NULL,
    amount_micro  INTEGER NOT NULL,             -- сколько зачислить, USD
    pay_amount    TEXT NOT NULL,                -- сколько перевести, в валюте способа
    pay_currency  TEXT NOT NULL,
    reference     TEXT,                         -- что указал клиент: номер чека, хэш и т.п.
    receipt_file  TEXT,                         -- фото/PDF чека в data/receipts
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | rejected | cancelled
    admin_note    TEXT,
    tx_id         INTEGER REFERENCES transactions(id),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS payments_status ON payments(status, id DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    text       TEXT NOT NULL,
    link       TEXT,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, id DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Посещаемость сайта: одна строка — один просмотр страницы (см. traffic.py)
CREATE TABLE IF NOT EXISTS visits (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,        -- UTC, YYYY-MM-DDTHH:MM:SS
    visitor      TEXT NOT NULL,        -- случайный id из cookie, не личные данные
    session      TEXT NOT NULL,        -- визит: обрывается после 30 минут тишины
    user_id      INTEGER,
    path         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',   -- откуда пришёл визит (Google, Telegram, …)
    referrer     TEXT,
    utm_source   TEXT,
    utm_medium   TEXT,
    utm_campaign TEXT,
    device       TEXT,
    browser      TEXT,
    os           TEXT,
    is_new       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS visits_ts ON visits(ts);

-- Бот поддержки: чей это Telegram, коды входа, переписка, обращения к админу
CREATE TABLE IF NOT EXISTS support_links (
    tg_id     INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id),
    linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_codes (
    tg_id      INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    tries      INTEGER NOT NULL DEFAULT 0,
    sent       INTEGER NOT NULL DEFAULT 1,
    sent_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS support_link_codes (
    code_hash  TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS support_history (
    id         INTEGER PRIMARY KEY,
    tg_id      INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS support_history_tg ON support_history(tg_id, id);
-- Долгая память бота поддержки о клиенте (как в ChatGPT): факты, которые AI решил запомнить
CREATE TABLE IF NOT EXISTS support_memory (
    id         INTEGER PRIMARY KEY,
    tg_id      INTEGER NOT NULL,
    fact       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS support_memory_tg ON support_memory(tg_id, id);
-- Чему админ научил бота командой /learn — важнее общей базы знаний
CREATE TABLE IF NOT EXISTS support_knowledge (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_tickets (
    id         INTEGER PRIMARY KEY,
    tg_id      INTEGER NOT NULL,
    user_id    INTEGER,
    summary    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referral_rewards (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL UNIQUE REFERENCES orders(id),   -- за один заказ — один раз
    referrer_id  INTEGER NOT NULL REFERENCES users(id),
    referred_id  INTEGER NOT NULL REFERENCES users(id),
    amount_micro INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS referral_rewards_referrer ON referral_rewards(referrer_id, id DESC);
-- Кассиры: проверяют чеки по своим банкам в Telegram и получают долю прибыли
CREATE TABLE IF NOT EXISTS cashiers (
    tg_id      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    methods    TEXT NOT NULL DEFAULT '*',   -- '*' — все банки, иначе JSON-список кодов способов оплаты
    percent    INTEGER NOT NULL DEFAULT 20,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- Куда ушёл чек (админу и кассирам): после решения правим все копии, чтобы не приняли дважды
CREATE TABLE IF NOT EXISTS payment_msgs (
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    chat_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    caption    TEXT NOT NULL,
    PRIMARY KEY (payment_id, chat_id, message_id)
);
-- D-коин: монеты за покупки и копилка из части прибыли (см. dcoin.py)
CREATE TABLE IF NOT EXISTS dcoin_ledger (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    amount     INTEGER NOT NULL,            -- сотые доли монеты; минус — обмен
    pool_micro INTEGER NOT NULL DEFAULT 0,  -- сколько добавилось в копилку (минус — выплачено)
    reason     TEXT NOT NULL,
    order_id   INTEGER UNIQUE REFERENCES orders(id),   -- за один заказ — один раз
    pending    INTEGER NOT NULL DEFAULT 0,  -- 1 — заказ ещё выполняется, 2 — не выполнен (монеты списаны)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dcoin_ledger_user ON dcoin_ledger(user_id, id DESC);
-- Точки графика: состояние копилки и монет после каждой покупки/обмена
CREATE TABLE IF NOT EXISTS dcoin_points (
    id     INTEGER PRIMARY KEY,
    ts     TEXT NOT NULL,
    pool   INTEGER NOT NULL,
    supply INTEGER NOT NULL,
    reserve INTEGER NOT NULL DEFAULT 0,   -- стартовый запас сайта (ни у кого на руках)
    price  REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dcoin_points_ts ON dcoin_points(ts);
CREATE INDEX IF NOT EXISTS visits_session ON visits(session, ts);
CREATE INDEX IF NOT EXISTS visits_user ON visits(user_id) WHERE user_id IS NOT NULL;
-- Быстрые отчёты: продажи/деньги за период, посещаемость без чтения всей таблицы
CREATE INDEX IF NOT EXISTS products_cat ON products(category_id, active, hidden);
CREATE INDEX IF NOT EXISTS orders_user_status ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS orders_status_created ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS payments_resolved ON payments(status, resolved_at);
CREATE INDEX IF NOT EXISTS users_role_created ON users(role, created_at);
CREATE INDEX IF NOT EXISTS tx_created ON transactions(created_at);
CREATE INDEX IF NOT EXISTS visits_ts_cover ON visits(ts, visitor, session, user_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Соединение живёт в пределах одного запроса, но FastAPI может открыть и закрыть
    # его в разных потоках — поэтому check_same_thread=False.
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    # Скорость: в режиме WAL «NORMAL» не ждёт записи на диск после каждой мелкой операции
    # (база остаётся целой; при внезапном отключении питания могут пропасть последние
    # доли секунды). Временные таблицы — в памяти, файл базы читается через mmap.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


class Pool:
    """Готовые соединения с базой для запросов сайта.

    Открывать SQLite на каждый запрос (файл + настройки) стоило четверть процессорного
    времени сервера. Соединение берётся из пула на время запроса и возвращается обратно;
    одновременно им пользуется только один запрос. Недописанная транзакция при возврате
    откатывается, чтобы следующий запрос начал с чистого листа.
    """

    def __init__(self, path: Path | str, keep: int = 32):
        self.path = Path(path)
        self.keep = keep
        self._free: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def acquire(self) -> sqlite3.Connection:
        with self._lock:
            if self._free:
                return self._free.pop()
        return connect(self.path)

    def release(self, conn: sqlite3.Connection) -> None:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            conn.close()
            return
        with self._lock:
            if len(self._free) < self.keep:
                self._free.append(conn)
                return
        conn.close()


_pools: dict[str, Pool] = {}
_pools_lock = threading.Lock()


def pool(path: Path | str) -> Pool:
    key = str(Path(path).resolve())
    with _pools_lock:
        p = _pools.get(key)
        if p is None:
            p = _pools[key] = Pool(path)
        return p


def init(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        if "image_url" not in cols:  # база, созданная до появления картинок
            conn.execute("ALTER TABLE products ADD COLUMN image_url TEXT")
        pay_cols = {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
        if "receipt_file" not in pay_cols:
            conn.execute("ALTER TABLE payments ADD COLUMN receipt_file TEXT")
        order_cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "api_key_id" not in order_cols:  # какой ключ (бот) сделал заказ — для учёта продаж бота
            conn.execute("ALTER TABLE orders ADD COLUMN api_key_id INTEGER")
        bot_cols = {r[1] for r in conn.execute("PRAGMA table_info(bots)")}
        for col, kind in (("warn_count", "INTEGER NOT NULL DEFAULT 0"), ("last_warn_at", "TEXT"),
                          ("active_since", "TEXT"), ("disabled_reason", "TEXT")):
            if col not in bot_cols:  # неактивные боты: предупреждения и автоотключение
                conn.execute(f"ALTER TABLE bots ADD COLUMN {col} {kind}")
        for col in ("auto_kind", "ext_id", "pay_url", "pay_address"):  # автоплатёж: TRC20 / Binance Pay
            if col not in pay_cols:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {col} TEXT")
        key_cols = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)")}
        if "key_enc" not in key_cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN key_enc TEXT")
        if "region" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN region TEXT")
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "ref_code" not in user_cols:   # реферальная программа
            conn.execute("ALTER TABLE users ADD COLUMN ref_code TEXT")
            conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_ref_code ON users(ref_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS users_referred_by ON users(referred_by)")
        if "resolved_tg" not in pay_cols:   # кто решил заявку в Telegram (кассир)
            conn.execute("ALTER TABLE payments ADD COLUMN resolved_tg TEXT")
        if "receipt_hash" not in pay_cols:  # один чек — одна заявка: тот же файл второй раз не примем
            conn.execute("ALTER TABLE payments ADD COLUMN receipt_hash TEXT")
        if "boosted_at" not in pay_cols:   # клиент нажал «Ускорить» — когда (не чаще раза в 10 минут)
            conn.execute("ALTER TABLE payments ADD COLUMN boosted_at TEXT")
        for col in ("receipt_ai", "receipt_txn", "receipt_fp"):   # что прочитал ИИ в чеке — от повторных чеков
            if col not in pay_cols:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {col} TEXT")
        for col in ("receipt_txn", "receipt_fp"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS payments_{col} ON payments({col}) WHERE {col} IS NOT NULL")
        if "resolved_who" not in pay_cols:  # кто решил: «кассир Али», «админ (сайт)», «автоматически»…
            conn.execute("ALTER TABLE payments ADD COLUMN resolved_who TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS payments_receipt_hash ON payments(receipt_hash) "
                     "WHERE receipt_hash IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id, status)")
        ledger_cols = {r[1] for r in conn.execute("PRAGMA table_info(dcoin_ledger)")}
        if "pending" not in ledger_cols:
            conn.execute("ALTER TABLE dcoin_ledger ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        point_cols = {r[1] for r in conn.execute("PRAGMA table_info(dcoin_points)")}
        if "reserve" not in point_cols:
            conn.execute("ALTER TABLE dcoin_points ADD COLUMN reserve INTEGER NOT NULL DEFAULT 0")
        if "fraud_count" not in user_cols:   # сколько раз присылал поддельный чек
            conn.execute("ALTER TABLE users ADD COLUMN fraud_count INTEGER NOT NULL DEFAULT 0")
        if "dcoin" not in user_cols:   # D-коины клиента, сотые доли
            conn.execute("ALTER TABLE users ADD COLUMN dcoin INTEGER NOT NULL DEFAULT 0")
        if "google_sub" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users(google_sub)")
        conn.executescript("""
CREATE TABLE IF NOT EXISTS shop_users (       -- покупатели из бота-магазина
    tg_id       INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT,
    lang        TEXT NOT NULL DEFAULT '',
    saved_json  TEXT,                           -- сохранённые ID игроков по играм
    start_param TEXT,                           -- откуда пришёл: ссылка ?start=
    subscribed  INTEGER NOT NULL DEFAULT 1,     -- получает новости и скидки
    created_at  TEXT NOT NULL,
    last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS shop_watch (       -- заказы и пополнения, о которых бот напишет покупателю
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    obj_id      INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    last_status TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_source ON orders(source, status);
CREATE TABLE IF NOT EXISTS player_names (     -- ник по ID игрока: проверили раз — помним
    game        TEXT NOT NULL,
    uid         TEXT NOT NULL,
    server      TEXT NOT NULL DEFAULT '',
    valid       INTEGER NOT NULL,
    player_name TEXT,
    region      TEXT,
    strict      INTEGER NOT NULL DEFAULT 1,
    checked_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (game, uid, server)
);
CREATE TABLE IF NOT EXISTS shop_names (       -- витрина бота: свои надписи и скрытые игры/пакеты
    key    TEXT PRIMARY KEY,                    -- g:<игра> или p:<id товара>
    title  TEXT,
    hidden INTEGER NOT NULL DEFAULT 0
);
""")
        conn.executescript("""
CREATE TABLE IF NOT EXISTS push_subs (          -- push-уведомления: подписка каждого устройства
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    endpoint   TEXT NOT NULL UNIQUE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    ua         TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS push_subs_user ON push_subs(user_id);
""")
        watch_cols = {r[1] for r in conn.execute("PRAGMA table_info(shop_watch)")}
        if "intent" not in watch_cols:   # что покупатель выбрал до пополнения — купим сами, когда деньги придут
            conn.execute("ALTER TABLE shop_watch ADD COLUMN intent TEXT")
        shop_cols = {r[1] for r in conn.execute("PRAGMA table_info(shop_users)")}
        if "username" not in shop_cols:   # @username покупателя — найти его в админ-боте
            conn.execute("ALTER TABLE shop_users ADD COLUMN username TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS shop_users_username ON shop_users(lower(username))")
        login_cols = {r[1] for r in conn.execute("PRAGMA table_info(logins)")}
        if "sid" not in login_cols:   # номер сессии: выход или блок обрывают именно её, украденный cookie не живёт
            conn.execute("ALTER TABLE logins ADD COLUMN sid TEXT")
            conn.execute("ALTER TABLE logins ADD COLUMN ended_at TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS logins_sid ON logins(sid) WHERE sid IS NOT NULL")
        conn.execute("PRAGMA optimize")   # статистика для планировщика запросов — чтобы брал индексы
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Транзакция с немедленной блокировкой записи: два заказа одного клиента
    не прочитают один и тот же баланс."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
