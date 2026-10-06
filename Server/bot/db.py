"""Хранилище: SQLite + полнотекстовый поиск FTS5 по русскому тексту."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 4

DIRECTIONS = ("out", "in")
KINDS = ("transfer", "payment", "debt", "income")

ROLES = ("admin", "user")
# invited       — владелец добавил id, человек ещё не заходил
# awaiting_name — нажал Старт, бот ждёт имя
# active        — пользуется
# blocked       — доступ отозван, данные сохранены
STATUSES = ("invited", "awaiting_name", "active", "blocked")

# manual — продиктовал человек; due — денежный срок; goods — жду товар;
# photo — неподписанное фото
REMINDER_KINDS = ("manual", "due", "goods", "photo")
REMINDER_STATUSES = ("pending", "sent", "cancelled")
DEFAULT_REMINDER_HOUR = 9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY,
    owner_id          INTEGER NOT NULL,
    created_at        TEXT    NOT NULL,
    happened_on       TEXT,
    direction         TEXT,
    kind              TEXT,
    counterparty      TEXT,
    counterparty_norm TEXT,
    amount            REAL,
    currency          TEXT,
    item              TEXT,
    quantity          REAL,
    unit              TEXT,
    due_date          TEXT,
    note              TEXT,
    raw_text          TEXT,
    source            TEXT,
    deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_tx_owner_date ON transactions(owner_id, happened_on);
CREATE INDEX IF NOT EXISTS ix_tx_counterparty ON transactions(owner_id, counterparty_norm);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    owner_id       INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    tg_file_id     TEXT    NOT NULL,
    file_path      TEXT    NOT NULL,
    doc_kind       TEXT,
    description    TEXT,
    transaction_id INTEGER REFERENCES transactions(id),
    deleted_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_doc_owner ON documents(owner_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    ts      TEXT    NOT NULL,
    role    TEXT    NOT NULL,
    text    TEXT
);
CREATE INDEX IF NOT EXISTS ix_msg_owner ON messages(owner_id, id);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,   -- Telegram user id
    name          TEXT,                  -- как представился при регистрации
    tg_username   TEXT,                  -- @ник, чтобы узнавать людей в панели
    role          TEXT NOT NULL,         -- admin | user
    status        TEXT NOT NULL,         -- invited | awaiting_name | active | blocked
    invited_at    TEXT NOT NULL,
    registered_at TEXT,
    last_seen_at  TEXT,
    reminder_hour INTEGER,         -- во сколько слать автоматические напоминания
    tz            TEXT             -- свой часовой пояс; пусто — общий из .env
);

CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY,
    owner_id       INTEGER NOT NULL,
    fire_at        TEXT NOT NULL,   -- когда сработать, UTC ISO
    text           TEXT NOT NULL,   -- что показать человеку
    kind           TEXT NOT NULL,   -- manual | due | goods | photo
    transaction_id INTEGER,         -- для kind='due' — чей это срок
    document_id    INTEGER,         -- для kind='photo'
    status         TEXT NOT NULL,   -- pending | sent | cancelled
    created_at     TEXT NOT NULL,
    sent_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_rem_due ON reminders(status, fire_at);
CREATE INDEX IF NOT EXISTS ix_rem_owner ON reminders(owner_id, status, fire_at);

CREATE VIRTUAL TABLE IF NOT EXISTS tx_fts USING fts5(
    counterparty, item, note, raw_text,
    content='transactions', content_rowid='id', tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    description, doc_kind,
    content='documents', content_rowid='id', tokenize='unicode61'
);
"""

