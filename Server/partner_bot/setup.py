"""Мастер первой настройки: создаёт .env, ничего не спрашивая дважды.

Запуск:  python setup.py
         python setup.py --quick                  три вопроса и готово
         python setup.py --quick ТОКЕН ID КЛЮЧ    то же без вопросов
Работает на голом Python, ставить ничего не нужно.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
EXAMPLE = BASE / ".env.example"

TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def _open_input():
    """Ввод с терминала. Если stdin занят (скрипт пришёл по конвейеру
    из curl), читаем напрямую из /dev/tty."""
    if sys.stdin is not None and sys.stdin.isatty():
        return None
    try:
        return open("/dev/tty", encoding="utf-8")
    except OSError:
        return None


_TTY = _open_input()


def _read(prompt: str) -> str:
    if _TTY is None:
        return input(prompt)
    print(prompt, end="", flush=True)
    line = _TTY.readline()
    if not line:
        raise EOFError
    return line


def ask(prompt: str, *, current: str = "", validate=None, allow_empty: bool = False) -> str:
    """Спросить значение. Enter — оставить текущее."""
    while True:
        hint = f" [{current}]" if current else ""
        answer = _read(f"{prompt}{hint}: ").strip()
        if not answer:
            if current:
                return current
            if allow_empty:
                return ""
            print("  ⚠️  Это поле обязательно.")
            continue
        if validate:
            problem = validate(answer)
            if problem:
                print(f"  ⚠️  {problem}")
                continue
        return answer


def check_token(value: str) -> str | None:
    if not TOKEN_RE.match(value):
        return "Токен выглядит неправильно. Формат: 123456789:AAH..."
    return None


def check_id(value: str) -> str | None:
    parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    if not parts or not all(p.isdigit() for p in parts):
        return "ID — это только цифры. Несколько админов — через запятую."
    return None


PRIVATE_HOST_RE = re.compile(
    r"^https?://(?:10\.|127\.|0\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|localhost)"
)


def check_base_url(value: str) -> str | None:
    """Отсечь внутренние адреса: бот MyStars иногда выдаёт их по ошибке,
    а с чужого сервера такой адрес недостижим."""
    if not value.startswith(("http://", "https://")):
        return "Адрес должен начинаться с https://"
    if PRIVATE_HOST_RE.match(value):
        return ("Это адрес внутренней сети — с вашего сервера он недоступен. "
                "Используйте https://api.mystars.tg/v1")
    if value.startswith("http://"):
        return "Нужен https:// — по http ключ уйдёт открытым текстом."
    return None


def check_seed(value: str) -> str | None:
    words = value.split()
    if len(words) != 24:
        return f"Нужно ровно 24 слова, а получено {len(words)}."
    if not all(word.isalpha() for word in words):
        return "Сид-фраза состоит только из слов латиницей, без цифр и знаков."
    return None


def check_price(value: str) -> str | None:
    try:
        if float(value.replace(",", ".")) <= 0:
            return "Цена должна быть больше нуля."
    except ValueError:
        return "Введите число, например 0.25"
    return None


def to_diram(value: str) -> int:
    return round(float(value.replace(",", ".")) * 100)


def read_existing() -> dict[str, str]:
    if not ENV.exists():
        return {}
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.split("  #")[0].strip()
    return values


def write_env(values: dict[str, str]) -> None:
    """Пишем поверх .env.example, сохраняя комментарии."""
    lines = []
    written = set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                lines.append(f"{key}={values[key]}")
                written.add(key)
                continue
        lines.append(line)

    # Ключи, которых нет в шаблоне, дописываем в конец — иначе они
    # молча потерялись бы при следующей записи.
    extra = [key for key in values if key not in written]
    if extra:
        lines.append("")
        for key in extra:
            lines.append(f"{key}={values[key]}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_values(pairs: list[str]) -> None:
    """Неинтерактивная правка .env: setup.py --set КЛЮЧ=значение ...

    Нужна, чтобы настройку можно было сделать одной командой, без диалога.
    """
    values = read_existing()
    changed = []
    for pair in pairs:
        if "=" not in pair:
            print(f"❌ Непонятный аргумент: {pair}. Нужно КЛЮЧ=значение.")
            sys.exit(1)
        key, _, value = pair.partition("=")
        key = key.strip().upper()
        values[key] = value.strip()
        changed.append(key)

    problem = check_base_url(values["MYSTARS_BASE_URL"]) if values.get("MYSTARS_BASE_URL") else None
    if problem:
        print(f"❌ MYSTARS_BASE_URL: {problem}")
        sys.exit(1)

    write_env(values)
    print(f"✅ Записано в {ENV}: " + ", ".join(changed))



# --------------------------------------------------- быстрая активация
#
# Три вещи — токен, ID админа, ключ поставщика. Остальное (реквизиты,
# цены, поддержка) бот берёт по умолчанию, а владелец правит в /panel.
# Токен и ключ проверяем сразу вживую: опечатка здесь — самая частая
# причина «нажал активировать, а бот молчит».

FAZER_URL = "https://api.fzr.cards"


def _http_json(url: str, headers: dict | None = None, timeout: int = 15):
    """GET → (код, json). Код 0 — сети нет или сервер не ответил."""
    request = urllib.request.Request(url, headers={"User-Agent": "stars-bot-setup",
                                                   **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            code, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    return code, data if isinstance(data, dict) else {"data": data}


def verify_token(token: str) -> tuple[bool, str]:
    """(годится ли, пояснение). Нет сети — пропускаем, не отказываем."""
    code, data = _http_json(f"https://api.telegram.org/bot{token}/getMe")
    if code == 200 and data.get("ok"):
        return True, "@" + str(data.get("result", {}).get("username", "?"))
    if code in (401, 404):
        return False, "Telegram не принял токен. Скопируйте его заново у @BotFather."
    return True, "не проверил — нет связи с Telegram"


def verify_supplier(key: str) -> tuple[bool, str]:
    code, _ = _http_json(FAZER_URL + "/api/v2/balance", {"X-API-Key": key})
    if code in (401, 403):
        return False, "Поставщик не принял ключ. Проверьте, что скопирован целиком."
    if 200 <= code < 300:
        return True, "ключ принят"
    return True, "не проверил — поставщик не ответил, проверьте потом в /panel"


def check_key(value: str) -> str | None:
    if len(value) < 8 or " " in value:
        return "Ключ выглядит неправильно — скопируйте его целиком, без пробелов."
    return None


def quick_values(token: str, admin: str, key: str) -> dict[str, str]:
    """Что пишется в .env при быстрой активации."""
    admins = ",".join(p.strip() for p in admin.replace(";", ",").split(",") if p.strip())
    return {
        "BOT_TOKEN": token,
        "ADMIN_IDS": admins,
        # Один ключ на всё: звёзды, Premium, игры и API. Отдельный игровой
        # ключ не пишем — пустой значит «тот же счёт».
        "FRAGMENT_MODE": "fazer",
        "FAZER_API_KEY": key,
        "FAZER_BASE_URL": FAZER_URL,
    }


def quick(argv: list[str], *, replace: bool = False) -> int:
    """Быстрая активация. Возвращает код выхода.

    replace=False — отказ, если здесь уже стоит ДРУГОЙ бот. Установщик с
    тремя значениями, запущенный на сервере, где бот уже работает, иначе
    молча подменил бы его токен: работающий магазин пропал бы, а его
    клиенты попали бы в новый. Сменить токен сознательно — stars-bot
    activate, он передаёт replace=True.
    """
    old = read_existing()
    given = [a.strip() for a in argv]
    if given and len(given) != 3:
        print("Использование: setup.py --quick ТОКЕН ID_АДМИНА КЛЮЧ_ПОСТАВЩИКА")
        return 1

    if given:
        token, admin, key = given
        for problem in (check_token(token), check_id(admin), check_key(key)):
            if problem:
                print(f"❌ {problem}")
                return 1
    else:
        print("=" * 58)
        print("  Активация бота — три вопроса.")
        print("=" * 58)
        print("\n1) Токен бота — у @BotFather: /newbot или /mybots → API Token")
        token = ask("Токен", current=old.get("BOT_TOKEN", ""), validate=check_token)
        print("\n2) Ваш Telegram ID — у @userinfobot (только цифры)")
        admin = ask("ID админа", current=old.get("ADMIN_IDS", ""), validate=check_id)
        print("\n3) Ключ поставщика FazerCards (X-API-Key из кабинета)")
        key = ask("Ключ", current=old.get("FAZER_API_KEY", ""), validate=check_key)

    current = old.get("BOT_TOKEN", "")
    if current and current != token and not replace and check_token(current) is None:
        print("❌ На этом сервере уже стоит другой бот — его токен не трогаю.")
        print("   Второй бот ставьте на другой сервер.")
        print("   Сменить токен у этого бота: sudo stars-bot activate")
        return 1

    print()
    good, note = verify_token(token)
    print(("  ✅ Токен: " if good else "  ❌ ") + note)
    if not good:
        return 1
    good, note = verify_supplier(key)
    print(("  ✅ Поставщик: " if good else "  ❌ ") + note)
    if not good:
        return 1

    values = dict(old)
    values.update(quick_values(token, admin, key))
    write_env(values)
    print(f"\n  ✅ Сохранено в {ENV}")
    print("  Реквизиты, цены и поддержку задайте потом прямо в боте: /panel")
    return 0



# ------------------------------------------------------ разделы настройки


def section_telegram(old: dict, new: dict) -> None:
    print("\n── Telegram ──")
    print("Токен берётся у @BotFather, ID — у @userinfobot.")
    new["BOT_TOKEN"] = ask("Токен бота", current=old.get("BOT_TOKEN", ""),
                           validate=check_token)
    new["ADMIN_IDS"] = ask("Ваш Telegram ID", current=old.get("ADMIN_IDS", ""),
                           validate=check_id)


def section_pay(old: dict, new: dict) -> None:
    print("\n── Реквизиты для приёма переводов ──")
    new["PAY_CARD_NUMBER"] = ask("Номер карты", current=old.get("PAY_CARD_NUMBER", ""))
    new["PAY_CARD_HOLDER"] = ask("Имя владельца", current=old.get("PAY_CARD_HOLDER", ""))
    new["PAY_CARD_BANK"] = ask("Банк", current=old.get("PAY_CARD_BANK", ""))
    new["PAY_CITY"] = ask("Город", current=old.get("PAY_CITY", "") or "Душанбе")
    print()
    print("Кнопка быстрой оплаты «Душанбе Сити» (можно пропустить):")
    print("возьмите номер счёта из своей ссылки pay.dc.tj — параметр a=")
    new["DC_ACCOUNT"] = ask("Счёт для pay.dc.tj", current=old.get("DC_ACCOUNT", ""),
                            allow_empty=True)


def section_prices(old: dict, new: dict) -> None:
    print("\n── Цены ──")
    print("Их же можно менять потом прямо в боте: /panel -> Цены.")
    current_price = old.get("STAR_PRICE_DIRAM", "")
    shown = f"{int(current_price) / 100:.2f}" if current_price.isdigit() else ""
    price = ask("Цена одной звезды в сомони (напр. 0.25)",
                current=shown, validate=check_price)
    new["STAR_PRICE_DIRAM"] = str(to_diram(price))

    current_min = old.get("MIN_DEPOSIT_DIRAM", "")
    shown_min = f"{int(current_min) / 100:.2f}" if current_min.isdigit() else "10"
    minimum = ask("Минимальное пополнение в сомони",
                  current=shown_min, validate=check_price)
    new["MIN_DEPOSIT_DIRAM"] = str(to_diram(minimum))


def section_fazer(old: dict, new: dict) -> None:
    print("\n── Выдача через FazerCards ──")
    print("Пополняете баланс реселлера один раз — дальше бот списывает сам.")
    print("Сид-фраза не нужна. Ключ начинается с fc_")
    print()
    new["FRAGMENT_MODE"] = "fazer"
    new["FAZER_API_KEY"] = ask("Ключ FazerCards (X-API-Key)",
                               current=old.get("FAZER_API_KEY", ""))
    new["FAZER_BASE_URL"] = ask(
        "Адрес API",
        current=old.get("FAZER_BASE_URL", "") or "https://api.fzr.cards",
        validate=check_base_url,
    )


def section_mystars(old: dict, new: dict) -> None:
    print("\n── Выдача через MyStars ──")
    print("Сид-фразу этот сервис не спрашивает: счёт на оплату придёт")
    print("вам в Telegram, оплатите в один тап через Tonkeeper.")
    print("Ключ выдают в @my_stars_tg_bot -> API access.")
    print()
    new["FRAGMENT_MODE"] = "mystars"
    new["MYSTARS_API_KEY"] = ask("Ключ MyStars (X-Api-Key)",
                                 current=old.get("MYSTARS_API_KEY", ""))
    new["MYSTARS_BASE_URL"] = ask(
        "Адрес API",
        current=old.get("MYSTARS_BASE_URL", "") or "https://api.mystars.tg/v1",
        validate=check_base_url,
    )
    new["MYSTARS_CURRENCY"] = ask(
        "Чем платить (ton / usdt_ton)",
        current=old.get("MYSTARS_CURRENCY", "") or "ton",
        validate=lambda v: None if v in ("ton", "usdt_ton") else "Только ton или usdt_ton",
    )


def section_apifragment(old: dict, new: dict) -> None:
    print("\n── Выдача через ApiFragment ──")
    print("  " + "!" * 54)
    print("  Этот сервис требует 24 слова вашего TON-кошелька и хранит их")
    print("  у себя. Владельцы сервиса технически могут вывести с этого")
    print("  кошелька всё. Заведите ОТДЕЛЬНЫЙ кошелёк с оборотной суммой.")
    print("  " + "!" * 54)
    print()
    new["FRAGMENT_MODE"] = "api"
    new["FRAGMENT_API_KEY"] = ask("API-токен ApiFragment",
                                  current=old.get("FRAGMENT_API_KEY", ""))
    new["FRAGMENT_WALLET_SEED"] = ask(
        "Сид-фраза кошелька (24 слова через пробел)",
        current=old.get("FRAGMENT_WALLET_SEED", ""), validate=check_seed,
    )
    new["FRAGMENT_PAYMENT_METHOD"] = ask(
        "Чем платить (ton / usdt_ton)",
        current=old.get("FRAGMENT_PAYMENT_METHOD", "") or "ton",
        validate=lambda v: None if v in ("ton", "usdt_ton") else "Только ton или usdt_ton",
    )


def section_delivery(old: dict, new: dict) -> None:
    print("\n── Как выдавать звёзды ──")
    print("mock    — бот работает, но ничего не отправляет (для проверки).")
    print("fazer   — api.fzr.cards: баланс реселлера, сид-фраза не нужна.")
    print("mystars — api.mystars.tg: платите за каждый заказ из кошелька.")
    print("api     — apifragment.online: хранит вашу сид-фразу у себя.")
    mode = ask("Режим (mock/fazer/mystars/api)",
               current=old.get("FRAGMENT_MODE", "") or "mock",
               validate=lambda v: None if v in ("mock", "fazer", "mystars", "api")
               else "Только mock, fazer, mystars или api")
    new["FRAGMENT_MODE"] = mode
    if mode == "fazer":
        section_fazer(old, new)
    elif mode == "mystars":
        section_mystars(old, new)
    elif mode == "api":
        section_apifragment(old, new)


def section_links(old: dict, new: dict) -> None:
    print("\n── Необязательное ──")
    new["SUPPORT_USERNAME"] = ask("Юзернейм поддержки без @",
                                  current=old.get("SUPPORT_USERNAME", ""), allow_empty=True)
    new["REVIEWS_URL"] = ask("Ссылка на канал с отзывами",
                             current=old.get("REVIEWS_URL", ""), allow_empty=True)


SECTIONS = {
    "telegram": ("Токен и админ", section_telegram),
    "pay": ("Реквизиты карты", section_pay),
    "prices": ("Цены", section_prices),
    "delivery": ("Способ выдачи", section_delivery),
    "fazer": ("FazerCards", section_fazer),
    "mystars": ("MyStars", section_mystars),
    "apifragment": ("ApiFragment", section_apifragment),
    "links": ("Ссылки", section_links),
}

ALL_SECTIONS = ("telegram", "pay", "prices", "delivery", "links")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        if not EXAMPLE.exists():
            print("❌ Запускайте из папки stars_bot.")
            sys.exit(1)
        rest = sys.argv[2:]
        replace = "--replace" in rest
        sys.exit(quick([a for a in rest if a != "--replace"], replace=replace))

    if len(sys.argv) > 1 and sys.argv[1] == "--set":
        if not EXAMPLE.exists():
            print("❌ Запускайте из папки stars_bot.")
            sys.exit(1)
        apply_values(sys.argv[2:])
        return

    if not EXAMPLE.exists():
        print("❌ Не найден .env.example — запускайте скрипт из папки stars_bot.")
        sys.exit(1)

    chosen = ALL_SECTIONS
    if len(sys.argv) > 2 and sys.argv[1] == "--only":
        chosen = tuple(part for part in sys.argv[2].split(",") if part in SECTIONS)
        if not chosen:
            print("Доступные разделы: " + ", ".join(SECTIONS))
            sys.exit(1)

    print("=" * 58)
    print("  Настройка бота. Enter — оставить значение в скобках.")
    print("=" * 58)

    old = read_existing()
    new = dict(old)
    for name in chosen:
        SECTIONS[name][1](old, new)

    write_env(new)

    print("\n" + "=" * 58)
    print(f"  ✅ Настройки сохранены в {ENV}")
    print("=" * 58)
    print("\nЗапуск бота:\n    python -m app.main\n")
    if new.get("FRAGMENT_MODE", "mock") == "mock":
        print("Сейчас режим mock — звёзды НЕ отправляются.")
        print("Включить выдачу: python setup.py --only delivery\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.")
