"""Мастер установки: спрашивает ключи, проверяет их и запускает бота.

Работает только на стандартной библиотеке — чтобы его можно было запустить
до установки зависимостей, если что-то пойдёт не так.

    python -m bot.setup
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
SERVICE_NAME = "moneybot"
SERVICE_PATH = Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"

BOLD, DIM, GREEN, RED, YELLOW, OFF = "\033[1m", "\033[90m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


# ── вывод ──────────────────────────────────────────────────────────────────

def say(text: str = "") -> None:
    print(text, flush=True)


def step(number: int, total: int, title: str) -> None:
    say(f"\n{BOLD}[{number}/{total}] {title}{OFF}")


def ok(text: str) -> None:
    say(f"  {GREEN}✓{OFF} {text}")


def bad(text: str) -> None:
    say(f"  {RED}✗{OFF} {text}")


def hint(text: str) -> None:
    say(f"  {DIM}{text}{OFF}")


def mask(secret: str) -> str:
    """Показать ключ так, чтобы его можно было узнать, но не подсмотреть."""
    if len(secret) <= 12:
        return "*" * len(secret)
    return f"{secret[:6]}…{secret[-4:]}"


# Невидимые символы, которые цепляются при копировании из мессенджера
# и потом ломают запрос без внятной ошибки.
_INVISIBLE = "\u00a0\u200b\u200c\u200d\u2060\ufeff"


def clean_secret(raw: str) -> str:
    """Убрать мусор, налипший при копировании: пробелы, кавычки, невидимки."""
    value = raw.strip().strip("\"'").strip()
    for char in _INVISIBLE:
        value = value.replace(char, "")
    return value


def looks_like_ascii(value: str) -> bool:
    """Ключи и токены всегда латиница и цифры.

    Кириллическая «с» вместо латинской «c» выглядит одинаково, но запрос с ней
    падает на кодировке заголовка — и без этой проверки пользователь увидел бы
    «нет связи» вместо «ключ испорчен».
    """
    return value.isascii()


def bad_characters(value: str) -> str:
    return " ".join(sorted({c for c in value if not c.isascii()}))


def ask(prompt: str, *, secret: bool = False) -> str:
    try:
        value = input(f"  {prompt}: ")
    except EOFError:
        say()
        raise SystemExit("Установка прервана.")
    return clean_secret(value) if secret else value.strip()


def ask_yes(prompt: str, default: bool = True) -> bool:
    suffix = "Д/н" if default else "д/Н"
    while True:
        answer = ask(f"{prompt} [{suffix}]").lower()
        if not answer:
            return default
        if answer in ("д", "да", "y", "yes"):
            return True
        if answer in ("н", "нет", "n", "no"):
            return False


# ── сеть ───────────────────────────────────────────────────────────────────

def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[int, Any]:
    """GET с JSON-ответом. Возвращает (код, тело). Ошибки не выбрасывает."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, None
    except Exception as exc:
        return 0, {"error": str(exc)}


# ── проверка ключей ────────────────────────────────────────────────────────

def check_telegram(token: str) -> str | None:
    """Вернуть @username бота или None, если токен не подошёл."""
    status, body = http_json(f"https://api.telegram.org/bot{token}/getMe")
    if status == 200 and isinstance(body, dict) and body.get("ok"):
        return body["result"].get("username")
    if status == 401:
        bad("Телеграм не принял этот токен.")
        hint("Скопируй его целиком из сообщения @BotFather — вида 123456789:AAE...")
    elif status == 0:
        bad(f"Нет связи с Телеграмом: {(body or {}).get('error')}")
    else:
        bad(f"Телеграм ответил ошибкой {status}.")
    return None