# Триггеры держат FTS в синхроне с таблицами — иначе после правки поиск начнёт врать.
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS tx_ai AFTER INSERT ON transactions BEGIN
    INSERT INTO tx_fts(rowid, counterparty, item, note, raw_text)
    VALUES (new.id, new.counterparty, new.item, new.note, new.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS tx_ad AFTER DELETE ON transactions BEGIN
    INSERT INTO tx_fts(tx_fts, rowid, counterparty, item, note, raw_text)
    VALUES ('delete', old.id, old.counterparty, old.item, old.note, old.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS tx_au AFTER UPDATE ON transactions BEGIN
    INSERT INTO tx_fts(tx_fts, rowid, counterparty, item, note, raw_text)
    VALUES ('delete', old.id, old.counterparty, old.item, old.note, old.raw_text);
    INSERT INTO tx_fts(rowid, counterparty, item, note, raw_text)
    VALUES (new.id, new.counterparty, new.item, new.note, new.raw_text);
END;

CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON documents BEGIN
    INSERT INTO doc_fts(rowid, description, doc_kind)
    VALUES (new.id, new.description, new.doc_kind);
END;
CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON documents BEGIN
    INSERT INTO doc_fts(doc_fts, rowid, description, doc_kind)
    VALUES ('delete', old.id, old.description, old.doc_kind);
END;
CREATE TRIGGER IF NOT EXISTS doc_au AFTER UPDATE ON documents BEGIN
    INSERT INTO doc_fts(doc_fts, rowid, description, doc_kind)
    VALUES ('delete', old.id, old.description, old.doc_kind);
    INSERT INTO doc_fts(rowid, description, doc_kind)
    VALUES (new.id, new.description, new.doc_kind);
END;
"""


# ── вспомогательное ────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str | None) -> str:
    """lower + ё→е + схлопнутые пробелы. Для поиска по именам и товарам."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("ё", "е").replace("Ё", "Е").lower()).strip()


_WORD_RE = re.compile(r"[\wЀ-ӿ]+", re.UNICODE)


def fts_query(text: str) -> str:
    """Собрать FTS5-запрос с префиксами: «сумки» → «сумк*».

    Русской морфологии в FTS5 нет, поэтому длинные слова обрезаем до корня —
    так «сумки» находит «сумка», а «обуви» находит «обувь».
    """
    words = [w for w in _WORD_RE.findall(normalize(text)) if len(w) > 2]
    if not words:
        return ""
    parts = []
    for w in words:
        stem = w[:-2] if len(w) > 5 else (w[:-1] if len(w) > 4 else w)
        parts.append(f'"{stem}"*')
    return " OR ".join(parts)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ── подключение ────────────────────────────────────────────────────────────

def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_chat_id_to_owner_id(conn: sqlite3.Connection) -> bool:
    """Схема 1 → 2: chat_id стал owner_id.

    Имя chat_id перестало быть честным, когда бот стал многопользовательским:
    это владелец записи, а не чат. В личной переписке id чата и id человека
    совпадают, поэтому старые записи остаются верными — меняется только название.
    """
    changed = False
    for table in ("transactions", "documents", "messages"):
        if not table_exists(conn, table):
            continue
        cols = columns_of(conn, table)
        if "chat_id" in cols and "owner_id" not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN chat_id TO owner_id")
            changed = True
    # Индексы SQLite переименование переживают, но со старыми именами —
    # убираем их, чтобы не держать по два одинаковых индекса на таблице.
    for index in ("ix_tx_chat_date", "ix_doc_chat", "ix_msg_chat"):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    return changed


def _migrate_add_reminder_hour(conn: sqlite3.Connection) -> None:
    """Схема 2 → 3: у пользователя появилось своё время для напоминаний."""
    if table_exists(conn, "users") and "reminder_hour" not in columns_of(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN reminder_hour INTEGER")


def _migrate_add_user_tz(conn: sqlite3.Connection) -> None:
    """Схема 3 → 4: у каждого человека свой часовой пояс."""
    if table_exists(conn, "users") and "tz" not in columns_of(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN tz TEXT")


def current_version(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return int(row["version"]) if row else 0


def init_schema(conn: sqlite3.Connection) -> None:
    """Создать или обновить схему. Безопасно вызывать сколько угодно раз."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")

    # Миграции — строго до _SCHEMA: индексы в ней ссылаются на новые имена колонок,
    # и на старой базе их создание упало бы с «no such column».
    _migrate_chat_id_to_owner_id(conn)
    _migrate_add_reminder_hour(conn)
    _migrate_add_user_tz(conn)

    conn.executescript(_SCHEMA)
    conn.executescript(_TRIGGERS)

    if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone() is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


# ── журнал сообщений ───────────────────────────────────────────────────────

def log_message(conn: sqlite3.Connection, owner_id: int, role: str, text: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO messages(owner_id, ts, role, text) VALUES (?,?,?,?)",
        (owner_id, now_iso(), role, text),
    )
    conn.commit()
    return int(cur.lastrowid)


# ── операции ───────────────────────────────────────────────────────────────

_TX_FIELDS = (
    "happened_on", "direction", "kind", "counterparty", "amount", "currency",
    "item", "quantity", "unit", "due_date", "note", "raw_text", "source",
)


def add_transaction(conn: sqlite3.Connection, owner_id: int, **fields: Any) -> int:
    data = {k: fields.get(k) for k in _TX_FIELDS}
    cur = conn.execute(
        f"""INSERT INTO transactions
            (owner_id, created_at, counterparty_norm, {', '.join(_TX_FIELDS)})
            VALUES (?,?,?,{','.join('?' * len(_TX_FIELDS))})""",
        (owner_id, now_iso(), normalize(data["counterparty"]), *[data[k] for k in _TX_FIELDS]),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_transaction(conn: sqlite3.Connection, owner_id: int, tx_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM transactions WHERE id=? AND owner_id=? AND deleted_at IS NULL",
        (tx_id, owner_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def update_transaction(conn: sqlite3.Connection, owner_id: int, tx_id: int, **fields: Any) -> bool:
    changes = {k: v for k, v in fields.items() if k in _TX_FIELDS and v is not None}
    if not changes:
        return False
    if "counterparty" in changes:
        changes["counterparty_norm"] = normalize(changes["counterparty"])
    sets = ", ".join(f"{k}=?" for k in changes)
    cur = conn.execute(
        f"UPDATE transactions SET {sets} WHERE id=? AND owner_id=? AND deleted_at IS NULL",
        (*changes.values(), tx_id, owner_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_transaction(conn: sqlite3.Connection, owner_id: int, tx_id: int) -> bool:
    cur = conn.execute(
        "UPDATE transactions SET deleted_at=? WHERE id=? AND owner_id=? AND deleted_at IS NULL",
        (now_iso(), tx_id, owner_id),
    )
    conn.commit()
    return cur.rowcount > 0


def search_transactions(
    conn: sqlite3.Connection,
    owner_id: int,
    *,
    text: str | None = None,
    counterparty: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Найти операции. Возвращает ВЕСЬ список — пользователю нужна история целиком."""
    where = ["t.owner_id = ?", "t.deleted_at IS NULL"]
    args: list[Any] = [owner_id]

    if counterparty:
        where.append("t.counterparty_norm LIKE ?")
        args.append(f"%{normalize(counterparty)}%")
    if date_from:
        where.append("COALESCE(t.happened_on, substr(t.created_at,1,10)) >= ?")
        args.append(date_from)
    if date_to:
        where.append("COALESCE(t.happened_on, substr(t.created_at,1,10)) <= ?")
        args.append(date_to)
    if direction in DIRECTIONS:
        where.append("t.direction = ?")
        args.append(direction)
    if kind in KINDS:
        where.append("t.kind = ?")
        args.append(kind)

    ids: list[int] | None = None
    if text:
        # Шаг 1: FTS5 с префиксами (морфология).
        query = fts_query(text)
        if query:
            try:
                ids = [
                    int(r["rowid"])
                    for r in conn.execute(
                        "SELECT rowid FROM tx_fts WHERE tx_fts MATCH ? ORDER BY rank", (query,)
                    )
                ]
            except sqlite3.OperationalError:
                ids = None
        # Шаг 2: если FTS пусто — запасной LIKE по нормализованному тексту.
        if not ids:
            like = f"%{normalize(text)}%"
            ids = [
                int(r["id"])
                for r in conn.execute(
                    """SELECT id FROM transactions
                       WHERE owner_id=? AND deleted_at IS NULL AND (
                             lower(COALESCE(item,'')) LIKE ?
                          OR lower(COALESCE(note,'')) LIKE ?
                          OR lower(COALESCE(raw_text,'')) LIKE ?
                          OR counterparty_norm LIKE ?)""",
                    (owner_id, like, like, like, like),
                )
            ]
        if not ids:
            return []
        where.append(f"t.id IN ({','.join('?' * len(ids))})")
        args.extend(ids)

    rows = conn.execute(
        f"""SELECT t.* FROM transactions t
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(t.happened_on, substr(t.created_at,1,10)) DESC, t.id DESC
            LIMIT ?""",
        (*args, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def totals_by_currency(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Итоги отдельно по каждой валюте — конвертации нет по решению пользователя."""
    totals: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("amount") is None:
            continue
        cur = (r.get("currency") or "?").upper()
        bucket = totals.setdefault(cur, {"out": 0.0, "in": 0.0, "count": 0})
        key = "in" if r.get("direction") == "in" else "out"
        bucket[key] += float(r["amount"])
        bucket["count"] += 1
    return totals


# ── документы (фото накладных) ─────────────────────────────────────────────

def add_document(
    conn: sqlite3.Connection,
    owner_id: int,
    *,
    tg_file_id: str,
    file_path: str,
    doc_kind: str | None = None,
    description: str | None = None,
    transaction_id: int | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO documents
           (owner_id, created_at, tg_file_id, file_path, doc_kind, description, transaction_id)
           VALUES (?,?,?,?,?,?,?)""",
        (owner_id, now_iso(), tg_file_id, file_path, doc_kind, description, transaction_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def describe_document(
    conn: sqlite3.Connection,
    owner_id: int,
    doc_id: int,
    *,
    description: str,
    doc_kind: str | None = None,
    transaction_id: int | None = None,
) -> bool:
    sets = ["description=?"]
    args: list[Any] = [description]
    if doc_kind:
        sets.append("doc_kind=?")
        args.append(doc_kind)
    if transaction_id:
        sets.append("transaction_id=?")
        args.append(transaction_id)
    cur = conn.execute(
        f"UPDATE documents SET {', '.join(sets)} WHERE id=? AND owner_id=? AND deleted_at IS NULL",
        (*args, doc_id, owner_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_document(conn: sqlite3.Connection, owner_id: int, doc_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE id=? AND owner_id=? AND deleted_at IS NULL",
        (doc_id, owner_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def pending_documents(conn: sqlite3.Connection, owner_id: int, limit: int = 3) -> list[dict[str, Any]]:
    """Фото без описания — их пользователь ещё не подписал."""
    rows = conn.execute(
        """SELECT * FROM documents
           WHERE owner_id=? AND deleted_at IS NULL
             AND (description IS NULL OR description='')
           ORDER BY id DESC LIMIT ?""",
        (owner_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_documents(
    conn: sqlite3.Connection,
    owner_id: int,
    *,
    text: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    where = ["d.owner_id = ?", "d.deleted_at IS NULL"]
    args: list[Any] = [owner_id]

    if date_from:
        where.append("substr(d.created_at,1,10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(d.created_at,1,10) <= ?")
        args.append(date_to)

    if text:
        ids: list[int] = []
        query = fts_query(text)
        if query:
            try:
                ids = [
                    int(r["rowid"])
                    for r in conn.execute(
                        "SELECT rowid FROM doc_fts WHERE doc_fts MATCH ? ORDER BY rank", (query,)
                    )
                ]
            except sqlite3.OperationalError:
                ids = []
        if not ids:
            like = f"%{normalize(text)}%"
            ids = [
                int(r["id"])
                for r in conn.execute(
                    """SELECT id FROM documents
                       WHERE owner_id=? AND deleted_at IS NULL AND (
                             lower(COALESCE(description,'')) LIKE ?
                          OR lower(COALESCE(doc_kind,'')) LIKE ?)""",
                    (owner_id, like, like),
                )
            ]
        if not ids:
            return []
        where.append(f"d.id IN ({','.join('?' * len(ids))})")
        args.extend(ids)

    rows = conn.execute(
        f"SELECT d.* FROM documents d WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── пользователи ───────────────────────────────────────────────────────────

def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row) if row else None


def invite_user(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    role: str = "user",
    name: str | None = None,
) -> bool:
    """Дать доступ по id. Возвращает False, если такой человек уже заведён."""
    if get_user(conn, user_id):
        return False
    conn.execute(
        """INSERT INTO users (id, name, role, status, invited_at)
           VALUES (?,?,?,?,?)""",
        (user_id, name, role if role in ROLES else "user", "invited", now_iso()),
    )
    conn.commit()
    return True


def ensure_admin(conn: sqlite3.Connection, user_id: int) -> None:
    """Завести владельца из .env. Уже заведённого — поднять до админа.

    Нужно при переходе со старой однопользовательской версии: у владельца уже
    есть записи, и он не должен потерять к ним доступ.
    """
    user = get_user(conn, user_id)
    if user is None:
        # Сразу active, а не invited: владелец сам вписал свой id в .env — это и
        # есть разрешение. Заставлять его «регистрироваться» в собственном боте,
        # где у него уже лежат записи, было бы странно.
        conn.execute(
            """INSERT INTO users (id, role, status, invited_at, registered_at)
               VALUES (?,?,?,?,?)""",
            (user_id, "admin", "active", now_iso(), now_iso()),
        )
    elif user["role"] != "admin" or user["status"] == "blocked":
        conn.execute(
            "UPDATE users SET role='admin', status=CASE WHEN status='blocked' THEN 'active' ELSE status END WHERE id=?",
            (user_id,),
        )
    conn.commit()


def start_registration(conn: sqlite3.Connection, user_id: int, tg_username: str | None) -> None:
    """Человек нажал Старт — ждём имя. Состояние в базе, чтобы пережить рестарт."""
    conn.execute(
        "UPDATE users SET status='awaiting_name', tg_username=? WHERE id=?",
        (tg_username, user_id),
    )
    conn.commit()


def register_user(conn: sqlite3.Connection, user_id: int, name: str) -> bool:
    cur = conn.execute(
        """UPDATE users SET name=?, status='active', registered_at=COALESCE(registered_at, ?)
           WHERE id=?""",
        (name, now_iso(), user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def rename_user(conn: sqlite3.Connection, user_id: int, name: str) -> bool:
    cur = conn.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))
    conn.commit()
    return cur.rowcount > 0


def set_status(conn: sqlite3.Connection, user_id: int, status: str) -> bool:
    if status not in STATUSES:
        return False
    cur = conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    conn.commit()
    return cur.rowcount > 0


def touch_last_seen(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now_iso(), user_id))
    conn.commit()


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Все пользователи: сначала админы, потом по времени добавления."""
    rows = conn.execute(
        """SELECT * FROM users
           ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, invited_at, id"""
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_admins(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND status != 'blocked'"
    ).fetchone()
    return int(row["n"])


def user_stats(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """Сколько у человека записей. Сами операции админу не показываем."""
    transactions = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE owner_id=? AND deleted_at IS NULL",
        (user_id,),
    ).fetchone()["n"]
    documents = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE owner_id=? AND deleted_at IS NULL",
        (user_id,),
    ).fetchone()["n"]
    return {"transactions": int(transactions), "documents": int(documents)}


def delete_user(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """Удалить человека вместе со всеми его данными. Необратимо.

    Возвращает, сколько чего удалено — чтобы показать это в подтверждении.
    """
    stats = user_stats(conn, user_id)
    files = [
        row["file_path"]
        for row in conn.execute("SELECT file_path FROM documents WHERE owner_id=?", (user_id,))
    ]
    conn.execute("DELETE FROM documents WHERE owner_id=?", (user_id,))
    conn.execute("DELETE FROM transactions WHERE owner_id=?", (user_id,))
    conn.execute("DELETE FROM messages WHERE owner_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()

    for path in files:  # фото на диске тоже наши, оставлять их незачем
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    return stats


# ── напоминания ────────────────────────────────────────────────────────────

def add_reminder(
    conn: sqlite3.Connection,
    owner_id: int,
    *,
    fire_at: str,
    text: str,
    kind: str = "manual",
    transaction_id: int | None = None,
    document_id: int | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO reminders
           (owner_id, fire_at, text, kind, transaction_id, document_id, status, created_at)
           VALUES (?,?,?,?,?,?, 'pending', ?)""",
        (owner_id, fire_at, text,
         kind if kind in REMINDER_KINDS else "manual",
         transaction_id, document_id, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_reminder(conn: sqlite3.Connection, reminder_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    return _row_to_dict(row) if row else None


def due_reminders(conn: sqlite3.Connection, now: str, limit: int = 100) -> list[dict[str, Any]]:
    """Что пора отправить. now — момент в UTC ISO."""
    rows = conn.execute(
        """SELECT * FROM reminders
           WHERE status='pending' AND fire_at <= ?
           ORDER BY fire_at LIMIT ?""",
        (now, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_reminders(
    conn: sqlite3.Connection, owner_id: int, *, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM reminders
           WHERE owner_id=? AND status=? ORDER BY fire_at LIMIT ?""",
        (owner_id, status, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_sent(conn: sqlite3.Connection, reminder_id: int) -> None:
    conn.execute(
        "UPDATE reminders SET status='sent', sent_at=? WHERE id=?", (now_iso(), reminder_id)
    )
    conn.commit()


def cancel_reminder(conn: sqlite3.Connection, owner_id: int, reminder_id: int) -> bool:
    cur = conn.execute(
        "UPDATE reminders SET status='cancelled' WHERE id=? AND owner_id=? AND status='pending'",
        (reminder_id, owner_id),
    )
    conn.commit()
    return cur.rowcount > 0


def reschedule_reminder(conn: sqlite3.Connection, reminder_id: int, fire_at: str) -> bool:
    """Отложить: снова pending на новый момент."""
    cur = conn.execute(
        "UPDATE reminders SET fire_at=?, status='pending', sent_at=NULL WHERE id=?",
        (fire_at, reminder_id),
    )
    conn.commit()
    return cur.rowcount > 0


def cancel_reminders_for_transaction(
    conn: sqlite3.Connection, owner_id: int, transaction_id: int
) -> int:
    """Операцию изменили или удалили — старые напоминания о её сроке не нужны."""
    cur = conn.execute(
        """UPDATE reminders SET status='cancelled'
           WHERE owner_id=? AND transaction_id=? AND status='pending' AND kind='due'""",
        (owner_id, transaction_id),
    )
    conn.commit()
    return cur.rowcount


def has_pending_reminder(
    conn: sqlite3.Connection, owner_id: int, kind: str, *, document_id: int | None = None
) -> bool:
    """Есть ли уже такое напоминание — чтобы не плодить дубли каждый день."""
    if document_id is not None:
        row = conn.execute(
            """SELECT 1 FROM reminders WHERE owner_id=? AND kind=? AND document_id=?
               AND status='pending' LIMIT 1""",
            (owner_id, kind, document_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE owner_id=? AND kind=? AND status='pending' LIMIT 1",
            (owner_id, kind),
        ).fetchone()
    return row is not None


def set_reminder_hour(conn: sqlite3.Connection, user_id: int, hour: int) -> bool:
    if not 0 <= hour <= 23:
        return False
    cur = conn.execute("UPDATE users SET reminder_hour=? WHERE id=?", (hour, user_id))
    conn.commit()
    return cur.rowcount > 0


def reminder_hour(conn: sqlite3.Connection, user_id: int) -> int:
    user = get_user(conn, user_id)
    hour = user.get("reminder_hour") if user else None
    return int(hour) if hour is not None else DEFAULT_REMINDER_HOUR


def set_user_tz(conn: sqlite3.Connection, user_id: int, tz_name: str) -> bool:
    """Запомнить пояс. Неизвестное имя не принимаем — иначе напоминания уедут."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    cur = conn.execute("UPDATE users SET tz=? WHERE id=?", (tz_name, user_id))
    conn.commit()
    return cur.rowcount > 0


def user_tz_name(conn: sqlite3.Connection, user_id: int, fallback: str) -> str:
    user = get_user(conn, user_id)
    return (user.get("tz") if user else None) or fallback


def user_tz(conn: sqlite3.Connection, user_id: int, fallback: Any):
    """Пояс человека как ZoneInfo. Битое имя в базе не должно ронять бота."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if isinstance(fallback, str):
        try:
            fallback = ZoneInfo(fallback)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            fallback = ZoneInfo("UTC")

    user = get_user(conn, user_id)
    name = user.get("tz") if user else None
    if not name:
        return fallback
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log_tz_problem(user_id, name)
        return fallback


def log_tz_problem(user_id: int, name: str) -> None:
    import logging

    logging.getLogger(__name__).warning(
        "У пользователя %s непонятный часовой пояс %r — беру общий", user_id, name
    )


def undescribed_documents(
    conn: sqlite3.Connection, older_than_iso: str
) -> list[dict[str, Any]]:
    """Фото, которые человек прислал и забыл подписать."""
    rows = conn.execute(
        """SELECT * FROM documents
           WHERE deleted_at IS NULL AND (description IS NULL OR description='')
             AND created_at <= ?
           ORDER BY id""",
        (older_than_iso,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def transactions_with_due_date(
    conn: sqlite3.Connection, owner_id: int, *, since: str | None = None
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM transactions
           WHERE owner_id=? AND deleted_at IS NULL AND due_date IS NOT NULL
             AND (? IS NULL OR due_date >= ?)
           ORDER BY due_date""",
        (owner_id, since, since),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]
