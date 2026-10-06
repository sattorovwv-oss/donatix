"""Push-уведомления: ключи, подписка устройства, отправка при событиях, удаление отписавшихся."""
import base64
import json
import time

from conftest import csrf_of, make_client, web_login
from fastapi.testclient import TestClient

from donatix import db, notify, webpush


def _device():
    """Настоящие ключи «браузера» — чтобы проверить шифрование целиком."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import os
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()   # noqa: E731
    return b64(pub), b64(os.urandom(16))


def _wait(cond, sec=5):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_subscribe_and_receive_push_on_notification(app, config, conn, monkeypatch):
    uid, _ = make_client(conn, login="pushy")
    c = TestClient(app)
    web_login(c, "pushy@example.com", "password123")
    info = c.get("/panel/push/key").json()
    assert info["ok"] and len(info["key"]) > 80 and info["devices"] == 0
    assert c.get("/panel/push/key").json()["key"] == info["key"]          # ключ один и тот же
    token = csrf_of(c.get("/panel/notifications").text)
    p256dh, auth = _device()
    r = c.post("/panel/push/subscribe", data={"csrf": token, "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                                              "p256dh": p256dh, "auth": auth})
    assert r.json()["ok"] and webpush.count(conn, uid) == 1
    assert c.post("/panel/push/subscribe", data={"csrf": token, "endpoint": "http://evil",
                                                 "p256dh": "x", "auth": "y"}).status_code == 400

    sent = []

    class Resp:
        status_code = 201
        text = ""
        reason = "Created"
        headers: dict = {}

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        sent.append((url, data, headers))
        return Resp()

    monkeypatch.setattr("requests.post", fake_post)
    notify.notify(conn, config, uid, "Заказ dx-5 выполнен: 100 алмазов", "/panel/orders/dx-5")
    assert _wait(lambda: sent)
    url, body, headers = sent[0]
    assert url.startswith("https://fcm.googleapis.com/") and headers["Content-Encoding"] == "aes128gcm"
    assert headers["Authorization"].startswith("vapid t=") and len(body) > 100   # зашифровано, подписано

    assert webpush.title_for("Заказ dx-5 выполнен") == "✅ Заказ выполнен"
    assert webpush.title_for("Заявка #3 отклонена") == "❌ Заявка отклонена"
    assert webpush.title_for("Баланс пополнен на $5") == "💰 Баланс пополнен"


def test_gone_device_is_removed(app, config, conn, monkeypatch):
    uid, _ = make_client(conn, login="gone")
    p256dh, auth = _device()
    webpush.subscribe(conn, uid, "https://updates.push.services.mozilla.com/wpush/v2/x", p256dh, auth)

    class Gone:
        status_code = 410
        text = "gone"
        reason = "Gone"
        headers: dict = {}

    monkeypatch.setattr("requests.post", lambda *a, **k: Gone())
    webpush.send(conn, config, uid, "Баланс пополнен", "/panel")
    other = db.connect(config.db_path)
    assert _wait(lambda: other.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0] == 0)


def test_service_worker_shows_push(client):
    sw = client.get("/sw.js").text
    assert 'addEventListener("push"' in sw and "notificationclick" in sw and "badge-96.png" in sw
    assert client.get("/static/app/badge-96.png").status_code == 200
    assert json.loads(json.dumps({"ok": True}))["ok"]
