import hashlib
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from donatix import accounts, db, google_auth
from donatix.config import Config
from donatix.suppliers.mock import MockSupplier
from donatix_android_extension.factory import PREFIX, create_app
from donatix_android_extension.fcm import InvalidToken
from donatix_android_extension.store import site_readonly


class FakeSender:
    configured = True

    def __init__(self):
        self.calls = []
        self.error = None

    def __call__(self, token, data):
        if self.error:
            raise self.error
        self.calls.append((token, data))


@pytest.fixture
def setup(tmp_path):
    config = Config(secret_key="extension-test-secret", db_path=tmp_path / "site.db", run_worker=False,
                    rate_auto=False, base_url="http://testserver", google_client_id="test-id", google_client_secret="test-secret")
    sender = FakeSender()
    app = create_app(config, MockSupplier(), store_path=tmp_path / "android.db", sender=sender, start_push=False)
    with db.connect(config.db_path) as conn:
        uid = accounts.create_user(conn, email="user@example.com", login="user", password="password123", status="active")
    return app, config, sender, uid


def csrf(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(app, email="user@example.com"):
    client = TestClient(app)
    token = csrf(client)
    r = client.post("/login", data={"csrf": token, "email": email, "password": "password123"}, follow_redirects=False)
    assert r.status_code == 303
    token = client.get(PREFIX + "/session").json()["csrf"]
    return client, token


def register(client, token, device="a" * 32, fcm="registration-token-" + "x" * 30):
    return client.post(PREFIX + "/push/register", headers={"X-CSRF-Token": token},
                       json={"device_id": device, "token": fcm, "platform": "android"})


def event(config, uid):
    with db.connect(config.db_path) as c:
        return c.execute("INSERT INTO notifications(user_id,text,link,created_at) VALUES(?,?,?,?)",
                         (uid, "Заказ выполнен", "/panel/orders", db.now())).lastrowid


def prepare(client):
    verifier = "v" * 64
    reply = client.post(PREFIX + "/oauth/prepare", json={"challenge": hashlib.sha256(verifier.encode()).hexdigest()})
    assert reply.status_code == 200
    return verifier, reply.json()["ticket"]


def test_site_schema_unchanged_and_extension_is_readonly_to_site(setup):
    app, config, _, _ = setup
    with site_readonly(config.db_path) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"flows", "devices", "outbox", "mobile_oauth", "native_push_devices"} & names
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM users")
    assert TestClient(app).get("/login").status_code == 200
    assert TestClient(app).get("/api/openapi.json").json()["paths"].get(PREFIX + "/push/register")


def test_password_login_and_logout_are_preserved(setup):
    app, _, _, _ = setup
    client, token = login(app)
    assert client.get("/panel").status_code == 200
    reply = client.post("/logout", data={"csrf": token}, follow_redirects=False)
    assert reply.status_code == 303
    assert client.get(PREFIX + "/session").status_code == 401


def test_complete_google_handoff_and_one_time_claim(setup, monkeypatch):
    app, _, _, uid = setup
    mobile, browser = TestClient(app), TestClient(app)
    verifier, ticket = prepare(mobile)
    reply = browser.get(PREFIX + "/oauth/start/" + ticket, follow_redirects=False)
    assert reply.headers["location"] == "/auth/google"
    response = browser.get("/auth/google", follow_redirects=False)
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    monkeypatch.setattr(google_auth, "fetch_profile", lambda *_: {"sub": "google-user", "email": "user@example.com", "email_verified": True})
    response = browser.get("/auth/google/callback", params={"code": "code", "state": state}, follow_redirects=False)
    assert response.headers["location"] == PREFIX + "/oauth/confirm/" + ticket
    assert mobile.post(PREFIX + "/oauth/claim", json={"ticket": ticket, "verifier": verifier}).json()["pending"] is True
    form_token = csrf(browser, PREFIX + "/oauth/confirm/" + ticket)
    assert browser.post(PREFIX + "/oauth/confirm/" + ticket, data={"csrf": form_token}).status_code == 200
    assert mobile.post(PREFIX + "/oauth/claim", json={"ticket": ticket, "verifier": "z" * 64}).status_code == 403
    assert mobile.post(PREFIX + "/oauth/claim", json={"ticket": ticket, "verifier": verifier}).json()["pending"] is False
    assert mobile.get(PREFIX + "/session").json()["user_id"] == uid
    assert mobile.post(PREFIX + "/oauth/claim", json={"ticket": ticket, "verifier": verifier}).status_code == 410


