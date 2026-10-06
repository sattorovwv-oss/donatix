"""Мастер установки: чистка ключей, разбор ответов API, запись .env."""

import stat

import pytest

from bot import setup


# ── чистка того, что пользователь вставил ──────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("  sk-ant-abc  ", "sk-ant-abc"),
    ('"sk-ant-abc"', "sk-ant-abc"),
    ("'sk-ant-abc'", "sk-ant-abc"),
    ("sk-ant abc", "sk-antabc"),       # неразрывный пробел из мессенджера
    ("sk-ant​abc", "sk-antabc"),       # ноль ширины
    ("﻿sk-ant-abc", "sk-ant-abc"),     # BOM
])
def test_clean_secret_removes_copy_paste_junk(raw, expected):
    assert setup.clean_secret(raw) == expected


def test_cyrillic_lookalike_is_caught():
    """Кириллическая «с» выглядит как латинская, но ломает запрос.

    Без этой проверки пользователь увидел бы «нет связи» вместо «ключ испорчен».
    """
    good = "sk-ant-abc"
    bad = "sk-ant-abс"   # последняя буква — кириллическая
    assert setup.looks_like_ascii(good) is True
    assert setup.looks_like_ascii(bad) is False
    assert setup.bad_characters(bad) == "с"


def test_mask_hides_the_middle_but_keeps_it_recognizable():
    assert setup.mask("sk-ant-api03-ABCDEFGHIJKL") == "sk-ant…IJKL"
    assert setup.mask("short") == "*****"


# ── разбор ответа getUpdates ───────────────────────────────────────────────

def test_user_id_is_found_in_a_plain_message():
    updates = {"result": [{"update_id": 1, "message": {
        "from": {"id": 777, "first_name": "Алиджон", "last_name": "Махмаджонов"},
        "text": "привет"}}]}
    assert setup.extract_user_id(updates) == (777, "Алиджон Махмаджонов")


def test_user_id_falls_back_to_username_then_placeholder():
    assert setup.extract_user_id(
        {"result": [{"message": {"from": {"id": 5, "username": "alijon"}}}]}
    ) == (5, "alijon")
    assert setup.extract_user_id(
        {"result": [{"message": {"from": {"id": 5}}}]}
    ) == (5, "без имени")


def test_user_id_is_found_in_a_button_press():
    updates = {"result": [{"callback_query": {"from": {"id": 9, "first_name": "А"}}}]}
    assert setup.extract_user_id(updates) == (9, "А")


def test_no_updates_means_no_id():
    assert setup.extract_user_id({"result": []}) is None
    assert setup.extract_user_id({}) is None


# ── проверка ключей: каждая ветка без сети ─────────────────────────────────

@pytest.fixture
def fake_http(monkeypatch):
    def install(status, body=None):
        monkeypatch.setattr(setup, "http_json", lambda *a, **kw: (status, body))
    return install


def test_telegram_ok_returns_username(fake_http):
    fake_http(200, {"ok": True, "result": {"username": "moneybot"}})
    assert setup.check_telegram("t") == "moneybot"


@pytest.mark.parametrize("status,body", [
    (401, {"ok": False}),
    (404, None),
    (0, {"error": "нет сети"}),
])
def test_telegram_failures_return_none(fake_http, status, body, capsys):
    fake_http(status, body)
    assert setup.check_telegram("t") is None
    assert "✗" in capsys.readouterr().out


def test_openai_ok(fake_http):
    fake_http(200, {"data": []})
    assert setup.check_openai("k") is True


def test_openai_no_money_says_so_plainly(fake_http, capsys):
    """429 — это не «неверный ключ», а «нет денег». Пользователь должен понять разницу."""
    fake_http(429, {"error": {"type": "insufficient_quota"}})
    assert setup.check_openai("k") is False
    assert "нет денег" in capsys.readouterr().out


def test_openai_bad_key(fake_http, capsys):
    fake_http(401, {"error": {}})
    assert setup.check_openai("k") is False
    assert "не принял этот ключ" in capsys.readouterr().out


def test_anthropic_ok(fake_http):
    fake_http(200, {"data": []})
    assert setup.check_anthropic("k") is True


@pytest.mark.parametrize("status", [401, 403])
def test_anthropic_bad_key(fake_http, capsys, status):
    fake_http(status, {"error": {"type": "authentication_error"}})
    assert setup.check_anthropic("k") is False
    assert "не принял этот ключ" in capsys.readouterr().out


def test_anthropic_no_money(fake_http, capsys):
    fake_http(400, {"error": {"message": "Your credit balance is too low"}})
    assert setup.check_anthropic("k") is False
    assert "нет денег" in capsys.readouterr().out


# ── файл .env ──────────────────────────────────────────────────────────────

VALUES = {
    "TELEGRAM_BOT_TOKEN": "123:AAE",
    "ALLOWED_USER_IDS": "777",
    "OPENAI_API_KEY": "sk-o",
    "ANTHROPIC_API_KEY": "sk-ant-a",
}


def test_env_is_written_and_read_back(tmp_path):
    path = tmp_path / ".env"
    setup.write_env(VALUES, path)
    assert setup.read_env(path) | VALUES == setup.read_env(path)


