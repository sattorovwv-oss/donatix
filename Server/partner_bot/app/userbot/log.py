"""Логи юзербота — с приставкой [USERBOT] и вычищенными секретами.

Логи читают глазами, копируют в переписку и присылают на помощь чужому
человеку. Поэтому в них не должно попадать ничего, чем можно
воспользоваться: ни api_hash, ни строки сессии, ни полного номера карты,
ни токена бота.

Чистка сделана фильтром, а не аккуратностью в местах вызова: понадеяться
на то, что никто никогда не напишет log.info(текст_сообщения), нельзя —
однажды напишет.
"""
from __future__ import annotations

import logging
import re

TAG = "[USERBOT]"

#: Что вырезать из любой строки, прежде чем она попадёт в журнал.
SECRETS = (
    # длинные цепочки цифр — номера карт
    (re.compile(r"\b(\d{4})\d{6,}(\d{4})\b"), r"\1****\2"),
    # токен бота: 1234567890:AA...
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}"), "<токен скрыт>"),
    # api_hash и ему подобные: 32 знака hex
    (re.compile(r"\b[a-f0-9]{32}\b", re.IGNORECASE), "<хеш скрыт>"),
    # ключи с говорящей приставкой
    (re.compile(r"\b(sk_live_|pk_live_|whsec_|fc_|cab_)[A-Za-z0-9_\-]{8,}"),
     r"\1<скрыт>"),
    # строка сессии Telethon — длинная база64 с ведущей единицей
    (re.compile(r"\b1[A-Za-z0-9+/=_\-]{80,}"), "<сессия скрыта>"),
)


def scrub(text: str) -> str:
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    return text


class Scrubber(logging.Filter):
    """Чистит и само сообщение, и подставляемые в него значения.

    Тип значения не меняем. Раньше всё подряд приводилось к строке — и
    запись вида «id=%d» с числом внутри переставала складываться вовсе:
    logging роняло её с ошибкой формата, а вместе с ней и то, что в ней
    было написано. Секреты бывают только в строках, поэтому и трогаем
    только строки.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: (scrub(value) if isinstance(value, str)
                                     else value)
                               for key, value in record.args.items()}
            else:
                record.args = tuple(scrub(one) if isinstance(one, str) else one
                                    for one in record.args)
        return True


def guard_root() -> None:
    """Повесить чистку на корневой журнал — на все логи разом.

    Фильтр стоял только на юзерботе, а ключи и пропуски проходят через
    обычный бот и API. Достаточно одной строки с исключением, где в
    сообщение попал токен, — и он навсегда в логах сервера.
    """
    root = logging.getLogger()
    if not any(isinstance(f, Scrubber) for f in root.filters):
        root.addFilter(Scrubber())
    for handler in root.handlers:
        if not any(isinstance(f, Scrubber) for f in handler.filters):
            handler.addFilter(Scrubber())


def get(name: str = "userbot") -> logging.Logger:
    log = logging.getLogger(name)
    if not any(isinstance(f, Scrubber) for f in log.filters):
        log.addFilter(Scrubber())
    return log


def setup(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%d.%m %H:%M:%S",
    )
    # Telethon болтлив на уровне INFO и пишет туда служебные пакеты.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    return get()
