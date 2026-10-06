"""База: схема, поиск по русским словоформам, мягкое удаление, синхрон FTS."""


from bot import db


def add(conn, **kw):
    kw.setdefault("owner_id", 1)
    owner_id = kw.pop("owner_id")
    return db.add_transaction(conn, owner_id, **kw)


def test_normalize_lowercases_and_folds_yo():
    assert db.normalize("Ёлка  БОЛЬШАЯ") == "елка большая"
    assert db.normalize(None) == ""


def test_fts_query_stems_long_words():
    query = db.fts_query("накладная от женской обуви")
    assert '"обув"*' in query
    assert '"накладн"*' in query


def test_search_finds_different_word_form(conn):
    """«сумки» в вопросе должно находить «сумка» в записи — иначе бот бесполезен."""
    add(conn, item="сумка", amount=500000, currency="KZT", direction="out",
        kind="payment", happened_on="2026-08-20")
    found = db.search_transactions(conn, 1, text="сумки")
    assert len(found) == 1
    assert found[0]["item"] == "сумка"


def test_search_by_counterparty_is_case_insensitive(conn):
    add(conn, counterparty="Абубакр", amount=3000, currency="TJS", direction="out")
    assert len(db.search_transactions(conn, 1, counterparty="абубакр")) == 1
    assert len(db.search_transactions(conn, 1, counterparty="АБУБАКР")) == 1


def test_search_returns_full_history_not_just_latest(conn):
    for day in ("2026-08-01", "2026-08-10", "2026-08-20"):
        add(conn, counterparty="Абубакр", amount=100, currency="TJS",
            direction="out", happened_on=day)
    rows = db.search_transactions(conn, 1, counterparty="Абубакр")
    assert len(rows) == 3
    assert rows[0]["happened_on"] == "2026-08-20"  # новые сверху


def test_search_filters_by_period(conn):
    add(conn, amount=1, currency="TJS", happened_on="2026-07-31")
    add(conn, amount=2, currency="TJS", happened_on="2026-08-15")
    rows = db.search_transactions(conn, 1, date_from="2026-08-01", date_to="2026-08-31")
    assert [r["amount"] for r in rows] == [2.0]


def test_chats_are_isolated(conn):
    db.add_transaction(conn, 1, item="моё", amount=10, currency="TJS")
    db.add_transaction(conn, 999, item="чужое", amount=10, currency="TJS")
    assert len(db.search_transactions(conn, 1, text="чужое")) == 0


def test_deleted_rows_disappear_from_search(conn):
    tx_id = add(conn, item="сумка", amount=5, currency="TJS")
    assert db.delete_transaction(conn, 1, tx_id) is True
    assert db.search_transactions(conn, 1, text="сумка") == []
    assert db.get_transaction(conn, 1, tx_id) is None


def test_update_keeps_fts_in_sync(conn):
    """После правки поиск должен находить новое значение и не находить старое."""
    tx_id = add(conn, item="сумка", amount=5, currency="TJS")
    db.update_transaction(conn, 1, tx_id, item="мебель")
    assert len(db.search_transactions(conn, 1, text="мебель")) == 1
    assert db.search_transactions(conn, 1, text="сумка") == []


def test_update_refreshes_counterparty_norm(conn):
    tx_id = add(conn, counterparty="Абубакр", amount=5, currency="TJS")
    db.update_transaction(conn, 1, tx_id, counterparty="Салим")
    assert len(db.search_transactions(conn, 1, counterparty="салим")) == 1
    assert db.search_transactions(conn, 1, counterparty="абубакр") == []


def test_totals_are_per_currency_without_conversion(conn):
    add(conn, amount=500000, currency="KZT", direction="out")
    add(conn, amount=3000, currency="TJS", direction="out")
    add(conn, amount=1200, currency="TJS", direction="in")
    totals = db.totals_by_currency(db.search_transactions(conn, 1))
    assert totals["KZT"]["out"] == 500000
    assert totals["TJS"]["out"] == 3000
    assert totals["TJS"]["in"] == 1200
    assert "USD" not in totals


def test_totals_ignore_rows_without_amount(conn):
    add(conn, amount=None, currency=None, direction="out")
    assert db.totals_by_currency(db.search_transactions(conn, 1)) == {}


# ── документы ──────────────────────────────────────────────────────────────

def test_photo_is_pending_until_described(conn):
    doc_id = db.add_document(conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")
    assert [d["id"] for d in db.pending_documents(conn, 1)] == [doc_id]
    db.describe_document(conn, 1, doc_id, description="накладная на женскую обувь",
                         doc_kind="накладная")
    assert db.pending_documents(conn, 1) == []


def test_invoice_found_by_spoken_words(conn):
    """Сценарий пользователя: подписал «женская обувь», через два дня ищет её же."""
    doc_id = db.add_document(conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")
    db.describe_document(conn, 1, doc_id, description="накладная на женскую обувь, 4 места",
                         doc_kind="накладная")
    for query in ("накладная от женской обуви", "женская обувь", "обувь"):
        assert [d["id"] for d in db.search_documents(conn, 1, text=query)] == [doc_id], query


def test_document_search_misses_unrelated(conn):
    doc_id = db.add_document(conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")
    db.describe_document(conn, 1, doc_id, description="чек за мебель")
    assert db.search_documents(conn, 1, text="женская обувь") == []


def test_message_log_survives(conn):
    db.log_message(conn, 1, "user", "отправил Абубакру 3000 сомони")
    rows = conn.execute("SELECT role, text FROM messages").fetchall()
    assert rows[0]["role"] == "user"