def test_env_is_only_readable_by_owner(tmp_path):
    """В файле лежат ключи — соседи по серверу читать его не должны."""
    path = tmp_path / ".env"
    setup.write_env(VALUES, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_env_has_defaults_for_timezone_and_currency(tmp_path):
    path = tmp_path / ".env"
    setup.write_env(VALUES, path)
    values = setup.read_env(path)
    assert values["TZ"] == "Asia/Dushanbe"
    assert values["DEFAULT_CURRENCY"] == "TJS"


def test_env_keeps_custom_timezone(tmp_path):
    path = tmp_path / ".env"
    setup.write_env({**VALUES, "TZ": "Asia/Almaty"}, path)
    assert setup.read_env(path)["TZ"] == "Asia/Almaty"


def test_reading_a_missing_env_is_not_an_error(tmp_path):
    assert setup.read_env(tmp_path / "нет.env") == {}


def test_env_written_here_is_accepted_by_the_real_config(tmp_path, monkeypatch):
    """Мастер и конфиг должны сходиться: файл от одного читается другим."""
    from bot.config import load_config

    path = tmp_path / ".env"
    setup.write_env(VALUES, path)
    for key in (*VALUES, "TZ", "DEFAULT_CURRENCY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("bot.config.ROOT", tmp_path)
    monkeypatch.setattr("bot.config.load_dotenv", lambda p: __import__("dotenv").load_dotenv(path))

    config = load_config()
    assert config.telegram_token == "123:AAE"
    assert config.allowed_user_ids == frozenset({777})
    assert config.anthropic_api_key == "sk-ant-a"


# ── systemd ────────────────────────────────────────────────────────────────

def test_service_file_points_at_this_install(tmp_path):
    unit = setup.render_service(tmp_path / ".venv/bin/python", tmp_path, "root")
    assert f"WorkingDirectory={tmp_path}" in unit
    assert f"ExecStart={tmp_path}/.venv/bin/python -m bot.main" in unit
    assert "User=root" in unit
    assert "Restart=always" in unit


# ── автозапуск: главная причина, по которой бот потом молчит ───────────────

class Stop(Exception):
    """Заглушка вместо реального запуска бота."""


@pytest.fixture
def wizard(monkeypatch, tmp_path):
    """Мастер с подменённым systemd и запуском бота."""
    import bot.main

    state = {"installed": False, "restarted": False, "answers": [], "output": []}

    monkeypatch.setattr(setup, "SERVICE_PATH", tmp_path / "moneybot.service")
    monkeypatch.setattr(setup, "can_install_service", lambda: True)
    monkeypatch.setattr(setup, "say", lambda text="": state["output"].append(text))
    monkeypatch.setattr(setup, "hint", lambda text: state["output"].append(text))
    monkeypatch.setattr(setup, "ok", lambda text: state["output"].append(text))

    def fake_install(python):
        state["installed"] = True
        return True

    def fake_run(cmd, **kwargs):
        state["restarted"] = True

    def refuse_to_run_bot():
        raise Stop()

    monkeypatch.setattr(setup, "install_service", fake_install)
    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    monkeypatch.setattr(bot.main, "main", refuse_to_run_bot)
    return state


def text_of(state) -> str:
    return "\n".join(state["output"])


def test_autostart_is_offered_with_yes_as_the_default(wizard, monkeypatch):
    """Отказ здесь почти всегда — непонимание последствий, а не выбор."""
    asked = {}

    def remember(prompt, default=True):
        asked["default"] = default
        return default

    monkeypatch.setattr(setup, "ask_yes", remember)
    setup.finish(skip_setup=True)

    assert asked["default"] is True
    assert wizard["installed"] is True


def test_refusing_autostart_warns_about_the_terminal(wizard, monkeypatch):
    """Именно этот случай и привёл к «бот молчит на всё»."""
    monkeypatch.setattr(setup, "ask_yes", lambda prompt, default=True: False)

    with pytest.raises(Stop):
        setup.finish(skip_setup=True)

    output = text_of(wizard)
    assert wizard["installed"] is False
    assert "только пока открыт этот терминал" in output
    assert "замолчит на всё" in output
    assert "bash setup.sh" in output       # как передумать


def test_without_root_the_warning_is_still_shown(wizard, monkeypatch):
    """Не смогли настроить — человек всё равно должен знать, чем это грозит."""
    monkeypatch.setattr(setup, "can_install_service", lambda: False)

    with pytest.raises(Stop):
        setup.finish(skip_setup=True)

    assert "только пока открыт этот терминал" in text_of(wizard)


def test_existing_service_is_restarted_not_asked_about(wizard, monkeypatch):
    """Повторный запуск на настроенном сервере просто перезапускает бота."""
    setup.SERVICE_PATH.write_text("[Unit]\n")
    monkeypatch.setattr(setup, "ask_yes",
                        lambda *a, **kw: pytest.fail("не должен ничего спрашивать"))

    assert setup.finish(skip_setup=True) == 0
    assert wizard["restarted"] is True
    assert "перезапущен" in text_of(wizard)


def test_the_restart_path_tells_how_to_check_and_read_logs(wizard, monkeypatch):
    setup.SERVICE_PATH.write_text("[Unit]\n")
    monkeypatch.setattr(setup, "ask_yes", lambda *a, **kw: True)
    setup.finish(skip_setup=True)

    output = text_of(wizard)
    assert "systemctl status" in output
    assert "journalctl" in output
