"""Последний рубеж: нажатие, которое не взял никто.

Часть кнопок привязана к состоянию диалога: «да, это мой аккаунт»
работает, только пока человек стоит на шаге подтверждения. Состояния
живут в памяти процесса, и перезапуск бота стирает их все. После
обновления человек жмёт кнопку на своём экране — а подходящего
обработчика больше нет. Aiogram молча выбрасывает такое нажатие, и у
клиента до упора крутятся часики.

Снаружи это неотличимо от сломанной кнопки, и чинить по одной бесполезно:
причин, по которым нажатие остаётся без обработчика, много — потерянное
состояние, переименованная кнопка, экран из старой версии бота.

Поэтому роутер подключается последним и ловит всё, что не взяли до него:
объясняет человеку, что шаг потерялся, и возвращает рабочее меню.
"""
from __future__ import annotations

import logging

import aiosqlite
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app import texts
from app.handlers.menu import main_markup
from app.money import fmt

log = logging.getLogger(__name__)
router = Router(name="fallback")


@router.callback_query(F.data)
async def nobody_took_it(
    call: CallbackQuery, state: FSMContext, conn: aiosqlite.Connection
) -> None:
    from app import db

    log.info("Нажатие %r не взял ни один обработчик", call.data)

    # Состояние уже не совпадает ни с чем — чистим, иначе следующий шаг
    # человека уедет в тот же тупик.
    await state.clear()
    from app.services import access

    admin = access.is_admin(call.from_user.id)
    await call.answer("Этот экран устарел — открываю панель заново. Повторите действие."
                      if admin else texts.STEP_LOST, show_alert=True)

    # Владельцу возвращаем панель, а не витрину: он нажимал в панели, и
    # клиентское меню ему сейчас бесполезно.
    if admin:
        from app.handlers.panel import show_home

        await show_home(call.message, conn)
        return

    user = await db.get_user(conn, call.from_user.id)
    await call.message.answer(
        texts.MENU.format(balance=fmt(user.balance if user else 0)),
        reply_markup=await main_markup(conn),
    )
