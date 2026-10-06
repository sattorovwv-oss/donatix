"""Цикл tool use — на поддельном клиенте Claude, без сети."""

import json
from dataclasses import dataclass
from typing import Any

import pytest

from bot import db
from bot.brain import Brain


# ── заглушка Anthropic ─────────────────────────────────────────────────────

@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list
    stop_reason: str = "end_turn"
    stop_details: Any = None


class FakeMessages:
    def __init__(self, script: list[FakeResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            return FakeResponse([TextBlock("готово")])
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script: list[FakeResponse]) -> None:
        self.messages = FakeMessages(script)


def make_brain(conn, config, script):
    config.ensure_dirs()
    return Brain(FakeClient(script), conn, config)


# ── тесты ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_saves_transaction_and_replies(conn, config):
    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("save_transaction", {
            "direction": "out", "kind": "payment", "counterparty": "Абубакр",
            "amount": 500000, "currency": "тенге", "item": "сумки",
            "quantity": 4, "unit": "мест",
        })], stop_reason="tool_use"),
        FakeResponse([TextBlock("Записал: Абубакр, 500 000 KZT за «сумки», 4 мест.")]),
    ])
    result = await brain.handle(1, "оплатил за товар сумки четыре места 500 тыщ тенге", source="voice")

    assert result.tool_calls == ["save_transaction"]
    assert len(result.saved_transaction_ids) == 1
    assert "500 000 KZT" in result.reply
    row = db.get_transaction(conn, 1, result.saved_transaction_ids[0])
    assert row["currency"] == "KZT" and row["quantity"] == 4


@pytest.mark.asyncio
async def test_two_tools_in_one_turn(conn, config):
    """Одно сообщение может и записать, и спросить — оба вызова должны пройти."""
    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("save_transaction", {"amount": 100, "currency": "TJS"},
                                   id="a")], stop_reason="tool_use"),
        FakeResponse([ToolUseBlock("search_transactions", {}, id="b")], stop_reason="tool_use"),
        FakeResponse([TextBlock("Записал и вот история.")]),
    ])
    result = await brain.handle(1, "отправил 100 сомони, и покажи что было раньше")
    assert result.tool_calls == ["save_transaction", "search_transactions"]


@pytest.mark.asyncio
async def test_parallel_tool_results_go_in_one_user_message(conn, config):
    """Результаты параллельных вызовов должны уйти ОДНИМ сообщением — иначе
    модель перестанет вызывать инструменты параллельно."""
    brain = make_brain(conn, config, [
        FakeResponse([
            ToolUseBlock("save_transaction", {"amount": 1, "currency": "TJS"}, id="a"),
            ToolUseBlock("save_transaction", {"amount": 2, "currency": "TJS"}, id="b"),
        ], stop_reason="tool_use"),
        FakeResponse([TextBlock("Записал обе.")]),
    ])
    result = await brain.handle(1, "отправил 1 сомони и ещё 2 сомони")

    assert len(result.saved_transaction_ids) == 2
    last_messages = brain.client.messages.calls[-1]["messages"]
    tool_result_msgs = [
        m for m in last_messages
        if isinstance(m["content"], list)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tool_result_msgs) == 1
    assert len(tool_result_msgs[0]["content"]) == 2


@pytest.mark.asyncio
async def test_invoice_flow_queues_photo_for_sending(conn, config):
    doc_id = db.add_document(conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")
    db.describe_document(conn, 1, doc_id, description="накладная на женскую обувь")

    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("find_documents", {"text": "накладная женская обувь"},
                                   id="a")], stop_reason="tool_use"),
        FakeResponse([ToolUseBlock("send_documents", {"document_ids": [doc_id]},
                                   id="b")], stop_reason="tool_use"),
        FakeResponse([TextBlock("Отправляю накладную.")]),
    ])
    result = await brain.handle(1, "отправь накладную от женской обуви", source="voice")
    assert result.documents_to_send == [doc_id]


