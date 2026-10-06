"""Прогон фраз через настоящий Claude по временной базе — без Telegram.

    python -m bot.selftest              # прогнать сценарий пользователя
    python -m bot.selftest "своя фраза" # разобрать одну фразу
    python -m bot.selftest --keep       # оставить базу и PDF для просмотра

Нужен только ANTHROPIC_API_KEY. Токен бота и ключ OpenAI не требуются.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from . import brain as brain_module
from . import db, reports
from .config import load_config

SCENARIO = [
    "сегодня отправил Абубакру три тысячи сомони",
    "оплатил за товар сумки четыре места 500 тыщ тенге, деньги должен отдать через 10 дней",
    "какие деньги я отправил Абубакру",
    "сколько я оплатил за сумки",
    "это накладная от женской обуви",
    "отправь мне накладную от женской обуви",
    "сделай отчёт с 1 августа 2026 по 31 августа 2026",
]


async def run_phrases(phrases: list[str], keep: bool) -> int:
    config = load_config(require_secrets=False)
    if not config.anthropic_api_key:
        print("Нужен ANTHROPIC_API_KEY в .env или в окружении.", file=sys.stderr)
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="bot-selftest-"))
    config = type(config)(**{**config.__dict__, "data_dir": workdir})
    config.ensure_dirs()
    reports.register_fonts(config.font_path, config.font_bold_path)

    conn = db.connect(config.db_path)
    # Фото-заглушка, чтобы сценарий с накладной был проверяемым.
    fake_photo = config.photos_dir / "demo.jpg"
    fake_photo.write_bytes(b"\xff\xd8\xff\xd9")
    db.add_document(conn, 1, tg_file_id="demo", file_path=str(fake_photo))

    brain = brain_module.Brain(brain_module.make_client(config), conn, config)
    failures = 0

    for phrase in phrases:
        print(f"\n\033[1m> {phrase}\033[0m")
        try:
            result = await brain.handle(1, phrase, source="voice")
        except Exception as exc:
            print(f"  ❌ ошибка: {exc}")
            failures += 1
            continue

        if result.tool_calls:
            print(f"  \033[90mинструменты: {', '.join(result.tool_calls)}\033[0m")
        print(f"  {result.reply}")
        for doc_id in result.documents_to_send:
            doc = db.get_document(conn, 1, doc_id)
            print(f"  📎 фото id={doc_id}: {doc.get('description') if doc else '?'}")
        for path in result.reports_to_send:
            print(f"  📄 PDF: {path} ({path.stat().st_size} байт)")

    print("\n\033[1mЧто в базе:\033[0m")
    for row in db.search_transactions(conn, 1):
        print(
            f"  #{row['id']} {row['happened_on']} "
            f"{row.get('counterparty') or '—'} "
            f"{reports.fmt_money(row.get('amount'), row.get('currency'))} "
            f"{('за «' + row['item'] + '»') if row.get('item') else ''} "
            f"{('срок ' + row['due_date']) if row.get('due_date') else ''}"
        )

    conn.close()
    if keep:
        print(f"\nФайлы остались здесь: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failures else 0


def main() -> int:
    import asyncio

    args = [a for a in sys.argv[1:] if a != "--keep"]
    keep = "--keep" in sys.argv
    phrases = args or SCENARIO
    return asyncio.run(run_phrases(phrases, keep))


if __name__ == "__main__":
    raise SystemExit(main())