def test_web_google_login_without_mobile_cookie_still_goes_to_panel(setup, monkeypatch):
    app, _, _, _ = setup
    client = TestClient(app)
    reply = client.get("/auth/google", follow_redirects=False)
    state = parse_qs(urlparse(reply.headers["location"]).query)["state"][0]
    monkeypatch.setattr(google_auth, "fetch_profile", lambda *_: {"sub": "web-only", "email": "user@example.com", "email_verified": True})
    reply = client.get("/auth/google/callback", params={"code": "code", "state": state}, follow_redirects=False)
    assert reply.headers["location"] == "/panel"


def test_google_expired_ticket_and_csrf_are_rejected(setup):
    app, _, _, _ = setup
    client, _ = login(app)
    _, ticket = prepare(client)
    assert client.post(PREFIX + "/oauth/confirm/" + ticket, data={"csrf": "forged"}).status_code == 403
    with app.state.android_store.connect() as c:
        c.execute("UPDATE flows SET expires=0 WHERE id=?", (ticket,))
    assert client.get(PREFIX + "/oauth/start/" + ticket).status_code == 410


def test_push_requires_live_cookie_csrf_and_same_origin(setup):
    app, _, _, _ = setup
    assert register(TestClient(app), "none").status_code == 401
    client, token = login(app)
    assert register(client, "wrong").status_code == 403
    r = client.post(PREFIX + "/push/register", headers={"X-CSRF-Token": token, "Origin": "https://other.example"},
                    json={"device_id": "a" * 32, "token": "x" * 40})
    assert r.status_code == 403
    assert register(client, token).status_code == 200


def test_real_notification_enqueued_and_encrypted_without_marking_read(setup):
    app, config, sender, uid = setup
    client, token = login(app)
    worker = app.state.android_worker
    worker.scan()
    response = register(client, token).json()
    nid = event(config, uid)
    worker.scan()
    worker.dispatch()
    assert len(sender.calls) == 1
    fcm, data = sender.calls[0]
    assert data["notification_id"] == nid and data["binding"] == response["binding"]
    with app.state.android_store.connect() as c:
        assert fcm not in c.execute("SELECT token FROM devices").fetchone()[0]
        assert c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    with site_readonly(config.db_path) as c:
        assert c.execute("SELECT read_at FROM notifications WHERE id=?", (nid,)).fetchone()[0] is None


def test_registration_does_not_send_historical_notifications(setup):
    app, config, sender, uid = setup
    event(config, uid)
    worker = app.state.android_worker
    worker.scan()
    client, token = login(app)
    assert register(client, token).status_code == 200
    worker.scan()
    worker.dispatch()
    assert not sender.calls


def test_logout_prevents_queued_push(setup):
    app, config, sender, uid = setup
    client, token = login(app)
    worker = app.state.android_worker
    worker.scan()
    register(client, token)
    event(config, uid)
    worker.scan()
    client.post("/logout", data={"csrf": token})
    worker.dispatch()
    assert not sender.calls


def test_device_account_switch_drops_old_queue_and_changes_binding(setup):
    app, config, sender, uid = setup
    worker = app.state.android_worker
    worker.scan()
    client, token = login(app)
    first = register(client, token).json()
    event(config, uid)
    worker.scan()
    with db.connect(config.db_path) as c:
        accounts.create_user(c, email="second@example.com", login="second", password="password123", status="active")
    other, other_token = login(app, "second@example.com")
    second = register(other, other_token).json()
    assert first["binding"] != second["binding"]
    worker.dispatch()
    assert not sender.calls


def test_fcm_failure_retries_and_invalid_token_is_removed(setup):
    app, config, sender, uid = setup
    client, token = login(app)
    worker = app.state.android_worker
    worker.scan()
    register(client, token)
    event(config, uid)
    worker.scan()
    sender.error = RuntimeError("temporary")
    worker.dispatch(now=100)
    with app.state.android_store.connect() as c:
        row = c.execute("SELECT attempts,next_try FROM outbox").fetchone()
        assert row["attempts"] == 1 and row["next_try"] > 100
    sender.error = InvalidToken()
    worker.dispatch(now=1000)
    with app.state.android_store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
