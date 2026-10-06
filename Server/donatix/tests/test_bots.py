"""Конструктор ботов: подключение по токену, запуск процесса с нужными настройками, остановка."""

import json
import sys
import time

import httpx
import pytest
from conftest import web_login
from fastapi.testclient import TestClient

from donatix import accounts, bots

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"


def _tg(ok=True):
    body = {"ok": ok, "result": {"username": "shop_test_bot"}}
    return httpx.MockTransport(lambda req: httpx.Response(200, json=body))


def test_token_already_running_elsewhere():
    def handler(req):
        if req.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "busy_bot"}})
        body = {"ok": False, "description": "Conflict: terminated by other getUpdates request"}
        return httpx.Response(409, json=body)
    with pytest.raises(bots.BotError, match="уже запущен"):
        bots.check_token(TOKEN, transport=httpx.MockTransport(handler))


def test_check_token():
    assert bots.check_token(TOKEN, transport=_tg()) == "shop_test_bot"
    with pytest.raises(bots.BotError):
        bots.check_token("nope")
    with pytest.raises(bots.BotError):
        bots.check_token(TOKEN, transport=_tg(ok=False))
    assert bots.parse_admin_ids("111, 222") == "111,222"
    with pytest.raises(bots.BotError):
        bots.parse_admin_ids("@user")


