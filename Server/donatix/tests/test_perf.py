import threading
import time

from donatix import cache, db


def test_cache_computed_once_under_concurrency():
    cache.clear("t:")
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return 42

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get_or_set("t:x", 60, slow))) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [42] * 20 and len(calls) == 1          # не «лавина» из 20 одинаковых запросов


def test_stale_value_served_while_one_refreshes():
    cache.clear("t:")
    cache.get_or_set("t:y", 0.01, lambda: "old")
    time.sleep(0.02)
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.3)
        return "new"

    t = threading.Thread(target=lambda: cache.get_or_set("t:y", 60, slow))
    t.start()
    started.wait(1)
    t0 = time.monotonic()
    assert cache.get_or_set("t:y", 60, lambda: "other") == "old"    # не ждёт пересчёта
    assert time.monotonic() - t0 < 0.1
    t.join()
    assert cache.get_or_set("t:y", 60, lambda: "other") == "new"


def test_pool_reuses_and_rolls_back(tmp_path):
    path = tmp_path / "p.db"
    db.init(path)
    pool = db.pool(path)
    c = pool.acquire()
    c.execute("BEGIN")
    c.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")
    pool.release(c)                                     # недописанная транзакция откатилась
    c2 = pool.acquire()
    assert c2 is c and c2.execute("SELECT value FROM settings WHERE key = 'x'").fetchone() is None
    pool.release(c2)


def test_api_key_activity_written_at_most_once_a_minute(client, conn):
    from conftest import make_client
    _, key = make_client(conn)
    client.get("/api/v1/balance", headers={"X-API-Key": key})
    first = conn.execute("SELECT last_used_at FROM api_keys").fetchone()[0]
    client.get("/api/v1/balance", headers={"X-API-Key": key})
    assert conn.execute("SELECT last_used_at FROM api_keys").fetchone()[0] == first


def test_admin_change_reaches_other_processes(config, conn):
    """Сайт в нескольких процессах: наценка, поменянная в одном, доходит до другого."""
    import dataclasses
    from decimal import Decimal

    from donatix import sitecfg
    other = dataclasses.replace(config, markups=dict(config.markups))     # «второй процесс»
    cache.get_or_set("t:stale", 600, lambda: "old")
    cache.sync_epoch(conn)
    sitecfg.save(conn, config, {"markup_bronze": "33"})
    cache._store["t:stale"] = (time.monotonic() + 600, "old")              # кеш второго процесса
    cache._epoch["seen"] = "до правки"
    sitecfg._refreshed["at"] = 0
    sitecfg.refresh(conn, other)
    assert other.markups["bronze"] == Decimal("33")
    assert cache.get_or_set("t:stale", 600, lambda: "new") == "new"        # кеш сброшен по отметке


def test_leader_lock_single_owner(config):
    from donatix import leader
    assert leader.try_lock(config, "t-lead")
    import subprocess
    import sys
    code = ("import sys; sys.path.insert(0, %r); from donatix import leader\n"
            "class C: db_path = %r\n"
            "print(leader.try_lock(C, 't-lead'))") % (str(__import__('pathlib').Path(__file__).parents[2]),
                                                     str(config.db_path))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "False"                                 # другой процесс замок не получит
    leader.release("t-lead")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "True"


def test_bots_state_shared_between_processes(conn):
    import json

    from donatix import bots
    assert bots.RUNNER is None
    assert not bots.runner_alive(conn)
    db.set_setting(conn, "bots.state", json.dumps({"beat": time.time(), "bots": {"5": {"running": True}}}))
    assert bots.runner_alive(conn)
