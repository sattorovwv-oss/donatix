"""Чтение чеков ИИ (тот же ключ OpenAI, что у бота поддержки) и защита от повторных чеков.

Когда клиент присылает чек, модель с «зрением» читает: банк, сумму, валюту, дату и время,
номер операции, получателя. Сайт запоминает это у заявки и ищет по базе:
  • тот же номер операции — чек уже был;
  • та же сумма + валюта + время до минуты + банк — тот же перевод, даже если скриншот другой.
Повтор отклоняется сразу, новый чек уходит админу вместе с тем, что прочитал ИИ.
Не принимаем вовсе (verdict): получатель не наши реквизиты, ИИ видит подделку, «не чек», перевод не прошёл,
дата в будущем или старше 3 дней, а также файл из фоторедактора или нейросети (метки в EXIF/XMP/C2PA).

Нет ключа, PDF или ИИ не ответил — заявка всё равно принимается, админ проверяет как раньше.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
from typing import Any

import httpx

from .config import Config

log = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 25
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

PROMPT = (
    "Это скриншот или фото банковского чека/перевода (банки Таджикистана: Алиф, Душанбе Сити, Эсхата, "
    "Амонатбонк, Спитамен и др.; также крипто-переводы). Прочитай его и верни ТОЛЬКО JSON:\n"
    '{"is_receipt": true/false, "bank": "название банка или кошелька", "amount": число, '
    '"currency": "TJS|USD|RUB|USDT|…", "datetime": "YYYY-MM-DD HH:MM", '
    '"txn_id": "номер операции/квитанции/транзакции", "recipient": "получатель: номер карты, телефона или имя", '
    '"status": "success|pending|failed|unknown", "recipient_ok": "yes|no|unknown", '
    '"forgery": "real|suspicious|fake", "signs": ["признак подделки", …]}\n'
    "Если поля нет на изображении — пустая строка (для amount — 0). Ничего не придумывай.\n"
    "recipient_ok — совпадает ли получатель в чеке с НАШИМИ реквизитами (ниже): номер карты/телефона/счёта "
    "(учитывай маску ****1234 и пробелы) или имя (учитывай латиницу/кириллицу). Не видно получателя — unknown.\n"
    "Затем как эксперт-криминалист проверь, не подделан ли чек. Ищи:\n"
    "• цифры суммы, даты, номера или получателя другим шрифтом, размером, жирностью, цветом или сглаживанием, "
    "чем остальной текст; неровная строка, сдвиг, разный межбуквенный интервал;\n"
    "• пятна другого фона, размытие, «ореол», следы ластика или наложенный прямоугольник вокруг цифр;\n"
    "• несходящиеся числа (сумма + комиссия ≠ итого, разные суммы в разных местах), невозможная дата/время, "
    "время в строке состояния телефона РАНЬШЕ времени операции;\n"
    "• оформление не как в настоящем приложении этого банка, ошибки и странные слова, лишние/пропавшие поля;\n"
    "• признаки картинки, созданной ИИ (ChatGPT, Gemini и т.п.) или собранной в редакторе: «слишком чистый» "
    "рендер, искажённые буквы и логотипы, бессмысленный текст, нет элементов интерфейса телефона;\n"
    "• фото экрана другого устройства или распечатки вместо скриншота.\n"
    "forgery: real — признаков нет; suspicious — есть сомнения; fake — явные признаки правки или подделки. "
    "signs — коротко по-русски, что именно не так (пусто, если real)."
)


def enabled(config: Config) -> bool:
    return bool(config.openai_api_key)


def read(config: Config, data: bytes, ext: str, transport: httpx.BaseTransport | None = None,
         our_details: str = "") -> dict[str, Any] | None:
    """Прочитать чек. None — не получилось (нет ключа, PDF, ошибка) — тогда проверяет только админ."""
    mime = MIME.get(ext)
    if not enabled(config) or mime is None:
        return None
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    body = {
        "model": config.receipt_model, "temperature": 0, "max_tokens": 700,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT + "\nНАШИ реквизиты: " + (re.sub(r"\s+", " ", our_details)[:500]
                                                                   or "не указаны")},
            {"type": "image_url", "image_url": {"url": url, "detail": "high"}}]}],
    }
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            resp = client.post(OPENAI_URL, json=body, headers={"Authorization": f"Bearer {config.openai_api_key}"})
        if resp.status_code != 200:
            log.warning("чек: OpenAI %s %s", resp.status_code, resp.text[:200])
            return None
        raw = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001 — чтение чека не должно мешать пополнению
        log.exception("чек: не прочитан")
        return None
    return clean(raw)


def clean(raw: dict[str, Any]) -> dict[str, Any]:
    """Привести ответ модели к одному виду."""
    def s(key: str) -> str:
        # Одна строка: текст на картинке не должен дописывать строки в сообщение админу
        return re.sub(r"\s+", " ", str(raw.get(key) or "")).strip()[:120]
    try:
        amount = round(float(str(raw.get("amount") or 0).replace(" ", "").replace(",", ".")), 2)
    except ValueError:
        amount = 0.0
    signs = raw.get("signs") if isinstance(raw.get("signs"), list) else []
    return {"is_receipt": bool(raw.get("is_receipt", True)), "bank": s("bank"), "amount": amount,
            "currency": s("currency").upper()[:8], "datetime": s("datetime")[:16], "txn_id": s("txn_id"),
            "recipient": s("recipient"), "status": s("status").lower() or "unknown",
            "recipient_ok": _pick(raw.get("recipient_ok"), ("yes", "no"), "unknown"),
            "forgery": _pick(raw.get("forgery"), ("suspicious", "fake"), "real"),
            "signs": [re.sub(r"\s+", " ", str(x)).strip()[:150] for x in signs if str(x).strip()][:6]}


def _pick(value: Any, allowed: tuple[str, ...], default: str) -> str:
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def txn_key(d: dict[str, Any]) -> str:
    """Номер операции без пробелов и знаков. Короче 5 символов — не надёжен, не используем."""
    key = re.sub(r"[^0-9A-Za-z]", "", d.get("txn_id") or "").upper()
    return key if len(key) >= 5 else ""


def fingerprint(d: dict[str, Any]) -> str:
    """Тот же перевод: сумма + валюта + время до минуты + банк. Без суммы или времени — не считаем."""
    when = re.sub(r"[^0-9]", "", d.get("datetime") or "")
    if not d.get("amount") or len(when) < 12:
        return ""
    bank = re.sub(r"[^a-zа-я0-9]", "", (d.get("bank") or "").lower())
    return f"{d['amount']:.2f}|{d.get('currency') or ''}|{when[:12]}|{bank}"


def duplicate(conn: sqlite3.Connection, payment_id: int, d: dict[str, Any]) -> sqlite3.Row | None:
    """Заявка с тем же чеком (ждёт проверки или уже зачислена)."""
    key, fp = txn_key(d), fingerprint(d)
    if key:
        row = conn.execute("SELECT id, status FROM payments WHERE receipt_txn = ? AND id != ? "
                           "AND status IN ('pending', 'paid', 'rejected')", (key, payment_id)).fetchone()
        if row:
            return row
    if fp:
        return conn.execute("SELECT id, status FROM payments WHERE receipt_fp = ? AND id != ? "
                            "AND status IN ('pending', 'paid', 'rejected')", (fp, payment_id)).fetchone()
    return None


def amount_matches(d: dict[str, Any], pay_amount: Any, pay_currency: str) -> bool | None:
    """Совпадает ли сумма чека с заявкой. None — сравнить нечем."""
    try:
        want = float(str(pay_amount).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not d.get("amount") or not want:
        return None
    if d.get("currency") and pay_currency and d["currency"] != pay_currency.upper():
        return False
    return abs(d["amount"] - want) <= max(0.01, want * 0.005)


def recipient_matches(d: dict[str, Any], our_details: str) -> bool | None:
    """Совпадает ли получатель в чеке с нашими реквизитами: сравниваем последние 4 цифры
    номера карты/телефона. None — сравнить нечем."""
    seen = re.sub(r"\D", "", d.get("recipient") or "")
    ours = [re.sub(r"\D", "", x) for x in re.findall(r"[\d][\d\s-]{5,}\d", our_details or "")]
    ours = [x for x in ours if len(x) >= 6]
    if len(seen) < 4 or not ours:
        return None
    return any(x[-4:] == seen[-4:] for x in ours)


def time_problem(d: dict[str, Any], created_at: str | None, tz_hours: int = 5) -> str:
    """Чек сделан задолго ДО заявки — частый признак старого или чужого чека."""
    raw = re.sub(r"[^0-9]", "", d.get("datetime") or "")
    if len(raw) < 12 or not created_at:
        return ""
    from datetime import datetime, timedelta
    try:
        when = datetime.strptime(raw[:12], "%Y%m%d%H%M") - timedelta(hours=tz_hours)   # местное → UTC
        made = datetime.strptime(created_at[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return ""
    if when < made - timedelta(hours=2):
        return f"⚠️ Чек сделан раньше заявки ({d.get('datetime')}) — возможно, старый или чужой"
    if when > made + timedelta(days=2):
        return "⚠️ Дата в чеке в будущем — возможно, чек изменён"
    return ""


# ── Следы в самом файле: чем его сделали ──

_AI_MARKS = (b"trainedalgorithmicmedia", b"openai", b"dall-e", b"dall\xc2\xb7e", b"chatgpt", b"midjourney",
             b"stable diffusion", b"google ai", b"firefly", b"leonardo.ai", b"ideogram")
_EDITORS = (b"photoshop", b"picsart", b"snapseed", b"gimp", b"canva", b"pixelmator", b"fotor", b"meitu",
            b"lightroom", b"photopea", b"adobe express", b"polarr", b"facetune", b"phonto", b"ibis paint",
            b"inshot", b"photo editor", b"photodirector", b"paint.net", b"affinity photo", b"krita")
_PDF_EDITORS = (b"ilovepdf", b"smallpdf", b"sejda", b"pdfescape", b"pdffiller", b"pdf-xchange", b"foxit pdf editor",
                b"acrobat pro", b"microsoft word", b"microsoft\xc2\xae word", b"libreoffice", b"canva", b"photoshop",
                b"wps office", b"pdf editor", b"google docs")


def _meta(data: bytes, ext: str) -> bytes:
    """Только служебные части файла (EXIF, XMP, текстовые блоки) — не сами пиксели:
    в сжатых пикселях случайно встречается что угодно."""
    out: list[bytes] = []
    if ext == "jpg":
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker == 0xDA:          # дальше — картинка
                break
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 1 if marker == 0xFF else 2
                continue
            size = int.from_bytes(data[i + 2:i + 4], "big")
            out.append(data[i + 4:i + 2 + size])
            i += 2 + size
    elif ext == "png":
        i = 8
        while i + 8 <= len(data):
            size, kind = int.from_bytes(data[i:i + 4], "big"), data[i + 4:i + 8]
            if kind != b"IDAT":
                out.append(kind + data[i + 8:i + 8 + size])
            if kind == b"IEND":
                break
            i += 12 + size
    elif ext == "webp":
        i = 12
        while i + 8 <= len(data):
            kind, size = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
            if kind not in (b"VP8 ", b"VP8L", b"ALPH", b"ANMF"):
                out.append(kind + data[i + 8:i + 8 + size])
            i += 8 + size + (size & 1)
    elif ext == "pdf":
        # описание документа и XMP лежат открытым текстом; сжатые потоки не трогаем
        out.extend(m.group(0) for m in re.finditer(rb"/(Producer|Creator)\s*\((?:[^()\\]|\\.){0,200}\)", data))
        out.extend(m.group(0) for m in re.finditer(rb"<x:xmpmeta.{0,20000}?</x:xmpmeta>", data, re.S))
    return b"\n".join(out).lower()


def _first(meta: bytes, marks: tuple[bytes, ...]) -> bytes | None:
    """Метка целым словом: «canva», но не «canvas»."""
    return next((m for m in marks if re.search(rb"(?<![a-z])" + re.escape(m) + rb"(?![a-z])", meta)), None)


def file_marks(data: bytes, ext: str) -> tuple[list[str], list[str]]:
    """(почему отклонить, что показать админу) по служебным данным файла."""
    meta = _meta(data or b"", ext or "")
    reject: list[str] = []
    warn: list[str] = []
    ai = _first(meta, _AI_MARKS)
    if ai or (b"c2pa" in meta and b"trainedalgorithmic" in meta):
        reject.append(f"файл создан нейросетью (метка «{(ai or b'c2pa').decode(errors='ignore')}»)")
    editor = _first(meta, _PDF_EDITORS if ext == "pdf" else _EDITORS)
    if editor:
        reject.append(f"файл прошёл через редактор «{editor.decode(errors='ignore')}»")
    if ext == "pdf" and (data or b"").count(b"%%EOF") > 1:
        warn.append("PDF меняли после создания (несколько версий в файле)")
    return reject, warn


def verdict(d: dict[str, Any] | None, data: bytes, ext: str, our_details: str = "",
            created_at: str | None = None, tz_hours: int = 5) -> list[str]:
    """Почему чек НЕЛЬЗЯ принимать (пусто — можно). Причины — для админа, клиенту их не показываем."""
    reasons, _ = file_marks(data, ext)
    if not d:
        return reasons
    if not d.get("is_receipt", True):
        reasons.append("это не чек о переводе")
    if d.get("status") == "failed":
        reasons.append("в чеке перевод не прошёл")
    rm = recipient_matches(d, our_details)
    if rm is False:
        reasons.append(f"получатель «{d.get('recipient')}» — не наши реквизиты")
    elif rm is None and d.get("recipient_ok") == "no" and d.get("recipient"):
        reasons.append(f"получатель «{d.get('recipient')}» не похож на наши реквизиты")
    if d.get("forgery") == "fake":
        signs = "; ".join(d.get("signs") or [])
        reasons.append("чек изменён или ненастоящий" + (f": {signs}" if signs else ""))
    raw = re.sub(r"[^0-9]", "", d.get("datetime") or "")
    if len(raw) >= 12 and created_at:
        from datetime import datetime, timedelta
        try:
            when = datetime.strptime(raw[:12], "%Y%m%d%H%M") - timedelta(hours=tz_hours)
            made = datetime.strptime(created_at[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return reasons
        if when > made + timedelta(days=1):
            reasons.append(f"дата в чеке в будущем ({d.get('datetime')})")
        elif when < made - timedelta(days=3):
            reasons.append(f"чек старый ({d.get('datetime')})")
    return reasons


def summary(d: dict[str, Any] | None, pay_amount: Any = None, pay_currency: str = "",
            our_details: str = "", created_at: str | None = None) -> str:
    """Строка для админа: что прочитал ИИ и совпадает ли сумма."""
    if not d:
        return "🤖 Чек не прочитан автоматически — проверьте вручную."
    parts = [p for p in (d["bank"], f"{d['amount']:g} {d['currency']}".strip() if d["amount"] else "",
                         d["datetime"], f"№ {d['txn_id']}" if d["txn_id"] else "") if p]
    line = "🤖 Чек: " + (" · ".join(parts) or "данных не видно")
    match = amount_matches(d, pay_amount, pay_currency)
    if d.get("fixed_from"):
        line += (f"\n✏️ Клиент ошибся суммой — заявка исправлена по чеку: было {d['fixed_from']}, "
                 f"стало {pay_amount} {pay_currency}. Сверьте поступление в банке")
    elif match is True:
        line += "\n✅ Сумма совпадает с заявкой"
    elif match is False:
        line += f"\n⚠️ Сумма НЕ совпадает с заявкой ({pay_amount} {pay_currency})"
    if not d["is_receipt"]:
        line += "\n⚠️ Похоже, это не чек"
    if d["status"] == "failed":
        line += "\n⚠️ В чеке перевод не прошёл"
    if d.get("recipient"):
        rm = recipient_matches(d, our_details)
        if rm is False:
            line += f"\n🚨 Получатель в чеке ({d['recipient']}) НЕ наши реквизиты"
        elif rm is True:
            line += "\n✅ Получатель — наши реквизиты"
    elif d.get("recipient_ok") == "no":
        line += "\n🚨 Получатель в чеке не похож на наши реквизиты"
    problem = time_problem(d, created_at)
    if problem:
        line += "\n" + problem
    if d.get("forgery") in ("suspicious", "fake"):
        head = "🚨 Чек изменён / ненастоящий" if d["forgery"] == "fake" else "⚠️ Есть признаки правки чека"
        line += f"\n{head}" + (": " + "; ".join(d.get("signs") or []) if d.get("signs") else "")
    for mark in d.get("file_marks") or []:
        line += f"\n⚠️ {mark}"
    return line
