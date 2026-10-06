"""Все тексты бота.

Значки не пишутся напрямую, а вставляются токеном [[stars]] — подстановка
идёт при обращении к тексту, поэтому смена значка в панели видна сразу.

Доступ через texts.ИМЯ работает как обычно: модульный __getattr__ отдаёт
шаблон уже с подставленными значками.
"""
from __future__ import annotations

from app.config import settings
from app.emoji import substitute
from app.money import fmt

LINE = "━━━━━━━━━━━━━━━━━━━━"


def support_username() -> str:
    """Контакт поддержки: свой у каждого бота (/panel → 📞 Контакт), иначе из настроек запуска."""
    from app import runtime
    return (runtime.get("support_username") or settings.support_username or "").strip().lstrip("@")


def support() -> str:
    name = support_username()
    return f"@{name}" if name else "поддержку"


_RAW: dict[str, str] = {}

# ═══════════════════════════════════════════════════════ менюи асосӣ

_RAW["MENU"] = (
    "<b>Хуш омадед!</b>\n"
    "<blockquote>Дар ин ҷо шумо метавонед Telegram Stars ва Telegram "
    "Premium ба ҳар ҳисоб харед — зуд ва бе ворид шудан ба он.</blockquote>\n\n"
    "[[money]] Баланси шумо: <b>{balance}</b>\n\n"
    "<i>Бахшро интихоб кунед</i> [[point]]"
)

# ═════════════════════════════════════════════════════════════ ситораҳо

_RAW["STARS_ENTRY"] = (
    "[[stars]] <b>Telegram Stars</b>\n"
    f"<code>{LINE}</code>\n\n"
    "[[price]] Нарх: <b>{rate}</b> барои як ситора\n\n"
    "<blockquote>Ситораҳо ба ҳар ҳисобе мераванд, ки юзернейми кушод "
    "дорад. Парол ва рамзи SMS ҳеҷ гоҳ лозим нест.</blockquote>\n\n"
    "<i>Маҷмӯаро интихоб кунед ё шумораи худро нависед</i> [[point]]"
)

_RAW["STARS_ASK_QUANTITY"] = (
    "[[stars]] <b>Чанд ситора?</b>\n"
    f"<code>{LINE}</code>\n\n"
    "[[price]] Нарх: <b>{rate}</b> барои як дона\n"
    "[[money]] Баланс: <b>{balance}</b> — ба <b>{affordable}</b> ⭐ мерасад\n\n"
    "<blockquote>Камтарин — <b>{min_stars}</b>, бештарин — <b>{max_stars}</b> "
    "барои як фармоиш.</blockquote>\n\n"
    "[[search]] <i>Шумораро бо рақам нависед:</i>"
)

_RAW["STARS_BAD_QUANTITY"] = (
    "[[fail]] <b>Рақами бутун</b> аз <code>{min_stars}</code> "
    "то <code>{max_stars}</code> нависед."
)

_RAW["STARS_NOT_ENOUGH"] = (
    "[[fail]] <b>Маблағ намерасад</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Лозим аст: <b>{need}</b>\n"
    "├ Мавҷуд аст: <b>{balance}</b>\n"
    "└ Намерасад: <b>{missing}</b>\n\n"
    "<blockquote>Балансро пур кунед — фармоиш дарҳол мегузарад.</blockquote>"
)

# ═══════════════════════════════════════════════════════════════ бозиҳо

_RAW["GAMES_ENTRY"] = (
    "[[game]][[pubg]] <b>Пур кардани бозиҳо</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Пуркунӣ рост ба ҳисоби бозӣ меояд. Танҳо <b>ID-и "
    "бозигар</b> лозим аст — пароли ҳисобро ҳеҷ гоҳ намепурсем.</blockquote>\n\n"
    "<i>Бозиро интихоб кунед</i> [[point]]"
)

_RAW["GAME_REGION"] = (
    "[[game]] <b>{title}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "[[point]] <b>Минтақаи ҳисобро интихоб кунед</b>\n\n"
    "<blockquote>Минтақа — ин сервере, ки шумо дар он бозӣ мекунед. Он "
    "дар профили бозӣ дар паҳлӯи ном ва ID дида мешавад.\n\n"
    "Минтақа муҳим аст: дар сервери бегона ID-и шумо ёфт намешавад ва "
    "пуркунӣ намерасад.</blockquote>"
)

_RAW["GAME_PACKS"] = (
    "[[game]] <b>{title}</b>{region}\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Маҷмӯаро интихоб кунед — баъд ID-и бозигарро мепурсам ва "
    "номро нишон медиҳам, то боварӣ ҳосил кунед, ки ин ҳисоби "
    "шумост.</blockquote>"
)

_RAW["GAME_ASK_ID"] = (
    "[[game]] <b>{title}</b>{region} — {pack}\n"
    f"<code>{LINE}</code>\n\n"
    "[[profile]] <b>ID-и бозигар</b>\n\n"
    "<blockquote><b>ID-и рақамии</b> ҳисоби худро фиристед. Он дар профили "
    "бозӣ дар паҳлӯи ном дида мешавад.</blockquote>"
)

_RAW["GAME_ASK_TWO"] = (
    "[[game]] <b>{title}</b>{region} — {pack}\n"
    f"<code>{LINE}</code>\n\n"
    "[[profile]] <b>{first} ва {second}</b>\n\n"
    "<blockquote>Бо як паём, бо фосила фиристед:\n"
    "<code>{example}</code>\n\n"
    "Дар профили бозӣ онҳо паҳлӯи ҳам меистанд: аввал ID, баъд сервер "
    "дар қавс.</blockquote>"
)

_RAW["GAME_TWO_FORMAT"] = (
    "[[fail]] <b>Ду рақам лозим аст</b>\n\n"
    "<blockquote>{first} ва {second} — бо як паём, бо фосила:\n"
    "<code>{example}</code>\n\n"
    "Дар профили бозӣ онҳо паҳлӯи ҳам навишта шудаанд, сервер одатан дар "
    "қавс аст.</blockquote>"
)

_RAW["GAME_CHECKING"] = "[[search]] <i>ID-ро месанҷам</i> <code>{player}</code>…"

_RAW["GAME_CONFIRM"] = (
    "[[search]] <b>Ҳисобро санҷед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Бозигар: <b>{name}</b>\n"
    "├ ID: <code>{player}</code>\n"
    "├ Маҷмӯа: <b>{pack}</b>\n"
    "├ Барои пардохт: <b>{price}</b>\n"
    "└ Мемонад: <b>{rest}</b>\n\n"
    "<blockquote>[[warn]] Пуркунӣ ба ҳамин ID меравад ва баргардонидани он "
    "мумкин нест. Боварӣ ҳосил кунед, ки ҳисоб аз они шумост.</blockquote>"
)

