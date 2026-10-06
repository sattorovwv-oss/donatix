"""Страховка от «text is too long»: Telegram берёт не больше 4096 символов
в сообщении и 1024 в подписи к фото. Экран, который вырос (сто игр, длинный
список заявок), иначе падает целиком — владелец видит ошибку вместо панели.

Мидлварь до отправки аккуратно укорачивает текст: режет по строке, закрывает
открытые HTML-теги и ставит «…». Кнопки остаются — по ним видно всё остальное.
"""
from __future__ import annotations

import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
#: Запас: лимит Telegram считается после разбора тегов, в UTF-16 — берём с запасом
MARGIN = 196

TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*?(/?)>")
TAIL = "\n…"


def _visible_len(html: str) -> int:
    return len(TAG_RE.sub("", html).encode("utf-16-le")) // 2


def shorten(html: str, limit: int) -> str:
    """HTML-текст не длиннее limit видимых символов, с закрытыми тегами."""
    if _visible_len(html) <= limit:
        return html
    budget = limit - MARGIN
    cut = html
    # Режем по строкам с конца, пока не влезет; внутри тега и &amp; не режем
    while _visible_len(cut) > budget and "\n" in cut:
        cut = cut[:cut.rindex("\n")]
    if _visible_len(cut) > budget:  # одна огромная строка — режем по символам вне тегов
        out, seen, i = [], 0, 0
        while i < len(cut) and seen < budget:
            if cut[i] == "<" and ">" in cut[i:]:
                j = cut.index(">", i) + 1
                out.append(cut[i:j])
                i = j
                continue
            if cut[i] == "&" and ";" in cut[i:i + 10]:
                j = cut.index(";", i) + 1
                out.append(cut[i:j])
                i, seen = j, seen + 1
                continue
            out.append(cut[i])
            i, seen = i + 1, seen + 1
        cut = "".join(out)
    stack: list[str] = []
    for closing, name, selfclose in TAG_RE.findall(cut):
        name = name.lower()
        if selfclose:
            continue
        if closing:
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return cut + TAIL + "".join(f"</{name}>" for name in reversed(stack))


class LengthGuard(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        for field, limit in (("text", TEXT_LIMIT), ("caption", CAPTION_LIMIT)):
            value = getattr(method, field, None)
            if isinstance(value, str) and len(value) > limit - MARGIN:
                short = shorten(value, limit)
                if short != value:
                    object.__setattr__(method, field, short)
        return await make_request(bot, method)
