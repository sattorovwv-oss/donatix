from decimal import Decimal

import pytest
from conftest import web_login
from fastapi.testclient import TestClient

from donatix import db, payments, rates


@pytest.fixture
def feed(monkeypatch):
    """Подменяем интернет: адрес → ответ (или исключение)."""
    answers = {}

    def fake(url):
        value = answers.get(url, RuntimeError("offline"))
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(rates, "_get_json", fake)
    return answers


ER, CUR = rates.SOURCES[0][1], rates.SOURCES[1][1]


def test_refresh_applies_margin_and_respects_age(feed, config, conn):
    config.rate_auto = True
    feed[ER] = {"result": "success", "rates": {"TJS": 10.62}}
    assert rates.refresh(conn, config, 300)
    conf = payments.settings(conn, config)
    assert conf["tjs_rate"] == Decimal("10.73")  # 10.62 + 1% = 10.7262 → вверх
    assert rates.status(conn, config)["source"] == "open.er-api.com"
    feed[ER] = {"result": "success", "rates": {"TJS": 11.0}}
    rates.reset()
    assert not rates.refresh(conn, config, 300)  # свежий — не трогаем
    db.set_setting(conn, "pay.rate_ts", "1")  # «давно»
    assert rates.refresh(conn, config, 30)
    assert payments.settings(conn, config)["tjs_rate"] == Decimal("11.11")


def test_fallback_source_and_bad_values_keep_last_rate(feed, config, conn):
    config.rate_auto = True
    feed[CUR] = {"usd": {"tjs": 10.5}}  # первый источник «лежит»
    assert rates.refresh(conn, config, force=True)
    assert rates.status(conn, config)["source"] == "currency-api"
    feed[ER] = {"result": "success", "rates": {"TJS": 55}}      # нелепое число
    feed[CUR] = {"usd": {"tjs": 14.0}}                          # скачок > 15%
    feed[rates.SOURCES[2][1]] = RuntimeError("down")
    rates.reset()
    assert not rates.refresh(conn, config, force=True)
    assert payments.settings(conn, config)["tjs_rate"] == Decimal("10.61")
    assert "скачок" in rates.status(conn, config)["error"]


def test_manual_mode_does_not_fetch(feed, config, conn):
    feed[ER] = {"result": "success", "rates": {"TJS": 10.62}}
    assert not rates.refresh(conn, config, force=True)  # в тестах автокурс выключен
    assert payments.settings(conn, config)["tjs_rate"] == Decimal("10.9")


def test_admin_turns_on_auto_and_page_polls(feed, app, config, conn):
    from donatix import accounts
    feed[ER] = {"result": "success", "rates": {"TJS": 10.0}}
    admin = TestClient(app)
    tok = web_login(admin, "admin@example.com", "adminpass123")
    r = admin.post("/admin/pay-settings", data={"csrf": tok, "n": "0", "tjs_rate": "10.9", "min_tjs": "500",
                                                "low_usd": "10", "rate_auto": "1", "rate_margin": "2"})
    assert "Реквизиты сохранены" in r.text
    assert payments.settings(conn, config)["tjs_rate"] == Decimal("10.20")
    assert "Рынок 10.0000 + запас 2%" in r.text
    accounts.create_user(conn, email="c@example.com", login="client1", password="password123", status="active")
    c = TestClient(app)
    web_login(c, "c@example.com", "password123")
    assert c.get("/panel/data/rate").json()["tjs_rate"] == "10.20"
    assert "/panel/data/rate" in c.get("/panel/balance").text