_RAW["GAME_NO_NAME"] = (
    "[[search]] <b>ID қабул шуд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ ID: <code>{player}</code>\n"
    "├ Маҷмӯа: <b>{pack}</b>\n"
    "├ Барои пардохт: <b>{price}</b>\n"
    "└ Мемонад: <b>{rest}</b>\n\n"
    "<blockquote>[[warn]] Номро нишон дода натавонистам — ID-ро худатон "
    "санҷед, ҳар рақамашро алоҳида. Пуркунӣ ба ҳамин ID меравад ва "
    "баргардонидани он мумкин нест.</blockquote>"
)

_RAW["GAME_ID_FORMAT"] = (
    "[[fail]] <b>Ин ба ID монанд нест</b>\n\n"
    "<blockquote>ID-и бозигар — танҳо рақам, аз 5 то 20 аломат. Он дар "
    "профили бозӣ дар паҳлӯи ном дида мешавад.\n\nБоз як бор "
    "фиристед.</blockquote>"
)

_RAW["GAME_WRONG_REGION"] = (
    "[[search]] <b>Ҳисоби шуморо дар минтақаи дигар ёфтам</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ ID: <code>{player}</code>\n"
    "├ Бозигар: <b>{name}</b>\n"
    "└ Минтақа: <b>{region}</b>\n\n"
    "<blockquote>Дар минтақаи интихобшуда чунин ID нест, вале дар ин ҷо "
    "ҳаст. Минтақаи даркориро пахш кунед ва маҷмӯаро аз нав интихоб "
    "кунед.</blockquote>"
)

_RAW["GAME_UNVERIFIED"] = (
    "[[warn]] <b>ID тасдиқ нашуд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ ID: <code>{player}</code>\n"
    "├ Маҷмӯа: <b>{pack}</b>\n"
    "├ Барои пардохт: <b>{price}</b>\n"
    "└ Мемонад: <b>{rest}</b>\n\n"
    "<blockquote>Санҷиш ин ID-ро наёфт. Ин ҳамеша хато нест: барои як "
    "қисми серверҳо санҷиш умуман кор намекунад.\n\n"
    "Ба ID боварӣ доред — харед. Боварӣ надоред — минтақаро иваз кунед ё "
    "ID-ро боз як бор фиристед.\n\n"
    "[[warn]] Пуркунӣ ба ҳамин ID меравад ва баргардонидани он мумкин "
    "нест.</blockquote>"
)

_RAW["GAME_ACCEPTED"] = (
    "[[ok]] <b>Фармоиши №{order_id} қабул шуд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Маҷмӯа: <b>{pack}</b>\n"
    "├ ID: <code>{player}</code>\n"
    "└ Гирифта шуд: <b>{price}</b>\n\n"
    "<blockquote>{wait}</blockquote>"
)

# Сколько ждать выдачу — клиенту в сообщении «заказ принят»
_RAW["GAME_WAIT_FAST"] = (
    "[[wait]] Пуркунӣ аллакай дар роҳ аст — одатан камтар аз "
    "як дақиқа. Ҳамин ки расид, менависам."
)
_RAW["GAME_WAIT_STANDOFF"] = (
    "[[wait]] Standoff 2 то <b>90 дақиқа</b> мерасад (одатан тезтар), аз соати "
    "<b>11:00 то 23:00</b> бо вақти Душанбе. Фармоиши шабона субҳ иҷро мешавад. "
    "Ҳамин ки расид, менависам."
)
_RAW["GAME_WAIT_COC"] = (
    "[[wait]] Clash of Clans тавассути воридшавӣ ба аккаунт: одатан "
    "<b>20–90 дақиқа</b>, аз соати <b>11:00 то 23:00</b> бо вақти Душанбе. "
    "Фармоиши шабона субҳ иҷро мешавад. Ҳамин ки расид, менависам."
)

_RAW["GAME_DELIVERED"] = (
    "[[party]] <b>Фармоиши №{order_id} иҷро шуд!</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Маҷмӯа: <b>{pack}</b>\n"
    "├ ID: <code>{player}</code>\n"
    "└ Гирифта шуд: <b>{price}</b>\n\n"
    "<blockquote>Бозиро санҷед. Агар пуркунӣ набошад — ба дастгирӣ "
    "нависед.</blockquote>"
)

# ══════════════════════════════════════════════════════════════ steam

_RAW["STEAM_ENTRY"] = (
    "[[steam]] <b>Пур кардани Steam</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Пул ба ҳамёни Steam меояд. Танҳо <b>логин</b> лозим аст — "
    "парол ва рамзи Steam Guard-ро ҳеҷ гоҳ намепурсем.</blockquote>\n\n"
    "<i>Маблағро интихоб кунед</i> [[point]]"
)

_RAW["STEAM_ASK_LOGIN"] = (
    "[[steam]] <b>{amount} {currency}</b> — <b>{price}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "[[profile]] <b>Логини Steam</b>\n\n"
    "<blockquote><b>Логини ҳисоб</b>-ро фиристед — ҳамонеро, ки бо он ба "
    "Steam ворид мешавед. Ин ном дар профил ё почта нест.</blockquote>"
)

_RAW["STEAM_CHECKING"] = "[[search]] <i>Ҳисобро месанҷам</i> <code>{login}</code>…"

_RAW["STEAM_CONFIRM"] = (
    "[[search]] <b>Ҳисобро санҷед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Ҳисоб: <b>{name}</b>\n"
    "├ Логин: <code>{login}</code>\n"
    "├ Маблағ: <b>{amount} {currency}</b>\n"
    "├ Барои пардохт: <b>{price}</b>\n"
    "└ Мемонад: <b>{rest}</b>\n\n"
    "<blockquote>[[warn]] Пул ба ҳамин ҳисоб меравад ва баргардонидани он "
    "мумкин нест. Боварӣ ҳосил кунед, ки логин дуруст аст.</blockquote>"
)

_RAW["STEAM_BAD_LOGIN"] = (
    "[[fail]] <b>Чунин ҳисоб нест</b>\n\n"
    "<blockquote>Steam логини <code>{login}</code>-ро намешиносад. Навишти "
    "онро санҷед ва боз як бор фиристед.\n\nМаҳз логини вуруд лозим аст, "
    "на номи профил.</blockquote>"
)

