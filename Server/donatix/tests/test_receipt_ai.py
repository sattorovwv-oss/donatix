"""ИИ читает чек: тот же перевод второй раз не пройдёт, новый — уходит админу с данными чека."""
import json
from datetime import datetime, timedelta, timezone

import httpx
from conftest import RECEIPT_PNG, web_login
from fastapi.testclient import TestClient

from donatix import accounts, receipt_ai

NOW = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")   # время в Душанбе
SEEN = {"is_receipt": True, "bank": "Алиф", "amount": 545, "currency": "TJS", "datetime": NOW,
        "txn_id": "AB-1234567", "recipient": "+992 90 000 00 00", "status": "success"}


def _client(app, config, conn, n):
    config.pay_methods = {"alif": "Алиф: +992 90 000 00 00"}
    accounts.create_user(conn, email=f"r{n}@example.com", login=f"rcpt{n}", password="password123", status="active")
    c = TestClient(app)
    return c, web_login(c, f"r{n}@example.com", "password123")


def _send(c, token, body):
    return c.post("/panel/balance", data={"csrf": token, "method": "alif", "amount": "50"},
                  files={"receipt": ("chek.png", body, "image/png")})


def test_same_transfer_cannot_be_used_twice(app, config, conn, monkeypatch):
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: None)
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean(SEEN))
    a, ta = _client(app, config, conn, 1)
    assert "Заявка #1 создана" in _send(a, ta, RECEIPT_PNG).text
    row = conn.execute("SELECT receipt_txn, receipt_fp, receipt_ai FROM payments WHERE id = 1").fetchone()
    assert row["receipt_txn"] == "AB1234567" and row["receipt_fp"] and json.loads(row["receipt_ai"])["bank"] == "Алиф"
    b, tb = _client(app, config, conn, 2)
    r = _send(b, tb, RECEIPT_PNG + b"other-screenshot")                 # другой файл, тот же перевод
    assert "Чек не прошёл проверку" in r.text and "заявка" not in r.text.split("Чек не прошёл")[1][:80]
    assert conn.execute("SELECT COUNT(*) FROM payments WHERE receipt_file IS NOT NULL").fetchone()[0] == 1


def test_same_amount_time_bank_without_number_is_caught(app, config, conn, monkeypatch):
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: None)
    no_number = {**SEEN, "txn_id": ""}
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean(no_number))
    a, ta = _client(app, config, conn, 1)
    _send(a, ta, RECEIPT_PNG)
    b, tb = _client(app, config, conn, 2)
    assert "Чек не прошёл проверку" in _send(b, tb, RECEIPT_PNG + b"x").text


def test_new_receipt_goes_to_admin_with_what_ai_read(app, config, conn, monkeypatch):
    captions = []
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda cfg, caption, *a, **k: captions.append(caption))
    config.openai_api_key = "sk-test"
    config.alert_telegram_chat_id = "777"
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean(SEEN))
    a, ta = _client(app, config, conn, 1)
    assert "Заявка #1 создана" in _send(a, ta, RECEIPT_PNG).text
    assert captions and f"🤖 Чек: Алиф · 545 TJS · {NOW} · № AB-1234567" in captions[-1]
    admin = TestClient(app)
    web_login(admin, "admin@example.com", "adminpass123")
    assert "🤖 Чек: Алиф" in admin.get("/admin/payments?status=pending").text


def test_amount_check_and_reading_via_openai():
    assert receipt_ai.amount_matches(receipt_ai.clean(SEEN), "545", "TJS") is True
    assert receipt_ai.amount_matches(receipt_ai.clean(SEEN), "600", "TJS") is False
    assert "НЕ совпадает" in receipt_ai.summary(receipt_ai.clean(SEEN), "600", "TJS")
    assert receipt_ai.txn_key({"txn_id": "12"}) == ""                       # слишком короткий — не надёжен

    seen_body = {}

    def handler(request):
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(SEEN)}}]})

    from donatix.config import Config
    cfg = Config(secret_key="x", db_path="x", openai_api_key="sk-test")
    got = receipt_ai.read(cfg, RECEIPT_PNG, "png", transport=httpx.MockTransport(handler))
    assert got["txn_id"] == "AB-1234567" and got["amount"] == 545.0
    url = seen_body["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert receipt_ai.read(cfg, b"%PDF-1.4", "pdf") is None                 # PDF не читаем — проверит админ
    assert receipt_ai.read(Config(secret_key="x", db_path="x"), RECEIPT_PNG, "png") is None   # без ключа


def test_client_page_tells_nothing_about_how_receipts_are_checked(app, config, conn):
    a, _ = _client(app, config, conn, 1)
    page = a.get("/panel/balance").text
    assert "Проверяем чек" in page
    for secret in ("ИИ", "в базе", "номер операции", "Читаем чек", "OpenAI", "ChatGPT"):
        assert secret not in page, secret


def _png_with_text(key: bytes, value: bytes) -> bytes:
    import struct
    import zlib
    body = key + b"\0" + value
    chunk = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body))
    return RECEIPT_PNG[:33] + chunk + RECEIPT_PNG[33:]


def test_foreign_requisites_are_not_accepted_and_admin_gets_reasons(app, config, conn, monkeypatch):
    alerts = []
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda *a, **k: None)
    monkeypatch.setattr("donatix.worker.notify_admin", lambda cfg, text, *a, **k: alerts.append(text))
    foreign = {**SEEN, "recipient": "9762 **** **** 3253"}
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean(foreign))
    a, ta = _client(app, config, conn, 1)
    r = _send(a, ta, RECEIPT_PNG)
    assert "Чек не прошёл проверку" in r.text and "реквизит" not in r.text.split("Чек не прошёл")[1][:200]
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    import time
    for _ in range(50):
        if alerts:
            break
        time.sleep(0.02)
    assert alerts and "не наши реквизиты" in alerts[0] and "3253" in alerts[0]