@pytest.mark.asyncio
async def test_pending_photo_is_shown_to_model(conn, config):
    """Модель должна узнать про неподписанное фото — иначе не свяжет подпись с ним."""
    doc_id = db.add_document(conn, 1, tg_file_id="f1", file_path="/tmp/a.jpg")
    brain = make_brain(conn, config, [FakeResponse([TextBlock("ок")])])
    await brain.handle(1, "это накладная от женской обуви")

    first_message = brain.client.messages.calls[0]["messages"][0]["content"]
    assert f"id={doc_id}" in first_message
    assert "Неподписанные фото" in first_message


@pytest.mark.asyncio
async def test_today_and_currency_are_in_context(conn, config):
    brain = make_brain(conn, config, [FakeResponse([TextBlock("ок")])])
    await brain.handle(1, "привет")
    first_message = brain.client.messages.calls[0]["messages"][0]["content"]
    assert "Сегодня:" in first_message
    assert "TJS" in first_message


@pytest.mark.asyncio
async def test_edit_context_names_the_transaction(conn, config):
    tx_id = db.add_transaction(conn, 1, amount=500000, currency="KZT")
    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("update_transaction",
                                   {"transaction_id": tx_id, "amount": 400000})],
                     stop_reason="tool_use"),
        FakeResponse([TextBlock("Исправил на 400 000 KZT.")]),
    ])
    result = await brain.handle(1, "там было не 500, а 400 тысяч", editing_transaction_id=tx_id)
    assert db.get_transaction(conn, 1, tx_id)["amount"] == 400000
    assert "Исправил" in result.reply


@pytest.mark.asyncio
async def test_failing_tool_is_reported_to_model_not_raised(conn, config):
    """Упавший инструмент не должен ронять бота — модель получает is_error."""
    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("update_transaction", {"transaction_id": 999, "amount": 1},
                                   id="a")], stop_reason="tool_use"),
        FakeResponse([TextBlock("Такой записи нет.")]),
    ])
    result = await brain.handle(1, "исправь запись 999")

    tool_results = brain.client.messages.calls[-1]["messages"][-1]["content"]
    assert tool_results[0]["is_error"] is True
    assert "Такой записи нет." in result.reply


@pytest.mark.asyncio
async def test_unknown_tool_name_does_not_crash(conn, config):
    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("выдуманный_инструмент", {}, id="a")], stop_reason="tool_use"),
        FakeResponse([TextBlock("Не смог.")]),
    ])
    result = await brain.handle(1, "сделай что-нибудь")
    assert result.reply == "Не смог."


@pytest.mark.asyncio
async def test_iteration_limit_stops_infinite_loop(conn, config):
    """Модель, зациклившаяся на вызовах, не должна крутиться вечно."""
    script = [
        FakeResponse([ToolUseBlock("search_transactions", {}, id=f"t{i}")], stop_reason="tool_use")
        for i in range(50)
    ]
    brain = make_brain(conn, config, script)
    result = await brain.handle(1, "зациклись")

    assert len(brain.client.messages.calls) == config.max_tool_iterations
    assert result.reply  # не молчим, даже упершись в предел


@pytest.mark.asyncio
async def test_refusal_gives_human_answer(conn, config):
    brain = make_brain(conn, config, [FakeResponse([], stop_reason="refusal")])
    result = await brain.handle(1, "что-то не то")
    assert "другими словами" in result.reply


@pytest.mark.asyncio
async def test_silent_model_still_confirms_saved_record(conn, config):
    """Если модель ничего не написала, но запись сохранена — бот не молчит."""
    brain = make_brain(conn, config, [
        FakeResponse([ToolUseBlock("save_transaction", {"amount": 5, "currency": "TJS"})],
                     stop_reason="tool_use"),
        FakeResponse([]),  # пустой ответ
    ])
    result = await brain.handle(1, "отправил 5 сомони")
    assert result.reply == "Записал."


@pytest.mark.asyncio
async def test_request_uses_configured_model_and_no_budget_tokens(conn, config):
    """budget_tokens на Opus 5 даёт 400 — его в запросе быть не должно."""
    brain = make_brain(conn, config, [FakeResponse([TextBlock("ок")])])
    await brain.handle(1, "привет")
    call = brain.client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert "budget_tokens" not in json.dumps(call, default=str)
    assert call["output_config"] == {"effort": "low"}
    assert call["system"]  # системный промпт отдельно от сообщений (для кэша)