_RAW["STEAM_NO_CHECK"] = (
    "[[warn]] <b>Ҳисобро санҷида натавонистам</b>\n\n"
    "<blockquote>Steam ҳозир ҷавоб намедиҳад. Пас аз якчанд дақиқа "
    "кӯшиш кунед — интизор шудан беҳтар аз он аст, ки пул ба ҷои нодуруст "
    "равад.</blockquote>"
)

_RAW["STEAM_DELIVERED"] = (
    "[[party]] <b>Фармоиши №{order_id} иҷро шуд!</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Steam: <code>{login}</code>\n"
    "├ Гузаронида шуд: <b>{amount} {currency}</b>\n"
    "└ Гирифта шуд: <b>{price}</b>\n\n"
    "<blockquote>Ҳамёни Steam-ро санҷед. Агар пул набошад — ба дастгирӣ "
    "нависед.</blockquote>"
)

# ════════════════════════════════════════════════════════════ premium

_RAW["PREMIUM_ENTRY"] = (
    "[[premium]] <b>Telegram Premium</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Обуна ба ҳисобе кушода мешавад, ки юзернейми кушод дорад. "
    "Дастрасӣ ба худи ҳисоб лозим нест.</blockquote>\n\n"
    "<i>Мӯҳлатро интихоб кунед</i> [[point]]"
)

# ══════════════════════════════════════════════════════════ гиранда

_RAW["ASK_RECIPIENT"] = (
    "<b>{title}</b> — <b>{price}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "[[profile]] <b>Ба кӣ мефиристем?</b>\n\n"
    "<blockquote>Агар барои ҳисоби худатон мехаред, «Ба худам»-ро пахш "
    "кунед — юзернейм худаш гузошта мешавад ва хато кардан "
    "имконнопазир.</blockquote>\n\n"
    "<i>Ё</i> <code>@username</code> <i>-и гирандаро фиристед.</i>"
)

_RAW["NO_OWN_USERNAME"] = (
    "[[fail]] <b>Шумо юзернейм надоред</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Ситораҳо танҳо ба ҳисобҳои дорои юзернейми кушод "
    "мераванд — бе он ҳисобро ёфтан мумкин нест.</blockquote>\n\n"
    "<b>Чӣ тавр гузоштан:</b>\n"
    "├ Танзимоти Telegram\n"
    "├ Профили ман\n"
    "└ <b>Номи корбар</b> → номи озодро интихоб кунед\n\n"
    "<i>Баъд баргардед ва «Ба худам»-ро боз як бор пахш кунед.</i>"
)

_RAW["CHECKING_RECIPIENT"] = "[[search]] <i>Ҳисобро месанҷам</i> <code>@{username}</code>…"

_RAW["BAD_USERNAME"] = (
    "[[fail]] <b>Ин ба юзернейм монанд нест</b>\n\n"
    "<blockquote>Шакли <code>@username</code> лозим аст: аз 5 то 32 аломат, "
    "ҳарфҳои лотинӣ, рақам ва зерхат.</blockquote>"
)

_RAW["UNKNOWN_RECIPIENT"] = (
    "[[fail]] <b>Ҳисоби @{username} ёфт нашуд</b>\n\n"
    "<blockquote>Санҷед, ки юзернейм кушод бошад ва бе хато навишта "
    "шуда бошад.</blockquote>"
)

_RAW["CONFIRM_RECIPIENT"] = (
    "[[search]] <b>Гирандаро санҷед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Ҳисоб: <b>{name}</b>\n"
    "└ Юзернейм: <code>@{username}</code>\n\n"
    "{who}\n\n"
    "<blockquote>[[warn]] <b>{title}</b> маҳз ба ҳамин ҳисоб меравад. Пас "
    "аз фиристодан баргардонидан мумкин нест.</blockquote>\n\n"
    "<i>Ҳама дуруст аст?</i>"
)

_RAW["CONFIRM_RECIPIENT_UNVERIFIED"] = (
    "[[search]] <b>Гирандаро санҷед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "└ Юзернейм: <code>@{username}</code>\n\n"
    "{who}\n\n"
    "<blockquote>[[warn]] Номи ҳисобро санҷида намешавад — хидмати "
    "интиқол онро намегӯяд. <code>t.me/{username}</code>-ро кушоед ва "
    "боварӣ ҳосил кунед, ки ин шахси даркорӣ аст.\n\n"
    "<b>{title}</b> маҳз ба ҳамин юзернейм меравад, баргардонидан мумкин "
    "нест.</blockquote>\n\n"
    "<i>Юзернейм дуруст аст?</i>"
)

_RAW["RECIPIENT_IS_YOU"] = "[[ok]] <b>Ин ҳисоби шумост.</b>"
_RAW["RECIPIENT_IS_OTHER"] = "[[warn]] Ин ҳисоби <b>бегона</b> аст — бодиққат санҷед."

_RAW["CONFIRM"] = (
    "[[receipt]] <b>Тасдиқи фармоиш</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Мол: <b>{title}</b>\n"
    "├ Гиранда: <b>{name}</b> (<code>@{recipient}</code>)\n"
    "{discount}"
    "├ Барои пардохт: <b>{price}</b>\n"
    "└ Мемонад: <b>{rest}</b>\n\n"
    "<blockquote>Бо пахши «Пардохт» шумо тасдиқ мекунед, ки ҳисоб дуруст "
    "нишон дода шудааст.</blockquote>"
)

_RAW["CONFIRM_DISCOUNT"] = (
    "├ Нарх: <s>{full}</s>\n"
    "├ Промокоди <code>{code}</code>: <b>−{percent}%</b> (−{saved})\n"
)

_RAW["ORDER_PROMO_ASK"] = (
    "[[promo]] <b>Промокоди тахфиф</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Рамзро нависед — арзиши фармоишро аз нав "
    "ҳисоб мекунам.</blockquote>"
)

_RAW["ORDER_PROMO_OK"] = (
    "[[party]] <b>Промокоди {code} истифода шуд</b>\n\n"
    "├ Тахфиф: <b>{percent}%</b>\n"
    "└ Сарфа: <b>{saved}</b>"
)

_RAW["PROCESSING"] = "[[wait]] <i>Пардохт шуд. {title}-ро мефиристам ба</i> <code>@{recipient}</code>…"

_RAW["PROCESSING_SLOW"] = (
    "[[ok]] <b>Фармоиш қабул ва пардохт шуд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Мол: <b>{title}</b>\n"
    "└ Гиранда: <code>@{recipient}</code>\n\n"
    "<blockquote>[[wait]] Интиқол якчанд дақиқа мегирад. Ҳамин ки ҳамааш "
    "расид, менависам — чатро пӯшида метавонед.</blockquote>"
)