def test_ai_says_fake_is_rejected_and_suspicious_goes_to_admin(app, config, conn, monkeypatch):
    monkeypatch.setattr("donatix.worker.notify_admin", lambda *a, **k: None)
    captions = []
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda cfg, caption, *a, **k: captions.append(caption))
    config.alert_telegram_chat_id = "777"
    config.openai_api_key = "sk-test"
    fake = {**SEEN, "forgery": "fake", "signs": ["сумма другим шрифтом"]}
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean(fake))
    a, ta = _client(app, config, conn, 1)
    assert "Чек не прошёл проверку" in _send(a, ta, RECEIPT_PNG).text
    odd = {**SEEN, "forgery": "suspicious", "signs": ["размытие вокруг суммы"]}
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean(odd))
    assert "создана" in _send(a, ta, RECEIPT_PNG).text
    assert "Есть признаки правки чека: размытие вокруг суммы" in captions[-1]


def test_verdict_rules():
    ours = "Алиф: +992 90 000 00 00"
    ok = receipt_ai.clean({**SEEN, "datetime": "2026-10-01 14:32"})
    assert receipt_ai.verdict(ok, RECEIPT_PNG, "png", ours, "2026-10-01T09:40") == []
    assert receipt_ai.verdict(receipt_ai.clean({**SEEN, "is_receipt": False}), b"", "png", ours)
    assert receipt_ai.verdict(receipt_ai.clean({**SEEN, "status": "failed"}), b"", "png", ours)
    by_name = receipt_ai.clean({**SEEN, "recipient": "Иван П.", "recipient_ok": "no"})
    assert "не похож на наши" in receipt_ai.verdict(by_name, b"", "png", ours)[0]
    assert receipt_ai.verdict(receipt_ai.clean({**SEEN, "recipient": "Иван П."}), b"", "png", ours) == []
    old = receipt_ai.verdict(ok, b"", "png", ours, "2026-10-09T10:00")
    assert old and "старый" in old[0]
    assert "будущем" in receipt_ai.verdict(ok, b"", "png", ours, "2026-09-25T10:00")[0]


def test_file_traces_of_editors_and_ai():
    assert receipt_ai.file_marks(RECEIPT_PNG, "png") == ([], [])
    assert "редактор «picsart»" in receipt_ai.file_marks(_png_with_text(b"Software", b"PicsArt Photo Studio"),
                                                          "png")[0][0]
    assert "нейросетью" in receipt_ai.file_marks(_png_with_text(b"XML:com.adobe.xmp",
                                                               b"digitalSourceType trainedAlgorithmicMedia"),
                                                 "png")[0][0]
    assert receipt_ai.file_marks(_png_with_text(b"Comment", b"canvas size"), "png") == ([], [])   # не «canva»
    jpg = b"\xff\xd8\xff\xe1\x00\x14Exif\0\0Photoshop 25\xff\xda\x00\x02" + b"OPENAI" * 3
    reject, _ = receipt_ai.file_marks(jpg, "jpg")
    assert len(reject) == 1 and "photoshop" in reject[0]                    # пиксели после SOS не смотрим
    pdf = b"%PDF-1.4\n/Producer (iLovePDF)\n%%EOF\nmore\n%%EOF"
    reject, warn = receipt_ai.file_marks(pdf, "pdf")
    assert "ilovepdf" in reject[0] and warn


def test_prompt_carries_our_requisites():
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {**SEEN, "forgery": "fake", "signs": ["шрифт"], "recipient_ok": "no"})}}]})

    from donatix.config import Config
    cfg = Config(secret_key="x", db_path="x", openai_api_key="sk-test")
    got = receipt_ai.read(cfg, RECEIPT_PNG, "png", transport=httpx.MockTransport(handler), our_details="Алиф 102208383")
    assert "102208383" in sent["messages"][0]["content"][0]["text"]
    assert got["forgery"] == "fake" and got["signs"] == ["шрифт"] and got["recipient_ok"] == "no"
    assert "🚨 Чек изменён / ненастоящий: шрифт" in receipt_ai.summary(got)


def test_wrong_currency_in_request_is_fixed_by_receipt(app, config, conn, monkeypatch):
    """Ввёл $20, а перевёл 20 сомони — заявка исправляется на 20 сомони, зачислится ровно столько."""
    captions = []
    monkeypatch.setattr("donatix.worker.notify_admin_file", lambda cfg, caption, *a, **k: captions.append(caption))
    config.openai_api_key = "sk-test"
    config.alert_telegram_chat_id = "777"
    monkeypatch.setattr(receipt_ai, "read", lambda cfg, data, ext, **k: receipt_ai.clean({**SEEN, "amount": 20}))
    a, ta = _client(app, config, conn, 1)
    r = a.post("/panel/balance", data={"csrf": ta, "method": "alif", "amount": "20"},
               files={"receipt": ("chek.png", RECEIPT_PNG, "image/png")})
    assert "Заявка #1 создана" in r.text
    p = conn.execute("SELECT pay_amount, amount_micro FROM payments WHERE id = 1").fetchone()
    assert p["pay_amount"] == "20.00" and p["amount_micro"] < 30_000          # ≈ $1.9, а не $20
    assert "заявка исправлена по чеку: было" in captions[-1]
