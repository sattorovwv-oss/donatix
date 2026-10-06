import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from donatix import accounts, catalog, db  # noqa: E402
from donatix.app import create_app  # noqa: E402
from donatix.config import Config  # noqa: E402
from donatix.money import to_micro  # noqa: E402
from donatix.suppliers.mock import MockSupplier  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return Config(
        secret_key="test-secret",
        db_path=tmp_path / "t.db",
        admin_email="admin@example.com",
        admin_password="adminpass123",
        run_worker=False,
        base_url="http://testserver",
        rate_auto=False,
    )


@pytest.fixture
def supplier():
    return MockSupplier()


@pytest.fixture
def app(config, supplier):
    application = create_app(config, supplier)
    conn = db.connect(config.db_path)
    catalog.sync_catalog(conn, supplier)
    conn.close()
    return application


@pytest.fixture
def conn(app, config):
    c = db.connect(config.db_path)
    yield c
    c.close()


@pytest.fixture
def client(app):
    return TestClient(app)


def make_client(conn, login="shop1", balance="100", status="active"):
    uid = accounts.create_user(conn, email=f"{login}@example.com", login=login, password="password123",
                               status=status)
    if balance:
        with db.tx(conn):
            accounts.post_ledger(conn, uid, to_micro(balance), "Пополнение")
    key = accounts.create_api_key(conn, uid, "test")
    return uid, key


@pytest.fixture
def shop(conn):
    uid, key = make_client(conn)
    return {"id": uid, "key": key, "h": {"X-API-Key": key}}


def balance(conn, uid) -> int:
    return conn.execute("SELECT balance_micro FROM users WHERE id = ?", (uid,)).fetchone()[0]


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def web_login(client, email, password):
    token = csrf_of(client.get("/login").text)
    r = client.post("/login", data={"csrf": token, "email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return csrf_of(client.get("/panel").text)


@pytest.fixture(autouse=True)
def _reset_account_check():
    from donatix import account_check, cache, cryptopay, rates, throttle, traffic
    throttle.SUPPLIER.configure(100_000)  # тесты не ждут очереди поставщика
    throttle.SUPPLIER.reset()
    account_check.reset()
    cryptopay.reset()
    cache.clear()
    rates.reset()
    traffic.reset()
    yield


RECEIPT_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
RECEIPT = {"receipt": ("chek.png", RECEIPT_PNG, "image/png")}


def add_receipt(conn, pid: int) -> None:
    """Заявке, созданной напрямую, — чек в базе: без него зачислить нельзя."""
    conn.execute("UPDATE payments SET receipt_file = 'test.png' WHERE id = ?", (pid,))