_RAW["DELIVERED"] = (
    "[[party]] <b>Фармоиши №{order_id} иҷро шуд!</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Мол: <b>{title}</b>\n"
    "├ Гиранда: <code>@{recipient}</code>\n"
    "└ Гирифта шуд: <b>{price}</b>\n\n"
    "<blockquote>Ташаккур барои харид! Агар чизе нарасида бошад — ба "
    "дастгирӣ нависед.</blockquote>"
)

_RAW["REVIEW_ASK"] = (
    "[[reviews]] <b>Ҳама чиз чӣ тавр гузашт?</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Ба фармоиши <b>№{order_id}</b> баҳо гузоред — ин як "
    "сония вақт мегирад ва ба харидорони дигар кӯмак мекунад.</blockquote>"
)

_RAW["REVIEW_ASK_OLD"] = (
    "[[reviews]] <b>Шумо аз мо харид кардед — ҳама чиз чӣ тавр гузашт?</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Фармоиш: <b>№{order_id}</b>\n"
    "└ Мол: <b>{title}</b>\n\n"
    "<blockquote>Баҳо гузоред — ин як сония вақт мегирад ва ба харидорони "
    "дигар кӯмак мекунад, ки моро интихоб кунанд.</blockquote>"
)

_RAW["REVIEW_ASK_TEXT"] = (
    "[[reviews]] <b>Баҳои {stars}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Дар бораи харид ду калима нависед — мо шарҳҳоро дар "
    "канали худ нашр мекунем.\n\nНоми шумо дар зери шарҳ дида "
    "мешавад.</blockquote>"
)

_RAW["REVIEW_SENT"] = (
    "[[party]] <b>Ташаккур барои шарҳ!</b>\n\n"
    "<blockquote>Ба зудӣ он дар канали мо пайдо мешавад.</blockquote>"
)

_RAW["REVIEW_ALREADY"] = (
    "[[warn]] <b>Ба ин фармоиш шарҳ аллакай ҳаст.</b>\n\n"
    "<blockquote>Як фармоиш — як шарҳ.</blockquote>"
)

#: Шарҳ дар канал чӣ гуна дида мешавад.
_RAW["REVIEW_POST"] = (
    "{stars}\n"
    "<b>Шарҳи харид</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>{text}</blockquote>\n\n"
    "├ Мол: <b>{title}</b>\n"
    "└ Харидор: <b>{author}</b>"
)

_RAW["REVIEW_POST_SHORT"] = (
    "{stars}\n"
    "<b>Шарҳи харид</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Мол: <b>{title}</b>\n"
    "└ Харидор: <b>{author}</b>"
)

# Этот текст видит владелец, а не клиент, — он остаётся на русском.
_RAW["ADMIN_REVIEW"] = (
    "[[reviews]] <b>Новый отзыв на проверку</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Оценка: {stars} <b>({rating}/5)</b>\n"
    "├ Заказ: <b>№{order_id}</b> — {title}\n"
    "├ Никнейм: {username}\n"
    "├ Имя: <b>{name}</b>\n"
    "└ ID: <code>{user_id}</code> · <a href=\"tg://user?id={user_id}\">написать</a>\n\n"
    "<blockquote>{text}</blockquote>\n\n"
    "<i>Так отзыв подпишут в канале: {author}</i>"
)

_RAW["REFUNDED"] = (
    "[[refund]] <b>Фармоиши №{order_id} иҷро нашуд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>[[money]] <b>{price}</b> аллакай ба баланси шумо "
    "баргашт — пул гум нашуд.</blockquote>\n\n"
    "<i>Каме дертар боз кӯшиш кунед ё ба {support} нависед.</i>"
)

# ═══════════════════════════════════════════════════════════ пуркунии баланс

_RAW["DEPOSIT_METHODS"] = (
    "[[deposit]] <b>Пур кардани баланс</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Маблағи даркориро аз рӯи реквизитҳо гузаронед ва чекро "
    "фиристед — баланс пас аз санҷиш пур мешавад.</blockquote>\n\n"
    "<i>Тарзро интихоб кунед</i> [[point]]"
)

_RAW["DEPOSIT_ASK_AMOUNT"] = (
    "[[deposit]] <b>Душанбе Сити</b>\n"
    f"<code>{LINE}</code>\n\n"
    "Камтарин маблағ: <b>{min_amount}</b>\n\n"
    "[[search]] <i>Маблағро бо сомонӣ нависед — масалан</i> "
    "<code>150</code> <i>ё</i> <code>150.50</code>:"
)

_RAW["DEPOSIT_BAD_AMOUNT"] = (
    "[[fail]] Маблағро бо рақам нависед: <code>150</code> ё <code>150.50</code>."
)

_RAW["DEPOSIT_TOO_SMALL"] = "[[fail]] Камтарин маблағи пуркунӣ — <b>{min_amount}</b>."

_RAW["DEPOSIT_REQUISITES"] = (
    "[[deposit]] <b>{amount} гузаронед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<code>{card}</code>\n"
    "{holder}{bank}"
    "{extra}"
    "<blockquote>[[warn]] Маблағ — <b>маҳз {amount}</b>.\n"
    "Тангаҳоро нагиред: бот маҳз аз рӯи онҳо интиқоли шуморо "
    "мешиносад.\n{dc_block}</blockquote>\n\n"
    "<i>[[ok]] Пул расид — баланс худаш пур мешавад</i>"
)

#: Приписка про код платежа. Он нужен, чтобы найти перевод в выписке,
#: даже если чек не придёт.
_RAW["DEPOSIT_DC_BLOCK"] = "Рамзи пардохт: <code>{reference}</code>"

_RAW["DEPOSIT_WAITING"] = (
    "[[wait]] <b>Интиқолро интизорем</b>\n"
    f"<code>{LINE}</code>\n\n"
    "Ҳамин ки бонк дар бораи интиқоли <b>{amount}</b> хабар диҳад, баланс "
    "<b>худаш</b> пур мешавад. Одатан ин камтар аз як дақиқа аст.\n\n"
    "<blockquote>[[ok]] Чек фиристодан лозим нест: тангаҳои маблағ — ин "
    "худи нишони пардохти шумост.\n\n"
    "Агар пас аз якчанд дақиқа чизе тағйир наёбад, скриншоти чекро "
    "фиристед — дастӣ ҳал мекунем.</blockquote>"
)

