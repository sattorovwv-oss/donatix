"""Пользователи, миграция старой базы и разделение данных между людьми."""

import sqlite3

import pytest

from bot import db

OWNER, SALIM, KARIM = 111, 222, 333


# ── миграция со старой однопользовательской версии ─────────────────────────

OLD_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (1);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    happened_on TEXT, direction TEXT, kind TEXT, counterparty TEXT,
    counterparty_norm TEXT, amount REAL, currency TEXT, item TEXT, quantity REAL,
    unit TEXT, due_date TEXT, note TEXT, raw_text TEXT, source TEXT, deleted_at TEXT);
CREATE INDEX ix_tx_chat_date ON transactions(chat_id, happened_on);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    tg_file_id TEXT NOT NULL, file_path TEXT NOT NULL, doc_kind TEXT,
    description TEXT, transaction_id INTEGER, deleted_at TEXT);
CREATE INDEX ix_doc_chat ON documents(chat_id, created_at);
CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
    ts TEXT NOT NULL, role TEXT NOT NULL, text TEXT);
CREATE INDEX ix_msg_chat ON messages(chat_id, id);
CREATE VIRTUAL TABLE tx_fts USING fts5(counterparty, item, note, raw_text,
    content='transactions', content_rowid='id', tokenize='unicode61');
CREATE VIRTUAL TABLE doc_fts USING fts5(description, doc_kind,
    content='documents', content_rowid='id', tokenize='unicode61');
