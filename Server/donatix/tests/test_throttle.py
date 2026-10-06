import threading
import time

import pytest

from donatix.throttle import QueueTimeout, Throttle


def test_limit_and_queue_not_reject():
    t = Throttle(per_minute=5, reserve=1, window=0.5)
    for _ in range(5):
        assert t.acquire(max_wait=0.1) < 0.05          # первые 5 — сразу
    waited = t.acquire(max_wait=2)                      # шестой ждёт, а не получает отказ
    assert 0.3 < waited < 1.0
    assert t.status()["per_minute"] == 5


def test_timeout_when_queue_too_long():
    t = Throttle(per_minute=1, reserve=0, window=5)
    t.acquire()
    with pytest.raises(QueueTimeout):
        t.acquire(max_wait=0.2)


def test_background_leaves_room_for_customers():
    t = Throttle(per_minute=5, reserve=2, window=0.6)
    for _ in range(3):
        t.acquire(background=True)
    with pytest.raises(QueueTimeout):                   # фону больше нельзя в этом окне
        t.acquire(background=True, max_wait=0.1)
    assert t.acquire(max_wait=0.1) < 0.05               # а покупке — можно сразу
    assert t.acquire(max_wait=0.1) < 0.05


def test_customer_goes_before_waiting_background():
    t = Throttle(per_minute=2, reserve=0, window=0.5)
    t.acquire()
    t.acquire()
    order = []

    def bg():
        t.acquire(background=True, max_wait=5)
        order.append("фон")

    th = threading.Thread(target=bg)
    th.start()
    time.sleep(0.05)
    t.acquire(max_wait=5)
    order.append("покупка")
    th.join()
    assert order[0] == "покупка"


def test_supplier_uses_queue(monkeypatch):
    import httpx

    from donatix import throttle
    from donatix.suppliers.fazer import FazerSupplier
    calls = []
    monkeypatch.setattr(throttle, "SUPPLIER", Throttle(per_minute=50))
    real = throttle.SUPPLIER.acquire
    monkeypatch.setattr(throttle.SUPPLIER, "acquire", lambda **kw: calls.append(kw) or real(**kw))
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "balance": 5}))
    s = FazerSupplier("k", transport=ok)
    s.balance()
    assert calls and calls[0]["background"] is True
