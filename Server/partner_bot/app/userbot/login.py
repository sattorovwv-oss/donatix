"""Первый вход в Telegram: python -m app.userbot.login

Запускается один раз, руками, в терминале. Telegram пришлёт код в ваш
же Telegram — его надо ввести здесь. После этого рядом появится файл
сессии, и юзербот будет входить сам.

Файл сессии равен доступу к вашему Telegram. Он лежит в data/, которая
исключена из git, и делиться им нельзя ни с кем.
"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.userbot.log import setup

log = setup(settings.log_level)


async def main() -> None:
    from telethon import TelegramClient

    if not (settings.tg_api_id and settings.tg_api_hash):
        print("❌ Нет TG_API_ID или TG_API_HASH в .env.\n"
              "   Возьмите их на https://my.telegram.org → API development tools")
        return

    settings.session_file.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.session_file), settings.tg_api_id,
                            settings.tg_api_hash)
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Вошли как @{me.username or me.id} ({me.first_name})")
    print(f"   Файл сессии: {settings.session_file}")
    print("   Больше код вводить не потребуется.\n")

    if settings.bank_bot:
        try:
            entity = await client.get_entity(settings.bank_bot)
            print(f"✅ Банковский бот найден: id={entity.id} "
                  f"@{getattr(entity, 'username', '—')}")
            print("   Впишите этот id в BANK_BOT — он надёжнее юзернейма.")
        except Exception as exc:  # noqa: BLE001 — это подсказка, не работа
            print(f"⚠️ Не нашёл {settings.bank_bot}: {exc}")
            print("   Проверьте, что переписка с этим ботом у вас есть.")
    else:
        print("⚠️ BANK_BOT в .env пуст — юзербот не будет знать, кого слушать.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
