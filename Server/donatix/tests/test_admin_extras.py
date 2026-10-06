import re
from datetime import datetime, timedelta, timezone

from conftest import csrf_of
from fastapi.testclient import TestClient

from donatix import accounts, db, reports


def test_admin_login_needs_telegram_code(app, config, conn, monkeypatch):
    config.alert_telegram_token, config.alert_telegram_chat_id = "t", "777"
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: sent.append(text))
    c = TestClient(app)
    token = csrf_of(c.get("/login").text)
    r = c.post("/login", data={"csrf": token, "email": "admin@example.com", "password": "adminpass123"},
               follow_redirects=False)
    assert r.headers["location"] == "/login/code"
    assert c.get("/admin", follow_redirects=False).status_code in (303, 302)   # без кода не пускает
    code = re.search(r"(\d{6})", sent[-1]).group(1)
    page = c.get("/login/code").text
    bad = c.post("/login/code", data={"csrf": csrf_of(page), "code": "000000" if code != "000000" else "111111"})
    assert "Неверный код" in bad.text
    r = c.post("/login/code", data={"csrf": csrf_of(bad.text), "code": code}, follow_redirects=False)
    assert r.headers["location"] == "/admin"
    assert c.get("/admin").status_code == 200


def test_client_login_has_no_code(app, config, conn, monkeypatch):
    config.alert_telegram_token, config.alert_telegram_chat_id = "t", "777"
    monkeypatch.setattr("donatix.worker.notify_admin", lambda *a, **k: None)
    accounts.create_user(conn, email="c@example.com", login="client1", password="password123", status="active")
    c = TestClient(app)
    token = csrf_of(c.get("/login").text)
    r = c.post("/login", data={"csrf": token, "email": "c@example.com", "password": "password123"},
               follow_redirects=False)
    assert r.headers["location"] == "/panel"


def test_daily_report_once_after_9(config, conn, monkeypatch):
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: sent.append(text))
    tz = timezone(timedelta(hours=config.tz_offset))
    early = datetime(2026, 9, 26, 8, 0, tzinfo=tz)
    assert not reports.maybe_send(conn, config, early)
    nine = datetime(2026, 9, 26, 9, 5, tzinfo=tz)
    assert reports.maybe_send(conn, config, nine)
    assert not reports.maybe_send(conn, config, nine + timedelta(hours=3))   # раз в день
    assert "Отчёт за 25.09.2026" in sent[-1] and "Прибыль" in sent[-1]
    db.set_setting(conn, "report.daily_on", "0")
    assert not reports.maybe_send(conn, config, nine + timedelta(days=1))


def test_crash_alert_after_three_restarts(config, conn, monkeypatch):
    from donatix import bots
    sent = []
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: sent.append(text))
    runner = bots.BotRunner(config, "http://127.0.0.1:8000")
    runner._alert_crash(5, 1, {"restarts": 2})
    assert sent == []
    meta = {"restarts": 3}
    runner._alert_crash(5, 1, meta)
    runner._alert_crash(5, 1, meta)          # не чаще раза в час
    assert len(sent) == 1 and "падает" in sent[0]