def test_runner_starts_bot_with_donatix_settings(config, conn, tmp_path, monkeypatch):
    # вместо настоящего магазина — крошечный app/main.py, который пишет свои настройки и ждёт
    tpl = tmp_path / "tpl"
    (tpl / "app").mkdir(parents=True)
    (tpl / "app" / "__init__.py").write_text("")
    (tpl / "app" / "main.py").write_text(
        "import json, os, time\n"
        "keys = ['BOT_TOKEN','ADMIN_IDS','FRAGMENT_MODE','FAZER_API_KEY','FAZER_BASE_URL','DONATIX_URL','DB_PATH']\n"
        "open(os.environ['DB_PATH'] + '.env.json', 'w').write(json.dumps({k: os.environ.get(k) for k in keys}))\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(bots, "TEMPLATE_DIR", tpl)
    uid = accounts.create_user(conn, email="o@example.com", login="owner", password="password123", status="active")
    bid = bots.create(conn, config, user_id=uid, token=TOKEN, admin_ids="555", username="shop_test_bot")
    with pytest.raises(bots.BotError):
        bots.create(conn, config, user_id=uid, token=TOKEN, admin_ids="555", username="x")  # второй раз нельзя
    runner = bots.BotRunner(config, "http://127.0.0.1:8000", python=sys.executable)
    try:
        runner.sync()
        dump = bots.bot_dir(config, bid) / "bot.sqlite3.env.json"
        for _ in range(100):
            if dump.exists():
                break
            time.sleep(0.05)
        env = json.loads(dump.read_text())
        assert env["BOT_TOKEN"] == TOKEN and env["ADMIN_IDS"] == "555" and env["FRAGMENT_MODE"] == "fazer"
        assert env["FAZER_BASE_URL"] == env["DONATIX_URL"] == "http://127.0.0.1:8000"
        assert accounts.user_by_api_key(conn, env["FAZER_API_KEY"])["id"] == uid  # ключ владельца, создан сам
        assert runner.state(bid)["running"]
        bots.set_enabled(conn, bid, False)
        runner.sync()
        assert not runner.state(bid)["running"]
        bots.delete(conn, bid)
        assert accounts.user_by_api_key(conn, env["FAZER_API_KEY"]) is None  # ключ отозван
    finally:
        runner.stop()


def test_admin_bots_page(app, config, conn, monkeypatch):
    monkeypatch.setattr(bots, "check_token", lambda token: "shop_test_bot")
    admin = TestClient(app)
    token = web_login(admin, "admin@example.com", "adminpass123")
    r = admin.post("/admin/bots", data={"csrf": token, "token": TOKEN, "admin_ids": "555", "user_id": "0"})
    assert "Бот @shop_test_bot подключён" in r.text and "@shop_test_bot" in r.text
    assert TOKEN not in r.text  # токен на странице не показываем
    (bots.bot_dir(config, 1)).mkdir(parents=True, exist_ok=True)
    (bots.bot_dir(config, 1) / "bot.log").write_text(f"start with {TOKEN}\n")
    assert TOKEN not in admin.get("/admin/bots/1/log").text


def test_no_double_start_and_conflict_warning(config, conn, tmp_path, monkeypatch):
    import threading
    tpl = tmp_path / "tpl2"
    (tpl / "app").mkdir(parents=True)
    (tpl / "app" / "__init__.py").write_text("")
    (tpl / "app" / "main.py").write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(bots, "TEMPLATE_DIR", tpl)
    uid = accounts.create_user(conn, email="d@example.com", login="dbl", password="password123", status="active")
    bid = bots.create(conn, config, user_id=uid, token=TOKEN, admin_ids="1", username="dbl_bot")
    runner = bots.BotRunner(config, "http://x", python=sys.executable)
    spawned = []
    real = runner._spawn
    monkeypatch.setattr(runner, "_spawn", lambda py, row: (spawned.append(row["id"]), real(py, row)))
    try:
        ts = [threading.Thread(target=runner.sync) for _ in range(5)]  # кнопка + фоновая проверка разом
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert spawned == [bid]
        (bots.bot_dir(config, bid) / "bot.log").write_text(
            "aiogram.exceptions.TelegramConflictError: Conflict: terminated by other getUpdates request\n")
        assert runner.state(bid)["conflict"] is True
    finally:
        runner.stop()


def test_client_connects_own_bot(app, config, conn, monkeypatch):
    monkeypatch.setattr(bots, "check_token", lambda token: token.split(":")[1][:6] + "_bot")
    uid = accounts.create_user(conn, email="c@example.com", login="client1", password="password123", status="active")
    other = accounts.create_user(conn, email="o@example.com", login="other1", password="password123",
                                 status="active")
    c = TestClient(app)
    tok = web_login(c, "c@example.com", "password123")
    assert "Мой Telegram-бот" in c.get("/panel").text
    # До 5 выполненных заказов свой бот закрыт
    page = c.get("/panel/bots").text
    assert "Свой бот пока закрыт" in page and "0 из 5" in page
    r = c.post("/panel/bots", data={"csrf": tok, "token": "1:AAAAAA", "admin_ids": "777"})
    assert "после 5 выполненных заказов" in r.text and conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 0
    _completed_orders(conn, uid, 5)
    r = c.post("/panel/bots", data={"csrf": tok, "token": "1:AAAAAA", "admin_ids": "777"})
    assert "Бот @AAAAAA_bot подключён" in r.text and "1:AAAAAA" not in r.text
    row = conn.execute("SELECT * FROM bots").fetchone()
    assert row["user_id"] == uid and row["admin_ids"] == "777"  # тратит баланс клиента, не админа

    # чужого бота не видно и не тронуть
    oid = bots.create(conn, config, user_id=other, token="2:BBBBBB", admin_ids="1", username="bbb_bot")
    assert "bbb_bot" not in c.get("/panel/bots").text
    r = c.post(f"/panel/bots/{oid}/delete", data={"csrf": tok})
    assert "Бот не найден" in r.text and conn.execute("SELECT 1 FROM bots WHERE id = ?", (oid,)).fetchone()

    # лимит
    for i in range(2, 4):
        c.post("/panel/bots", data={"csrf": tok, "token": f"{i}:CCCCC{i}", "admin_ids": "777"})
    r = c.post("/panel/bots", data={"csrf": tok, "token": "9:DDDDDD", "admin_ids": "777"})
    assert "Можно подключить до 3 ботов" in r.text

    r = c.post(f"/panel/bots/{row['id']}/delete", data={"csrf": tok})
    assert "Бот удалён" in r.text


def test_pending_client_cannot_add_bot(app, conn, monkeypatch):
    monkeypatch.setattr(bots, "check_token", lambda token: "x_bot")
    accounts.create_user(conn, email="p@example.com", login="pend1", password="password123")
    c = TestClient(app)
    tok = web_login(c, "p@example.com", "password123")
    r = c.post("/panel/bots", data={"csrf": tok, "token": "1:AAAAAA", "admin_ids": "777"})
    assert "после подтверждения аккаунта" in r.text or r.url.path == "/login"
    assert conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 0



def _completed_orders(conn, uid, n):
    for i in range(n):
        conn.execute("INSERT INTO orders (public_id, user_id, product_id, kind, product_name, quantity, unit_price, "
                     "total_micro, cost_micro, status, supplier_idem_key, created_at, updated_at) VALUES "
                     "(?, ?, 'p', 'topup', 'x', 1, '1', 1, 1, 'completed', ?, '2026-01-01T00:00:00.000Z', "
                     "'2026-01-01T00:00:00.000Z')", (f"dx-t{uid}-{i}", uid, f"k{uid}-{i}"))


def test_inactive_disabled_bot_blocks_client(app, config, conn, monkeypatch):
    """Бот отключён алгоритмом — клиент сам не включит и нового не подключит; админ снимает запрет."""
    from donatix import bot_watch
    monkeypatch.setattr(bots, "check_token", lambda token: token.split(":")[1][:6] + "_bot")
    monkeypatch.setattr("donatix.worker.notify_admin", lambda *a, **k: None)
    uid = accounts.create_user(conn, email="c@example.com", login="client1", password="password123", status="active")
    _completed_orders(conn, uid, 5)
    bid = bots.create(conn, config, user_id=uid, token="1:AAAAAA", admin_ids="777", username="aaa_bot")
    conn.execute("UPDATE bots SET created_at = '2020-01-01T00:00:00.000Z', active_since = '2020-01-01T00:00:00.000Z', "
                 "warn_count = 3, last_warn_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (bid,))
    bot_watch.check(conn, config)
    assert conn.execute("SELECT enabled, disabled_reason FROM bots WHERE id = ?", (bid,)).fetchone()[1] == "inactive"

    c = TestClient(app)
    tok = web_login(c, "c@example.com", "password123")
    r = c.post(f"/panel/bots/{bid}/start", data={"csrf": tok})
    assert "только администратор" in r.text
    assert conn.execute("SELECT enabled FROM bots WHERE id = ?", (bid,)).fetchone()[0] == 0
    c.post(f"/panel/bots/{bid}/delete", data={"csrf": tok})          # удалить и подключить нового — тоже нельзя
    r = c.post("/panel/bots", data={"csrf": tok, "token": "2:BBBBBB", "admin_ids": "777"})
    assert "только администратор" in r.text and conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 0

    bots.unblock_user(conn, uid)                                        # админ снял запрет
    r = c.post("/panel/bots", data={"csrf": tok, "token": "2:BBBBBB", "admin_ids": "777"})
    assert "подключён" in r.text