# Боты без юзербота (из конструктора): платёж проверяет владелец по чеку.
# Там нельзя обещать «баланс пополнится сам» и «чек не нужен» — клиент
# поверит, чек не пришлёт, и заявка повиснет. Эти тексты подставляются
# вместо обычных сами (см. __getattr__).
_RAW["DEPOSIT_REQUISITES_MANUAL"] = (
    "[[deposit]] <b>{amount} гузаронед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<code>{card}</code>\n"
    "{holder}{bank}"
    "{extra}"
    "<blockquote>[[warn]] Маблағ — <b>маҳз {amount}</b>.\n"
    "Пас аз пардохт «Ман пардохт кардам»-ро пахш кунед.\n{dc_block}</blockquote>\n\n"
    "<i>[[ok]] Пардохтро месанҷем ва балансро пур мекунем</i>"
)

_RAW["DEPOSIT_WAITING_MANUAL"] = (
    "[[wait]] <b>Скриншоти чекро фиристед</b>\n"
    f"<code>{LINE}</code>\n\n"
    "Барои пуркунии <b>{amount}</b> акс ё скриншоти чекро ҳамин ҷо "
    "фиристед.\n\n"
    "<blockquote>[[ok]] Чекро месанҷем ва балансро пур мекунем — ҳамин ки "
    "шуд, менависам.</blockquote>"
)

_RAW["DEPOSIT_ALREADY_MANUAL"] = (
    "[[warn]] <b>Шумо аллакай дархост доред</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Маблағ: <b>{amount}</b>\n"
    "├ Тарз: {method}\n"
    "└ Кушода шуд: {when}\n\n"
    "<blockquote>Агар пардохт карда бошед — «Чек фиристодан»-ро пахш кунед "
    "ва скриншоти чекро фиристед. Агар не — дархостро бекор кунед ва "
    "навашро кушоед.</blockquote>"
)

_RAW["DEPOSIT_NEED_PHOTO"] = (
    "📸 Агар баланс худаш пур нашуда бошад — <b>акс ё скриншоти</b> чекро "
    "фиристед. Аз рӯи матн пардохтро санҷидан мумкин нест."
)

_RAW["DEPOSIT_SENT"] = (
    "[[ok]] <b>Чек гирифта шуд — дархости №{deposit_id}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "└ Маблағ: <b>{amount}</b>\n\n"
    "<blockquote>[[wait]] Дастӣ месанҷем ва балансро пур мекунем. Ҳамин "
    "ки ин шуд, менависам.</blockquote>"
)

_RAW["DEPOSIT_APPROVED"] = (
    "[[party]] <b>Баланс ба {amount} пур шуд!</b>\n\n"
    "[[money]] Баланси ҳозира: <b>{balance}</b>"
)

_RAW["DEPOSIT_REJECTED"] = (
    "[[fail]] <b>Пуркунии №{deposit_id} рад шуд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Пардохт ба маблағи {amount} ёфт нашуд. Агар ин хато "
    "бошад — ба {support} нависед ва чекро замима кунед.</blockquote>"
)

#: Как способ называется в заявках и отчётах. Строка попадает в базу,
#: поэтому живёт отдельно от оформления и остаётся на русском: поменять
#: текст экрана можно, а название способа в старых заявках — нельзя.
RU_METHOD = "Из России (Сбербанк/Тинькофф)"

#: Как способ показать клиенту. В базе он записан по-русски и таким
#: обязан остаться — иначе старые заявки и отчёты разъедутся на два
#: разных способа. Переводим только надпись на экране.
METHOD_TITLES = {
    "Перевод на карту": "Душанбе Сити",
    RU_METHOD: "Аз Русия (Сбербанк/Тинькофф)",
}


def method_title(stored: str) -> str:
    """Название способа для клиента. Незнакомое отдаём как есть."""
    return METHOD_TITLES.get((stored or "").strip(), stored or "интиқол")

_RAW["DEPOSIT_RU_HOW"] = (
    "🇷🇺 <b>Пардохт аз Русия</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<b>1.</b> Сбербанк, Тинькофф ё бонки дигари худро кушоед\n"
    "<b>2.</b> <b>«Переводы за рубеж»</b>-ро интихоб кунед "
    "(интиқол ба хориҷа)\n"
    "<b>3.</b> Гирандаи <b>«Душанбе Сити»</b>-ро ёбед, кишвар — "
    "Тоҷикистон\n"
    "<b>4.</b> Рақами гирандаро нависед:\n\n"
    "<code>{number}</code>\n\n"
    "<b>5.</b> Маблағро <b>в рублях</b> нишон диҳед — ҳар маблағ\n"
    "<b>6.</b> Гузаронед ва <b>чекро нигоҳ доред</b>\n\n"
    "<blockquote>[[warn]] Бонк худаш рублро ба сомонӣ мегардонад. Дар чек "
    "сатри <b>«Зачислено получателю»</b> пайдо мешавад ва дар паҳлӯи он — "
    "маблағ бо TJS ҳамроҳи тангаҳо.\n\n"
    "Маҳз ҳамонро дар қадами навбатӣ ба бот мегӯед. Ин тангаҳо нишони "
    "шумост: чунин маблағ ҳозир дар ҳеҷ кас нест ва бот аз рӯи он "
    "интиқоли шуморо худаш меёбад.</blockquote>\n\n"
    "<i>Гузарондед — «Ман фиристодам»-ро пахш кунед</i>"
)

_RAW["DEPOSIT_RU_ASK"] = (
    "🇷🇺 <b>Маблағ аз чек</b>\n"
    f"<code>{LINE}</code>\n\n"
    "Чекро кушоед ва сатри <b>«Зачислено получателю»</b>-ро ёбед.\n"
    "Дар он ҷо маблағ <b>бо сомонӣ (TJS)</b> навишта шудааст — на бо рубл.\n\n"
    "<blockquote>[[warn]] Онро <b>дақиқ, ҳамроҳи тангаҳо</b> нависед: "
    "<code>10.08</code>, на <code>10</code>.\n\n"
    "Тангаҳо ягона чизест, ки интиқоли шуморо аз дигарон ҷудо мекунад. "
    "Бе онҳо бот онро ёфта наметавонад ва пуркунӣ соҳибро интизор "
    "мешавад.</blockquote>\n\n"
    "[[search]] <i>Маблағро аз чек нависед:</i>"
)

_RAW["DEPOSIT_RU_ROUND"] = (
    "[[warn]] <b>Маблағ бе тангаҳо</b>\n\n"
    "Дар чек қариб ҳамеша тангаҳо ҳастанд — боз як бор нигаред. Агар дар "
    "ҳақиқат маблағ ҳамвор бошад, онро дубора фиристед ва ман қабул "
    "мекунам: танҳо чунин интиқолро дарозтар ҷустуҷӯ мекунем.\n\n"
    "<i>Бори дуюм ҳамон рақам — ҳамон тавр қабул мекунам.</i>"
)

