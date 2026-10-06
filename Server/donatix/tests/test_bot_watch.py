from datetime import datetime, timedelta, timezone

from donatix import accounts, bot_watch, bots, notify


def _setup(config, conn, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send_telegram", lambda token, chats, text: sent.append(text))
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: sent.append("ADMIN " + text))
    uid = accounts.create_user(conn, email="p@example.com", login="partner1", password="password123",
                               status="active")
    bid = bots.create(conn, config, user_id=uid, token="1:AAAAAA", admin_ids="777", username="p_bot")
    return uid, bid, sent


def _run(conn, config, days):
    return bot_watch.check(conn, config, now=datetime.now(timezone.utc) + timedelta(days=days))


def test_three_warnings_then_disable(config, conn, monkeypatch):
    uid, bid, sent = _setup(config, conn, monkeypatch)
    monkeypatch.setattr("threading.Thread", lambda target, args=(), daemon=None: type(
        "T", (), {"start": lambda self: target(*args)})())
    assert _run(conn, config, 3)["warned"] == 0          # рано
    assert _run(conn, config, 8)["warned"] == 1
    assert _run(conn, config, 9)["warned"] == 0          # между предупреждениями 2 дня
    assert _run(conn, config, 10)["warned"] == 1
    assert _run(conn, config, 12)["warned"] == 1
    assert any("3 из 3" in t for t in sent)
    assert _run(conn, config, 14)["disabled"] == 1
    b = conn.execute("SELECT enabled, disabled_reason FROM bots WHERE id = ?", (bid,)).fetchone()
    assert b["enabled"] == 0 and b["disabled_reason"] == "inactive"
    assert any("отключён" in t for t in sent)
    bots.set_enabled(conn, bid, True)                    # включили снова — отсчёт заново
    b = conn.execute("SELECT warn_count FROM bots WHERE id = ?", (bid,)).fetchone()
    assert b["warn_count"] == 0


def test_sale_resets_warnings(config, conn, monkeypatch):
    uid, bid, sent = _setup(config, conn, monkeypatch)
    _run(conn, config, 8)
    key_id = conn.execute("SELECT api_key_id FROM bots WHERE id = ?", (bid,)).fetchone()[0]
    sale = (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()
    conn.execute("INSERT INTO orders (user_id, product_id, kind, product_name, quantity, fields_json, unit_price, "
                 "total_micro, cost_micro, status, supplier_idem_key, idempotent_supply, source, created_at, "
                 "updated_at, api_key_id) VALUES (?, 'x', 'topup', 'x', 1, '{}', '1', 1, 1, 'completed', 'k', 0, "
                 "'api', ?, ?, ?)", (uid, sale, sale, key_id))
    assert _run(conn, config, 10)["reset"] == 1
    assert conn.execute("SELECT warn_count FROM bots WHERE id = ?", (bid,)).fetchone()[0] == 0


def test_total_limit_and_admin_bots_untouched(config, conn, monkeypatch):
    import pytest

    from donatix import sitecfg
    uid, bid, sent = _setup(config, conn, monkeypatch)
    sitecfg.save(conn, config, {"max_bots_total": "1"})
    with pytest.raises(bots.BotError):
        bots.create(conn, config, user_id=uid, token="2:BBBBBB", admin_ids="1", username="b2")
    admin = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()[0]
    conn.execute("UPDATE bots SET user_id = ? WHERE id = ?", (admin, bid))
    assert _run(conn, config, 30) == {"warned": 0, "disabled": 0, "reset": 0}
