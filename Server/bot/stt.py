"""Распознавание речи через OpenAI Whisper.

Telegram отдаёт голосовые в OGG/Opus — Whisper принимает их напрямую,
ffmpeg не нужен.
"""

from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, client: Any, model: str = "whisper-1", language: str = "ru") -> None:
        self.client = client
        self.model = model
        self.language = language

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        buffer = io.BytesIO(audio)
        buffer.name = filename  # openai определяет формат по имени файла
        response = await self.client.audio.transcriptions.create(
            model=self.model,
            file=buffer,
            language=self.language,
            # Подсказка задаёт словарь и стиль — распознавание реже путает валюты.
            prompt="Деньги, переводы, накладные. Валюты: сомони, тенге, рубли, доллары, сум.",
        )
        text = (getattr(response, "text", "") or "").strip()
        log.info("Распознано %d символов", len(text))
        return text


def make_client(api_key: str) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)