_RAW["DEPOSIT_RU_WAIT"] = (
    "🕔 <b>Интиқоли шуморо интизорем</b>\n"
    f"<code>{LINE}</code>\n\n"
    "Маблағ навишта шуд: <b>{amount}</b>\n\n"
    "<blockquote>Пул аз Русия аз якчанд дақиқа то як шабонарӯз меояд — ин "
    "ба бонк вобаста аст, на ба мо. Ҳамин ки расид, баланс худаш пур "
    "мешавад ва ман менависам.</blockquote>\n\n"
    "<i>Чекро фиристед — бо он, агар чизе нодуруст равад, зудтар ҳал "
    "мекунем.</i>"
)

_RAW["DEPOSIT_ALREADY"] = (
    "[[warn]] <b>Шумо аллакай дархост доред</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Маблағ: <b>{amount}</b>\n"
    "├ Тарз: {method}\n"
    "└ Кушода шуд: {when}\n\n"
    "<blockquote>Дархости дуюм ҳозир танҳо халал мерасонад. Бот интиқоли "
    "шуморо аз рӯи маблағ бо тангаҳо мешиносад — агар ду дархост бошад, "
    "ҳар ду мувофиқ меоянд ва пуркуниро аз соҳиб интизор шудан лозим "
    "меояд.\n\n"
    "Ҳамин дархостро пардохт кунед ё онро бекор кунед — ва нави онро "
    "кушоед.</blockquote>"
)

_RAW["DEPOSIT_RECEIPT_TWICE"] = (
    "[[ok]] <b>Чек аллакай гирифта шуд</b>\n\n"
    "Аз рӯи дархост ба <b>{amount}</b> он дар мо ҳаст — боз фиристодан "
    "лозим нест.\n\n"
    "<blockquote>Ҳамин ки пул расид, баланс худаш пур мешавад ва ман "
    "менависам. Чекҳои такрорӣ пуркуниро тезтар намекунанд.</blockquote>"
)

_RAW["DEPOSIT_RECEIPT_OLD"] = (
    "[[fail]] <b>Ин чекро аллакай фиристода будед</b>\n\n"
    "Он ба дархости гузашта тааллуқ дорад. Барои як интиқол баланс як "
    "бор пур мешавад.\n\n"
    "<blockquote>Агар шумо интиқоли нав карда бошед — чеки маҳз ҳамонро "
    "фиристед.</blockquote>"
)

_RAW["DEPOSIT_CANCELLED"] = (
    "[[ok]] <b>Дархост бекор шуд</b>\n\n"
    "Акнун дархости нав кушодан мумкин аст.\n\n"
    "<blockquote>Агар шумо пулро бо вуҷуди ин фиристода бошед — ба "
    "дастгирӣ {support} нависед, соҳиб онро дастӣ мегузаронад.</blockquote>"
)

_RAW["DEPOSIT_SOON"] = (
    "🔧 Ин тарз ҳоло пайваст нашудааст.\n\nҲозир интиқол ба корт дастрас аст."
)

# ══════════════════════════════════════════════════════════════ профил

_RAW["PROFILE"] = (
    "[[profile]] <b>Профил</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ ID: <code>{user_id}</code>\n"
    "└ Username: {username}\n\n"
    "[[money]] <b>Молия</b>\n"
    "├ Баланс: <b>{balance}</b>\n"
    "└ Ҳамагӣ пур карда шуд: <b>{total_deposit}</b>\n\n"
    "📦 <b>Фармоишҳо</b>\n"
    "├ Ҳамагӣ: <b>{total}</b>\n"
    "├ Иҷро шуд: <b>{done}</b>\n"
    "├ Дар коркард: <b>{active}</b>\n"
    "├ Premium: <b>{premium}</b> моҳ <i>(~{premium_spent})</i>\n"
    "└ Ситора харида шуд: <b>{stars}</b> <i>(~{stars_spent})</i>\n\n"
    "📅 <i>Бо мо аз {created}</i>"
)

_RAW["HISTORY_EMPTY"] = (
    "[[history]] <b>Таърихи харидҳо</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Дар ин ҷо фармоишҳои шумо пайдо мешаванд: чӣ харидед, "
    "ба кӣ ва бо чӣ анҷом ёфт.</blockquote>\n\n"
    "<i>Ҳоло холӣ.</i>"
)

_RAW["HISTORY"] = (
    "[[history]] <b>Таърихи харидҳо</b>\n"
    f"<code>{LINE}</code>\n\n"
    "{summary}\n\n"
    "{items}"
)

_RAW["HISTORY_SUMMARY"] = (
    "<blockquote>[[ok]] Иҷро шуд: <b>{done}</b>   "
    "[[refund]] Баргардонида шуд: <b>{refunded}</b>\n"
    "[[money]] Ҳамагӣ харҷ шуд: <b>{spent}</b></blockquote>"
)

# ═══════════════════════════════════════════════════════════ промокодҳо

_RAW["PROMO_ASK"] = (
    "[[promo]] <b>Промокод</b>\n\n"
    "<blockquote>Рамзро нависед — маблағ дарҳол ба баланс "
    "гузаронида мешавад.</blockquote>"
)

_RAW["PROMO_OK"] = (
    "[[party]] <b>Промокод фаъол шуд!</b>\n\n"
    "├ Ҳисоб карда шуд: <b>{amount}</b>\n"
    "└ Баланс: <b>{balance}</b>"
)

# ════════════════════════════════════════════════════════════ муаррифӣ

_RAW["REFERRAL"] = (
    "[[referral]] <b>Низоми муаррифӣ</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>Дӯстонро даъват кунед ва аз ҳар пуркунии онҳо "
    "<b>{percent}%</b> гиред — ҳамеша.</blockquote>\n\n"
    "├ Даъват шуд: <b>{ref_count}</b> нафар\n"
    "└ Кор карда шуд: <b>{ref_earned}</b>\n\n"
    "🔗 <b>Пайванди шумо</b>\n<code>{link}</code>\n\n"
    "<i>Барои нусхабардорӣ пайвандро пахш кунед.</i>"
)

_RAW["REFERRAL_BONUS"] = (
    "[[referral]] <b>+{amount}</b> барои пуркунии муаррифшудаи шумо!\n\n"
    "[[money]] Баланс: <b>{balance}</b>"
)

# ═══════════════════════════════════════════════════════════ дастгирӣ

_RAW["SUPPORT"] = (
    "[[support]] <b>Дастгирӣ</b>\n"
    f"<code>{LINE}</code>\n\n"
    "📊 Муроҷиатҳои фаъол: <b>{open_tickets}</b>\n\n"
    "<blockquote>{notice}</blockquote>\n\n"
    "<i>Мушкилотро нависед — дар ҳамин чат ҷавоб медиҳам.</i>"
)