"""


@pytest.fixture
def old_db(tmp_path):
    """База старой версии с настоящими записями владельца."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.execute(
        """INSERT INTO transactions (chat_id, created_at, happened_on, counterparty,
           counterparty_norm, amount, currency, item, direction)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (OWNER, "2026-08-20T00:00:00", "2026-08-20", "Абубакр", "абубакр",
         500000, "KZT", "сумки", "out"),
    )
    old.execute("INSERT INTO tx_fts(rowid, counterparty, item, note, raw_text) "
                "VALUES (1,'Абубакр','сумки',NULL,NULL)")
    old.execute(
        """INSERT INTO documents (chat_id, created_at, tg_file_id, file_path, description)
           VALUES (?,?,?,?,?)""",
        (OWNER, "2026-08-21T00:00:00", "f1", "/tmp/a.jpg", "накладная на женскую обувь"),
    )
    old.execute("INSERT INTO doc_fts(rowid, description, doc_kind) "
                "VALUES (1,'накладная на женскую обувь',NULL)")
    old.commit()
    old.close()
    return path


def test_migration_keeps_the_owners_records(old_db):
    """Главное при переходе: владелец не должен потерять то, что уже записал."""
    conn = db.connect(old_db)
    assert db.current_version(conn) == db.SCHEMA_VERSION

    found = db.search_transactions(conn, OWNER, text="сумки")
    assert len(found) == 1
    assert found[0]["amount"] == 500000
    assert found[0]["counterparty"] == "Абубакр"

    docs = db.search_documents(conn, OWNER, text="накладная от женской обуви")
    assert len(docs) == 1
    conn.close()


def test_migration_renames_the_column(old_db):
    conn = db.connect(old_db)
    for table in ("transactions", "documents", "messages"):
        columns = db.columns_of(conn, table)
        assert "owner_id" in columns, table
        assert "chat_id" not in columns, table
    conn.close()


def test_migration_drops_the_old_indexes(old_db):
    """Иначе на таблице висели бы два одинаковых индекса."""
    conn = db.connect(old_db)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert not {"ix_tx_chat_date", "ix_doc_chat", "ix_msg_chat"} & names
    conn.close()


def test_migration_can_run_twice(old_db):
    """Бот перезапускают часто — миграция обязана быть безобидной при повторе."""
    db.connect(old_db).close()
    conn = db.connect(old_db)
    assert db.current_version(conn) == db.SCHEMA_VERSION
    assert len(db.search_transactions(conn, OWNER)) == 1
    conn.close()


def test_fresh_database_needs_no_migration(tmp_path):
    conn = db.connect(tmp_path / "new.db")
    assert db.current_version(conn) == db.SCHEMA_VERSION
    assert "owner_id" in db.columns_of(conn, "transactions")
    conn.close()


# ── у каждого свой учёт ────────────────────────────────────────────────────

def test_two_people_do_not_see_each_others_records(conn):
    db.add_transaction(conn, OWNER, item="сумки", amount=500000, currency="KZT",
                       counterparty="Абубакр", happened_on="2026-08-20")
    db.add_transaction(conn, SALIM, item="сумки", amount=999, currency="TJS",
                       counterparty="Абубакр", happened_on="2026-08-20")

    mine = db.search_transactions(conn, OWNER, text="сумки")
    his = db.search_transactions(conn, SALIM, text="сумки")

    assert [r["amount"] for r in mine] == [500000]
    assert [r["amount"] for r in his] == [999]


def test_search_by_name_does_not_leak_across_people(conn):
    db.add_transaction(conn, OWNER, counterparty="Абубакр", amount=1, currency="TJS")
    db.add_transaction(conn, SALIM, counterparty="Абубакр", amount=2, currency="TJS")
    assert len(db.search_transactions(conn, OWNER, counterparty="Абубакр")) == 1


def test_documents_are_separate_too(conn):
    mine = db.add_document(conn, OWNER, tg_file_id="a", file_path="/tmp/a.jpg")
    db.describe_document(conn, OWNER, mine, description="накладная на обувь")
    his = db.add_document(conn, SALIM, tg_file_id="b", file_path="/tmp/b.jpg")
    db.describe_document(conn, SALIM, his, description="накладная на обувь")

    assert [d["id"] for d in db.search_documents(conn, OWNER, text="обувь")] == [mine]
    assert [d["id"] for d in db.search_documents(conn, SALIM, text="обувь")] == [his]


def test_one_cannot_delete_anothers_record(conn):
    tx_id = db.add_transaction(conn, OWNER, amount=100, currency="TJS")
    assert db.delete_transaction(conn, SALIM, tx_id) is False
    assert db.get_transaction(conn, OWNER, tx_id) is not None


def test_one_cannot_edit_anothers_record(conn):
    tx_id = db.add_transaction(conn, OWNER, amount=100, currency="TJS")
    assert db.update_transaction(conn, SALIM, tx_id, amount=1) is False
    assert db.get_transaction(conn, OWNER, tx_id)["amount"] == 100


# ── заведение и состояния ──────────────────────────────────────────────────

def test_invite_then_register(conn):
    assert db.invite_user(conn, SALIM) is True
    assert db.get_user(conn, SALIM)["status"] == "invited"

    db.start_registration(conn, SALIM, "salim")
    assert db.get_user(conn, SALIM)["status"] == "awaiting_name"

    assert db.register_user(conn, SALIM, "Салим") is True
    user = db.get_user(conn, SALIM)
    assert user["status"] == "active"
    assert user["name"] == "Салим"
    assert user["registered_at"]


def test_inviting_twice_does_not_reset_anyone(conn):
    db.invite_user(conn, SALIM)
    db.register_user(conn, SALIM, "Салим")
    assert db.invite_user(conn, SALIM) is False
    assert db.get_user(conn, SALIM)["status"] == "active"


def test_owner_from_env_becomes_admin_without_losing_data(conn):
    """Переход со старой версии: записи уже есть, доступ теряться не должен."""
    db.add_transaction(conn, OWNER, item="сумки", amount=1, currency="TJS")
    db.ensure_admin(conn, OWNER)
    user = db.get_user(conn, OWNER)
    assert user["role"] == "admin"
    assert len(db.search_transactions(conn, OWNER)) == 1


def test_owner_from_env_does_not_have_to_register_again(conn):
    """Он неделями пользовался ботом — гнать его через «как тебя зовут» нельзя."""
    db.ensure_admin(conn, OWNER)
    assert db.get_user(conn, OWNER)["status"] == "active"


def test_ensure_admin_promotes_and_unblocks(conn):
    db.invite_user(conn, SALIM)
    db.set_status(conn, SALIM, "blocked")
    db.ensure_admin(conn, SALIM)
    user = db.get_user(conn, SALIM)
    assert user["role"] == "admin"
    assert user["status"] != "blocked"


def test_ensure_admin_is_idempotent(conn):
    db.ensure_admin(conn, OWNER)
    db.register_user(conn, OWNER, "Алиджон")
    db.ensure_admin(conn, OWNER)
    assert db.get_user(conn, OWNER)["name"] == "Алиджон"


def test_status_must_be_one_of_the_known_ones(conn):
    db.invite_user(conn, SALIM)
    assert db.set_status(conn, SALIM, "выдуманный") is False
    assert db.get_user(conn, SALIM)["status"] == "invited"


def test_admins_are_listed_first(conn):
    db.invite_user(conn, SALIM)
    db.invite_user(conn, KARIM)
    db.ensure_admin(conn, OWNER)
    assert db.list_users(conn)[0]["id"] == OWNER


def test_count_admins_ignores_blocked(conn):
    db.ensure_admin(conn, OWNER)
    db.ensure_admin(conn, SALIM)
    assert db.count_admins(conn) == 2
    db.set_status(conn, SALIM, "blocked")
    assert db.count_admins(conn) == 1


def test_stats_count_only_this_persons_records(conn):
    db.add_transaction(conn, OWNER, amount=1, currency="TJS")
    db.add_transaction(conn, OWNER, amount=2, currency="TJS")
    db.add_transaction(conn, SALIM, amount=3, currency="TJS")
    db.add_document(conn, OWNER, tg_file_id="a", file_path="/tmp/a.jpg")

    assert db.user_stats(conn, OWNER) == {"transactions": 2, "documents": 1}
    assert db.user_stats(conn, SALIM) == {"transactions": 1, "documents": 0}


def test_deleted_records_are_not_counted(conn):
    tx_id = db.add_transaction(conn, OWNER, amount=1, currency="TJS")
    db.delete_transaction(conn, OWNER, tx_id)
    assert db.user_stats(conn, OWNER)["transactions"] == 0


# ── удаление человека ──────────────────────────────────────────────────────

def test_deleting_a_person_removes_their_data_and_files(conn, tmp_path):
    photo = tmp_path / "salim.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    db.add_transaction(conn, SALIM, amount=1, currency="TJS")
    db.add_document(conn, SALIM, tg_file_id="a", file_path=str(photo))
    db.log_message(conn, SALIM, "user", "привет")

    stats = db.delete_user(conn, SALIM)

    assert stats == {"transactions": 1, "documents": 1}
    assert db.get_user(conn, SALIM) is None
    assert db.search_transactions(conn, SALIM) == []
    assert db.search_documents(conn, SALIM) == []
    assert not photo.exists()   # фото на диске тоже наше, оставлять его незачем


def test_deleting_one_person_does_not_touch_another(conn):
    db.add_transaction(conn, OWNER, item="сумки", amount=1, currency="TJS")
    db.add_transaction(conn, SALIM, item="сумки", amount=2, currency="TJS")
    db.delete_user(conn, SALIM)

    assert len(db.search_transactions(conn, OWNER, text="сумки")) == 1


def test_deleting_a_person_with_a_missing_photo_does_not_crash(conn):
    db.add_document(conn, SALIM, tg_file_id="a", file_path="/nope/gone.jpg")
    assert db.delete_user(conn, SALIM)["documents"] == 1
