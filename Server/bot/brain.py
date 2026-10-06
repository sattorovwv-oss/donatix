"""Цикл tool use с Claude: понимает сообщение и вызывает инструменты.

Используем обычный client.messages.create (GA), а не beta-раннер: бот должен
работать годами без правок под изменения беты. Шаблон цикла — из документации
Anthropic (Manual Agentic Loop).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from . import db, prompts, tools
from .config import Config
from .schemas import TOOLS

log = logging.getLogger(__name__)

MAX_TOKENS = 8000
MAX_PAUSE_RESTARTS = 3


class Brain:
    def __init__(self, client: Any, conn: sqlite3.Connection, config: Config) -> None:
        self.client = client
        self.conn = conn
        self.config = config

    # ── публичный вход ─────────────────────────────────────────────────────

    async def handle(
        self,
        owner_id: int,
        user_text: str,
        *,
        source: str = "text",
        editing_transaction_id: int | None = None,
    ) -> tools.TurnResult:
        """Обработать одно сообщение пользователя целиком."""
        return await asyncio.to_thread(
            self._handle_sync, owner_id, user_text, source, editing_transaction_id
        )

    # ── синхронная часть (выполняется в отдельном потоке) ──────────────────

    def _handle_sync(
        self,
        owner_id: int,
        user_text: str,
        source: str,
        editing_transaction_id: int | None,
    ) -> tools.TurnResult:
        result = tools.TurnResult()
        now = datetime.now(self.config.tz)
        today = now.date().isoformat()

        ctx = tools.ToolContext(
            conn=self.conn,
            owner_id=owner_id,
            result=result,
            reports_dir=self.config.reports_dir,
            font_path=self.config.font_path,
            font_bold_path=self.config.font_bold_path,
            default_currency=self.config.default_currency,
            today=today,
            tz=self.config.tz,
            now=now.astimezone(__import__("datetime").timezone.utc),
        )

        editing = (
            db.get_transaction(self.conn, owner_id, editing_transaction_id)
            if editing_transaction_id
            else None
        )
        context_block = prompts.build_context_block(
            today=today,
            weekday_ru=prompts.WEEKDAYS_RU[now.weekday()],
            now_time=now.strftime("%H:%M"),
            tz_name=self.config.tz_name,
            default_currency=self.config.default_currency,
            pending_documents=db.pending_documents(self.conn, owner_id),
            editing_transaction=editing,
            source=source,
        )

        first_user_message = f"{context_block}\n\n{user_text}"
        messages: list[dict[str, Any]] = [{"role": "user", "content": first_user_message}]

        result.reply = self._run_loop(messages, ctx)
        return result

    def _run_loop(self, messages: list[dict[str, Any]], ctx: tools.ToolContext) -> str:
        pause_restarts = 0
        response = None

        for _ in range(self.config.max_tool_iterations):
            response = self.client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=prompts.SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "low"},
            )

            stop_reason = getattr(response, "stop_reason", None)

            if stop_reason == "refusal":
                log.warning("Claude отказался отвечать: %s", getattr(response, "stop_details", None))
                return "Не могу это обработать. Попробуй сказать другими словами."

            if stop_reason == "pause_turn":
                pause_restarts += 1
                if pause_restarts > MAX_PAUSE_RESTARTS:
                    break
                messages.append({"role": "assistant", "content": response.content})
                continue

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break

            messages.append({"role": "assistant", "content": response.content})

            # Все результаты параллельных вызовов — в ОДНОМ user-сообщении.
            tool_results = []
            for block in tool_uses:
                tool_results.append(self._run_tool(block, ctx))
            messages.append({"role": "user", "content": tool_results})
        else:
            log.warning("Достигнут предел итераций инструментов (%s)", self.config.max_tool_iterations)

        return self._extract_text(response) or self._fallback_reply(ctx)

    def _run_tool(self, block: Any, ctx: tools.ToolContext) -> dict[str, Any]:
        name = block.name
        raw_input = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
        ctx.result.tool_calls.append(name)

        handler = tools.HANDLERS.get(name)
        if handler is None:
            log.error("Неизвестный инструмент: %s", name)
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps({"ошибка": f"нет такого инструмента: {name}"}, ensure_ascii=False),
                "is_error": True,
            }

        try:
            payload = handler(ctx, raw_input)
            is_error = payload.get("ok") is False
        except Exception:  # инструмент упал — сообщаем модели, а не роняем бота
            log.exception("Инструмент %s упал на входе %r", name, raw_input)
            payload = {"ошибка": "инструмент не отработал, попробуй другой способ"}
            is_error = True

        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(payload, ensure_ascii=False, default=str),
            "is_error": is_error,
        }

    @staticmethod
    def _extract_text(response: Any) -> str:
        if response is None:
            return ""
        parts = [
            b.text.strip()
            for b in getattr(response, "content", [])
            if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip()
        ]
        return "\n\n".join(parts).strip()

    @staticmethod
    def _fallback_reply(ctx: tools.ToolContext) -> str:
        """Если модель не написала текст, но что-то сделала — не молчим."""
        r = ctx.result
        if r.saved_transaction_ids:
            return "Записал."
        if r.reports_to_send:
            return "Отчёт готов, отправляю."
        if r.documents_to_send:
            return "Отправляю фото."
        return "Не понял, что нужно сделать. Скажи, пожалуйста, ещё раз."


def make_client(config: Config) -> Any:
    """Отдельная функция — в тестах вместо неё подставляется заглушка."""
    import anthropic

    return anthropic.Anthropic(api_key=config.anthropic_api_key)