_RAW["SUPPORT_NOTICE_DEFAULT"] = (
    "Вақти миёнаи ҷавоб — то 30 дақиқа. Пеш аз муроҷиат ба бахши "
    "«Маълумот» назар кунед: дар он ҷо ҷавоби саволҳои роиҷ ҳаст."
)

_RAW["TICKET_ASK_SUBJECT"] = (
    "📝 <b>Муроҷиати нав</b>\n\n"
    "<blockquote>Мушкилотро бо як паём нависед. Агар савол оид ба фармоиш "
    "бошад — рақами онро нишон диҳед.</blockquote>"
)

_RAW["TICKET_CREATED"] = (
    "[[ok]] <b>Муроҷиати №{ticket_id} кушода шуд</b>\n\n"
    "<blockquote>Ҷавоб ба ҳамин чат меояд. Барои илова кардан — бахши "
    "«Дастгирӣ»-ро кушоед.</blockquote>"
)

_RAW["TICKET_ASK_REPLY"] = "✍️ <i>Ба муроҷиати №{ticket_id} паём нависед:</i>"
_RAW["TICKET_USER_REPLY_SENT"] = "[[ok]] Паём ба муроҷиати №{ticket_id} фиристода шуд."
_RAW["TICKET_ADMIN_ANSWER"] = (
    "[[support]] <b>Ҷавоби дастгирӣ</b> <i>(муроҷиати №{ticket_id})</i>\n"
    f"<code>{LINE}</code>\n\n"
    "<blockquote>{text}</blockquote>"
)
_RAW["TICKET_CLOSED_USER"] = (
    "[[ok]] Муроҷиати №{ticket_id} пӯшида шуд.\n\n"
    "<i>Агар савол боқӣ монда бошад — нави онро кушоед.</i>"
)
_RAW["TICKET_LIMIT"] = "Шумо аллакай муроҷиати кушода доред. Ҷавоби онро интизор шавед."

# ═════════════════════════════════════════════════════════ ҳисобкунак

_RAW["CALC_ASK"] = (
    "[[calc]] <b>Ҳисобкунак</b>\n"
    f"<code>{LINE}</code>\n\n"
    "[[price]] Қурб: <b>{rate}</b> барои як ситора\n\n"
    "<blockquote><b>Рақам</b> фиристед — арзишро ҳисоб мекунам.\n"
    "<b>Маблағро бо ҳарфи с</b> фиристед — ҳисоб мекунам, ки чанд ситора "
    "мебарояд.</blockquote>\n\n"
    "<i>Масалан:</i> <code>500</code> <i>ё</i> <code>100с</code>"
)

_RAW["CALC_STARS"] = "[[stars]] <b>{stars}</b> ситора = <b>{price}</b>"
_RAW["CALC_MONEY"] = "[[money]] Бо <b>{money}</b> тақрибан <b>{stars}</b> ситора харидан мумкин"
_RAW["CALC_BAD"] = (
    "[[fail]] Нафаҳмидам. Шумораи ситора ё маблағро фиристед: <code>100с</code>"
)

# ══════════════════════════════════════════════════════════ маълумот

_RAW["INFO"] = (
    "[[info]] <b>Ин чӣ тавр кор мекунад</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<b>1️⃣</b> Балансро бо интиқол ба корт пур мекунед\n"
    "<b>2️⃣</b> Шумораи ситораҳоро интихоб мекунед\n"
    "<b>3️⃣</b> <code>@username</code>-и гирандаро нишон медиҳед\n"
    "<b>4️⃣</b> Ситораҳо мерасанд\n\n"
    "<blockquote expandable><b>Саволҳои роиҷ</b>\n\n"
    "<b>Оё дастрасӣ ба ҳисоб лозим аст?</b>\n"
    "Не. Парол, рамзи SMS ва ворид шудан ба ҳисоб <b>ҳеҷ гоҳ</b> лозим "
    "нест. Агар касе онҳоро пурсад — ин қаллоб аст.\n\n"
    "<b>Ба ҳисоби бегона мумкин аст?</b>\n"
    "Ҳа, юзернейми кушод кифоя аст.\n\n"
    "<b>Агар фармоиш нагузарад чӣ мешавад?</b>\n"
    "Пул худкорона ба баланс бармегардад.\n\n"
    "<b>Пуркуниро чанд вақт интизор шудан лозим аст?</b>\n"
    "Одатан якчанд дақиқа пас аз фиристодани чек.\n\n"
    "<b>Оё ситораҳоро баргардонидан мумкин аст?</b>\n"
    "Не. Пас аз фиристодан амал бебозгашт аст — гирандаро санҷед."
    "</blockquote>\n\n"
    "[[support]] Дастгирӣ: {support}"
)

_RAW["TOP_CLIENTS"] = (
    "[[top]] <b>Беҳтарин мизоҷон</b>\n"
    f"<code>{LINE}</code>\n\n"
    "{items}\n\n"
    "<blockquote>Рейтинг аз рӯи маблағи {basis} барои тамоми вақт.</blockquote>"
)
_RAW["TOP_EMPTY"] = (
    "[[top]] <b>Беҳтарин мизоҷон</b>\n\n"
    "<blockquote>Ҳоло холӣ — аввалин шавед!</blockquote>"
)

_RAW["BANNED"] = "[[block]] <b>Дастрасӣ ба бот баста шуд.</b>"
_RAW["SPONSOR_ASK"] = (
    "[[warn]] <b>Як қадам монд</b>\n"
    f"<code>{LINE}</code>\n\n"
    "Ба канали мо обуна шавед — ва баргардед, ҳама чиз кушода мешавад.\n\n"
    "<blockquote>Дар он ҷо тахфифҳо, лотереяҳо ва хабарҳо дар бораи "
    "бозиҳои нав мебароянд.</blockquote>"
)
_RAW["SPONSOR_NOT_YET"] = (
    "[[fail]] Ҳоло обунаро намебинам. Обуна шавед ва боз як бор пахш кунед."
)
_RAW["SPONSOR_OK"] = "[[party]] Ташаккур! Дастрасӣ кушода шуд."
_RAW["SOON"] = "🔧 Бахш дар таҳия аст."
_RAW["SOMETHING_BROKE"] = (
    "Хатогӣ рух дод. Соҳиб хабар дорад — каме дертар кӯшиш кунед ё ба "
    "дастгирӣ нависед."
)
_RAW["STEP_LOST"] = (
    "Ин қадам гум шуд — эҳтимол бот навсозӣ шуд. Аз менюи нав сар кунед."
)
_RAW["SCREEN_OLD"] = (
    "Ин экран кӯҳна шудааст — Telegram тугмаҳои кӯҳнаро дигар қабул "
    "намекунад. Менюи нав дар поён."
)