def check_openai(key: str) -> bool:
    status, body = http_json(
        "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
    )
    if status == 200:
        return True
    if status == 401:
        bad("OpenAI не принял этот ключ.")
        hint("Возьми новый на platform.openai.com/api-keys — он начинается с sk-")
    elif status == 429:
        bad("Ключ верный, но на счёте OpenAI нет денег.")
        hint("Пополни баланс на platform.openai.com/settings/organization/billing")
    elif status == 0:
        bad(f"Нет связи с OpenAI: {(body or {}).get('error')}")
    else:
        bad(f"OpenAI ответил ошибкой {status}.")
    return False


def check_anthropic(key: str) -> bool:
    status, body = http_json(
        "https://api.anthropic.com/v1/models",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    if status == 200:
        return True
    if status in (401, 403):
        bad("Anthropic не принял этот ключ.")
        hint("Возьми новый на console.anthropic.com/settings/keys — он начинается с sk-ant-")
    elif status == 400 and isinstance(body, dict) and "credit" in json.dumps(body).lower():
        bad("Ключ верный, но на счёте Anthropic нет денег.")
        hint("Пополни баланс на console.anthropic.com/settings/billing")
    elif status == 0:
        bad(f"Нет связи с Anthropic: {(body or {}).get('error')}")
    else:
        bad(f"Anthropic ответил ошибкой {status}.")
    return False


# ── определение своего Telegram id ─────────────────────────────────────────

def extract_user_id(updates: dict[str, Any]) -> tuple[int, str] | None:
    """Найти автора первого сообщения в ответе getUpdates."""
    for update in updates.get("result", []):
        for key in ("message", "edited_message", "channel_post", "callback_query"):
            payload = update.get(key)
            if isinstance(payload, dict) and isinstance(payload.get("from"), dict):
                sender = payload["from"]
                name = " ".join(
                    x for x in (sender.get("first_name"), sender.get("last_name")) if x
                ) or sender.get("username") or "без имени"
                return int(sender["id"]), name
    return None


def detect_user_id(token: str, username: str | None) -> int | None:
    """Попросить написать боту и вычислить id автора — без @userinfobot."""
    http_json(f"https://api.telegram.org/bot{token}/deleteWebhook")

    link = f"https://t.me/{username}" if username else "своего бота в Телеграме"
    say()
    say(f"  {BOLD}Открой {link} и напиши ему любое сообщение.{OFF}")
    hint("Я сам определю твой id — искать его отдельно не надо.")
    hint("Если не получается — нажми Enter, впишешь id вручную.")
    say()

    import select

    say(f"  {DIM}Жду сообщение…{OFF}")
    for _ in range(20):  # примерно 100 секунд ожидания
        # Enter прерывает ожидание и переводит на ручной ввод.
        if select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()
            return None
        status, body = http_json(
            f"https://api.telegram.org/bot{token}/getUpdates?timeout=5&offset=-1", timeout=15
        )
        if status == 200 and isinstance(body, dict):
            found = extract_user_id(body)
            if found:
                user_id, name = found
                ok(f"Это ты: {name}, id {user_id}")
                return user_id
    return None


# ── файл .env ──────────────────────────────────────────────────────────────

def render_env(values: dict[str, str]) -> str:
    lines = [
        "# Создано мастером установки: python -m bot.setup",
        "# Никому не показывай этот файл — здесь твои ключи.",
        "",
        f"TELEGRAM_BOT_TOKEN={values['TELEGRAM_BOT_TOKEN']}",
        f"ALLOWED_USER_IDS={values['ALLOWED_USER_IDS']}",
        f"OPENAI_API_KEY={values['OPENAI_API_KEY']}",
        f"ANTHROPIC_API_KEY={values['ANTHROPIC_API_KEY']}",
        "",
        f"TZ={values.get('TZ', 'Asia/Dushanbe')}",
        f"DEFAULT_CURRENCY={values.get('DEFAULT_CURRENCY', 'TJS')}",
        "",
    ]
    return "\n".join(lines)


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    path.write_text(render_env(values), encoding="utf-8")
    path.chmod(0o600)  # ключи не должны читаться другими пользователями


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


# ── systemd ────────────────────────────────────────────────────────────────

def render_service(python: Path, workdir: Path, user: str) -> str:
    return f"""[Unit]
Description=Telegram money bot
After=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
ExecStart={python} -m bot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def can_install_service() -> bool:
    import shutil

    return os.geteuid() == 0 and shutil.which("systemctl") is not None


def install_service(python: Path) -> bool:
    user = os.environ.get("SUDO_USER") or "root"
    SERVICE_PATH.write_text(render_service(python, ROOT, user), encoding="utf-8")
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME], check=True,
                       capture_output=True)
    except subprocess.CalledProcessError as exc:
        bad(f"systemd не смог запустить сервис: {exc}")
        return False
    ok(f"Сервис {SERVICE_NAME} установлен и запущен")
    hint(f"Логи:      journalctl -u {SERVICE_NAME} -f")
    hint(f"Стоп:      systemctl stop {SERVICE_NAME}")
    hint(f"Рестарт:   systemctl restart {SERVICE_NAME}")
    return True


# ── шаги ───────────────────────────────────────────────────────────────────

def collect_keys() -> dict[str, str]:
    existing = read_env()
    values: dict[str, str] = {}
    total = 4

    # 1. Токен бота
    step(1, total, "Токен телеграм-бота")
    hint("Открой @BotFather в Телеграме, отправь /newbot и скопируй токен.")
    if existing.get("TELEGRAM_BOT_TOKEN"):
        hint(f"Сейчас записан: {mask(existing['TELEGRAM_BOT_TOKEN'])}")
    while True:
        token = ask("Токен", secret=True) or existing.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            bad("Без токена бот работать не сможет.")
            continue
        if not looks_like_ascii(token):
            bad(f"В токене посторонние символы: {bad_characters(token)}")
            hint("Так бывает при копировании из чата. Скопируй токен ещё раз, целиком.")
            continue
        username = check_telegram(token)
        if username:
            ok(f"Бот найден: @{username}")
            values["TELEGRAM_BOT_TOKEN"] = token
            break

    # 2. Свой id
    step(2, total, "Твой Telegram id")
    hint("Бот будет отвечать только тебе — чужие не потратят твои ключи.")
    user_id = detect_user_id(values["TELEGRAM_BOT_TOKEN"], username)
    if user_id is None:
        hint("Узнать id можно у @userinfobot — он пришлёт его числом.")
        while True:
            raw = ask("Твой id (число)") or existing.get("ALLOWED_USER_IDS", "")
            if re.fullmatch(r"\d+(\s*,\s*\d+)*", raw or ""):
                values["ALLOWED_USER_IDS"] = re.sub(r"\s+", "", raw)
                break
            bad("Нужно число, например 123456789.")
    else:
        values["ALLOWED_USER_IDS"] = str(user_id)

    # 3. OpenAI
    step(3, total, "Ключ OpenAI — распознавание голоса")
    hint("Возьми на platform.openai.com/api-keys. Начинается с sk-")
    if existing.get("OPENAI_API_KEY"):
        hint(f"Сейчас записан: {mask(existing['OPENAI_API_KEY'])}")
    while True:
        key = ask("Ключ OpenAI", secret=True) or existing.get("OPENAI_API_KEY", "")
        if not key:
            bad("Без него бот не сможет слушать голосовые.")
            continue
        if not looks_like_ascii(key):
            bad(f"В ключе посторонние символы: {bad_characters(key)}")
            hint("Так бывает при копировании из чата. Скопируй ключ ещё раз, целиком.")
            continue
        if check_openai(key):
            ok("Ключ рабочий")
            values["OPENAI_API_KEY"] = key
            break

    # 4. Anthropic
    step(4, total, "Ключ Anthropic — понимание смысла")
    hint("Возьми на console.anthropic.com/settings/keys. Начинается с sk-ant-")
    if existing.get("ANTHROPIC_API_KEY"):
        hint(f"Сейчас записан: {mask(existing['ANTHROPIC_API_KEY'])}")
    while True:
        key = ask("Ключ Anthropic", secret=True) or existing.get("ANTHROPIC_API_KEY", "")
        if not key:
            bad("Без него бот не поймёт, что ты сказал.")
            continue
        if not looks_like_ascii(key):
            bad(f"В ключе посторонние символы: {bad_characters(key)}")
            hint("Так бывает при копировании из чата. Скопируй ключ ещё раз, целиком.")
            continue
        if check_anthropic(key):
            ok("Ключ рабочий")
            values["ANTHROPIC_API_KEY"] = key
            break

    for key in ("TZ", "DEFAULT_CURRENCY"):
        if existing.get(key):
            values[key] = existing[key]
    return values


def main() -> int:
    say(f"\n{BOLD}Настройка бота{OFF}")
    say(f"{DIM}Спрошу четыре вещи, проверю каждую и запущу бота.{OFF}")

    if ENV_PATH.is_file():
        say()
        existing = read_env()
        ok(f"Настройки уже есть: {ENV_PATH}")
        for key in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if existing.get(key):
                hint(f"{key} = {mask(existing[key])}")
        if existing.get("ALLOWED_USER_IDS"):
            hint(f"ALLOWED_USER_IDS = {existing['ALLOWED_USER_IDS']}")
        say()
        if not ask_yes("Ввести ключи заново?", default=False):
            say(f"\n{GREEN}Оставляю как есть.{OFF}")
            return finish(skip_setup=True)

    values = collect_keys()
    write_env(values)
    say()
    ok(f"Настройки записаны: {ENV_PATH} (доступ только тебе)")
    return finish()


TERMINAL_WARNING = (
    "Бот будет работать, только пока открыт этот терминал.\n"
    "  Закроешь окно или отключишься от сервера — он остановится, "
    "и в Телеграме замолчит на всё."
)


def finish(skip_setup: bool = False) -> int:
    python = Path(sys.executable)

    if SERVICE_PATH.is_file():
        say()
        ok("Автозапуск уже настроен")
        subprocess.run(["systemctl", "restart", SERVICE_NAME], check=False)
        say(f"\n{GREEN}{BOLD}Готово. Бот перезапущен с новыми настройками.{OFF}")
        hint(f"Проверить:  systemctl status {SERVICE_NAME}")
        hint(f"Логи:       journalctl -u {SERVICE_NAME} -f")
        return 0

    if can_install_service():
        say()
        say(f"{BOLD}Автозапуск{OFF}")
        hint("Бот будет сам подниматься после перезагрузки сервера и после падений.")
        hint("Без него он живёт только пока открыт терминал — это главная причина,")
        hint("по которой бот потом молча перестаёт отвечать.")
        # Умолчание — «да»: отказ здесь почти всегда означает не осознанный выбор,
        # а непонимание последствий.
        if ask_yes("Настроить автозапуск?", default=True):
            if install_service(python):
                say(f"\n{GREEN}{BOLD}Готово. Бот работает и переживёт перезагрузку.{OFF}")
                say("Напиши ему в Телеграме — например: «Отправил Абубакру три тысячи сомони».")
                return 0
        say()
        say(f"{YELLOW}⚠ Автозапуск не настроен.{OFF}")
        hint(TERMINAL_WARNING)
        hint("Передумаешь — запусти ещё раз: bash setup.sh")
    else:
        say()
        say(f"{YELLOW}⚠ Автозапуск настроить не смог{OFF} "
            f"{DIM}(нужен root и systemd){OFF}")
        hint(TERMINAL_WARNING)

    say(f"\n{GREEN}{BOLD}Запускаю бота.{OFF} {DIM}Остановить — Ctrl+C{OFF}")
    say("Напиши ему в Телеграме — например: «Отправил Абубакру три тысячи сомони».\n")
    from .main import main as run_bot

    return run_bot()


if __name__ == "__main__":
    raise SystemExit(main())