# ═════════════════════════════════════════════════════════════ админка

_RAW["ADMIN_NEW_DEPOSIT"] = (
    "🔔 <b>Пополнение №{deposit_id}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Сумма: <b>{amount}</b>\n"
    "├ Способ: {method}\n"
    "├ Код платежа: <code>{reference}</code>\n"
    "├ Покупатель: {buyer}\n"
    "└ ID: <code>{user_id}</code>"
)

_RAW["ADMIN_DEPOSIT_RECEIPT"] = (
    "[[ok]] <b>Чек к пополнению №{deposit_id}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ Заявлено клиентом: <b>{amount}</b>\n"
    "├ Банк подтвердил: <b>{paid}</b>\n"
    "├ Код банка: <code>{code}</code>\n"
    "├ Отправитель: <code>{sender}</code>\n"
    "├ Покупатель: {buyer}\n"
    "└ ID: <code>{user_id}</code>\n\n"
    "<blockquote>Деньги уже зачислены автоматически — делать ничего не "
    "надо. Чек лежит здесь на случай спора.</blockquote>"
)

_RAW["ADMIN_NEW_TICKET"] = (
    "[[support]] <b>Обращение №{ticket_id}</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ От: {buyer} (<code>{user_id}</code>)\n"
    "└ Баланс: <b>{balance}</b>\n\n"
    "<blockquote>{subject}</blockquote>\n\n"
    "<i>Ответить:</i> <code>/answer {ticket_id} текст</code>"
)

_RAW["ADMIN_TICKET_REPLY"] = (
    "💬 <b>Ответ в обращении №{ticket_id}</b>\n"
    "От {buyer} (<code>{user_id}</code>)\n\n"
    "<blockquote>{text}</blockquote>"
)

_RAW["ADMIN_ORDER_DONE"] = (
    "[[ok]] <b>Заказ №{order_id}</b>\n"
    "{title} → <code>@{recipient}</code>\n"
    "<b>{price}</b> · покупатель <code>{user_id}</code>\n"
    "🔗 На платформе: <code>{external}</code>"
)

_RAW["ADMIN_ORDER_FAILED"] = (
    "[[warn]] <b>Заказ №{order_id} не прошёл</b>\n"
    f"<code>{LINE}</code>\n\n"
    "├ {title} → <code>@{recipient}</code>\n"
    "└ Покупатель: <code>{user_id}</code>\n\n"
    "<blockquote expandable>{error}</blockquote>"
)

_RAW["ADMIN_ALREADY_HANDLED"] = "Эта заявка уже обработана."
_RAW["ADMIN_DEPOSIT_OK"] = "[[ok]] Пополнение №{deposit_id} на {amount} зачислено."
_RAW["ADMIN_DEPOSIT_NO"] = "[[fail]] Пополнение №{deposit_id} отклонено."

_RAW["ADMIN_HELP"] = (
    "🛠 <b>Команды администратора</b>\n"
    f"<code>{LINE}</code>\n\n"
    "<b>Заявки</b>\n"
    "├ /pending — пополнения на проверке\n"
    "├ /tickets — открытые обращения\n"
    "├ /answer &lt;id&gt; &lt;текст&gt; — ответить\n"
    "└ /close &lt;id&gt; — закрыть обращение\n\n"
    "<b>Заказы и деньги</b>\n"
    "├ /stats — статистика\n"
    "├ /orders — последние заказы\n"
    "├ /retry &lt;id&gt; — повторить выдачу\n"
    "├ /done &lt;id&gt; · /refund &lt;id&gt; — закрыть или вернуть\n"    "├ /gorder &lt;id&gt; — статус игрового заказа у поставщика\n"    "├ /nick &lt;id&gt; — проверить ник у всех источников\n"
    "├ /give &lt;id&gt; &lt;сумма&gt; — начислить\n"
    "└ /take &lt;id&gt; &lt;сумма&gt; — списать\n\n"
    "<b>Прочее</b>\n"
    "├ /promo &lt;код&gt; &lt;сумма&gt; &lt;лимит&gt;\n"
    "├ /broadcast &lt;текст&gt; — рассылка\n"
    "├ /user &lt;id&gt; — карточка\n"
    "└ /ban &lt;id&gt; · /unban &lt;id&gt;"
)


def money_stats(data: dict) -> str:
    return substitute(
        "📊 <b>Статистика</b>\n"
        f"<code>{LINE}</code>\n\n"
        f"[[referral]] Пользователей: <b>{data['users']}</b>\n"
        f"[[money]] Пополнений: <b>{fmt(data['deposits'])}</b>\n"
        f"🛒 Продано: <b>{fmt(data['revenue'])}</b> "
        f"<i>({data['orders']} заказов)</i>\n"
        f"👛 На балансах: <b>{fmt(data['held_balance'])}</b>\n\n"
        "<blockquote>"
        f"[[search]] На проверке: <b>{data['pending_deposits']}</b>\n"
        f"[[support]] Обращений: <b>{data['open_tickets']}</b>\n"
        f"[[warn]] Упавших заказов: <b>{data['failed_orders']}</b>"
        "</blockquote>"
    )


_MANUAL = ("DEPOSIT_REQUISITES", "DEPOSIT_WAITING", "DEPOSIT_ALREADY")


def __getattr__(name: str) -> str:
    """Отдать шаблон с уже подставленными значками."""
    if name in _MANUAL:
        from app.config import settings
        if not settings.userbot_ready:
            name += "_MANUAL"
    try:
        return substitute(_RAW[name])
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_RAW))


# Ошибки промокода — словарь, а не шаблон, поэтому собирается отдельно.
PROMO_ERRORS = {
    "not_found": "[[fail]] Такого промокода не существует.",
    "already_used": "[[fail]] Вы уже использовали этот промокод.",
    "exhausted": "[[fail]] Лимит активаций этого промокода исчерпан.",
    "not_for_balance": (
        "[[fail]] Это код на <b>скидку</b>, а не на пополнение.\n\n"
        "<blockquote>Введите его при покупке — на шаге подтверждения "
        "заказа есть кнопка «Промокод».</blockquote>"
    ),
    "not_for_order": (
        "[[fail]] Это код на <b>пополнение баланса</b>, а не на скидку.\n\n"
        "<blockquote>Активируйте его в профиле — там он зачислит деньги "
        "на счёт.</blockquote>"
    ),
}
PROMO_ERRORS = {key: substitute(value) for key, value in PROMO_ERRORS.items()}
