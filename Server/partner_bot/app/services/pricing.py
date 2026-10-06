"""Автообновление цен: бот сам спрашивает себестоимость и держит наценку.

Курс TON и цены сервиса выдачи меняются в течение дня. Если цену не
обновлять, наценка расползается: сегодня 15%, завтра 3%, послезавтра
продажа в убыток. Задача раз в N минут спрашивает свежую цену и
пересчитывает продажную по заданному проценту.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal

import aiosqlite
from aiogram import Bot

from app import db, runtime
from app.money import fmt, fmt4
from app.services import rates

log = logging.getLogger(__name__)

#: Насколько цена должна скакнуть, чтобы предупредить владельца.
ALERT_PERCENT = 20


async def refresh_rate(conn: aiosqlite.Connection) -> rates.Rate:
    """Забрать свежий курс доллара и сохранить его.

    Резкий скачок не проглатываем молча: курс задаёт все цены сразу, и
    ошибка источника уехала бы в магазин без единого следа.
    """
    from datetime import datetime, timezone

    rate = await rates.fetch(runtime.get_int("usd_rate_spread"))
    old = runtime.usd_rate()
    await runtime.set_value(conn, "usd_rate_diram", str(rate.diram))
    await runtime.set_value(conn, "usd_rate_source", rate.source)
    await runtime.set_value(
        conn, "usd_rate_at",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if old > 0 and abs(rate.diram - old) * 100 / old >= ALERT_PERCENT:
        log.warning("Курс скакнул: %s -> %s (%s)", fmt(old), fmt(rate.diram), rate.source)
    return rate


async def refresh_once(
    conn: aiosqlite.Connection, provider, bot: Bot | None = None
) -> dict:
    """Обновить себестоимость и продажные цены. Возвращает отчёт."""
    if runtime.get_bool("usd_auto"):
        try:
            await refresh_rate(conn)
        except Exception as exc:  # noqa: BLE001 — без курса просто идём дальше
            log.info("Курс не обновился: %s", exc)
    estimate_fn = getattr(provider, "cost_estimate", None)
    if estimate_fn is None:
        return {"ok": False, "reason": "сервис выдачи не отдаёт цены"}

    rate = runtime.usd_rate()
    if rate <= 0:
        return {"ok": False, "reason": "не задан курс доллара"}

    margin = runtime.margin_percent()
    if margin <= 0:
        return {"ok": False, "reason": "не задана наценка"}

    report: dict = {"ok": True, "changed": [], "reason": ""}

    # ---- звёзды ----
    try:
        estimate = await estimate_fn("stars", 1000)
    except Exception as exc:  # noqa: BLE001 — фон не должен падать
        return {"ok": False, "reason": f"цена не пришла: {exc}"}

    # Звезда стоит доли дирама, поэтому считаем в десятитысячных сомони:
    # округление до дирама съедало бы разницу между 10% и 15% наценки.
    cost_e4 = _to_e4(estimate.usd_per_unit, rate)
    old_price = runtime.star_price_e4()
    new_price = _with_margin(cost_e4, margin)

    if cost_e4 != runtime.star_cost_e4():
        await runtime.set_value(conn, "star_cost_e4", str(cost_e4))
    if new_price != old_price:
        await runtime.set_value(conn, "star_price_e4", str(new_price))
        report["changed"].append(("Звезда", old_price, new_price, cost_e4))
        report["star_e4"] = True

    # ---- premium ----
    costs = dict(runtime.premium_costs())
    plans = runtime.premium_plans()
    for plan in plans:
        months = int(plan["months"])
        try:
            item = await estimate_fn("premium", months)
        except Exception as exc:  # noqa: BLE001
            log.info("Цена Premium %s мес. не пришла: %s", months, exc)
            continue
        plan_cost = _to_diram(item.usd_total, rate)
        costs[months] = plan_cost
        wanted = _with_margin(plan_cost, margin)
        if wanted != int(plan["price"]):
            report["changed"].append((f"Premium {months} мес.",
                                      int(plan["price"]), wanted, plan_cost))
            plan["price"] = wanted

    if costs:
        await runtime.save_premium_costs(conn, costs)
    if any(name.startswith("Premium") for name, *_ in report["changed"]):
        await runtime.save_premium_plans(conn, plans)

    if report["changed"] and bot is not None:
        await _maybe_alert(bot, report["changed"])
    return report


async def apply_margin_now(conn: aiosqlite.Connection, provider=None) -> list[str]:
    """Наценку поменяли — цены поднимаются сразу, а не при следующем автообновлении.

    Сначала пробуем свежие закупочные цены у сервиса выдачи; не вышло —
    пересчитываем от последних известных. Игры считаются от наценки при
    каждом показе, им достаточно сбросить память о пакетах.
    """
    from app.services import games as gsvc

    lines: list[str] = []
    fresh = False
    if provider is not None:
        try:
            report = await refresh_once(conn, provider)
            fresh = bool(report.get("ok"))
            for name, old, new, _cost in report.get("changed", []):
                show = fmt4 if name == "Звезда" else fmt
                lines.append(f"{name}: {show(old)} → {show(new)}")
        except Exception as exc:  # noqa: BLE001 — пересчитаем по сохранённым ценам
            log.info("Свежие цены не пришли: %s", exc)
    if not fresh:
        margin = runtime.margin_percent()
        old = runtime.star_price_e4()
        new = runtime.price_from_margin_e4()
        if runtime.star_cost_e4() > 0 and new != old:
            await runtime.set_value(conn, "star_price_e4", str(new))
            lines.append(f"Звезда: {fmt4(old)} → {fmt4(new)}")
        costs = dict(runtime.premium_costs())
        plans = runtime.premium_plans()
        changed = False
        for plan in plans:
            cost = costs.get(int(plan["months"]))
            if cost:
                wanted = _with_margin(cost, margin)
                if wanted != int(plan["price"]):
                    lines.append(f"Premium {plan['months']} мес.: {fmt(int(plan['price']))} → {fmt(wanted)}")
                    plan["price"] = wanted
                    changed = True
        if changed:
            await runtime.save_premium_plans(conn, plans)
    gsvc._offers_raw.clear()
    return lines


def _to_diram(usd: Decimal, rate_diram: int) -> int:
    return int((usd * rate_diram).to_integral_value(rounding=ROUND_HALF_UP))


def _to_e4(usd: Decimal, rate_diram: int) -> int:
    """Доллары -> десятитысячные сомони. rate_diram — курс в дирамах."""
    return int((usd * rate_diram * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _with_margin(cost: int, margin: int) -> int:
    return int((Decimal(cost) * (100 + margin) / 100).to_integral_value(
        rounding=ROUND_HALF_UP
    ))


async def _maybe_alert(bot: Bot, changed: list) -> None:
    """Сообщить владельцу, если цена скакнула заметно.

    Мелкие колебания курса не тревожим — иначе уведомления станут шумом
    и их перестанут читать.
    """
    from app.config import settings
    from app.services.delivery import notify_admins

    big = [
        (name, old, new)
        for name, old, new, _ in changed
        if old > 0 and abs(new - old) * 100 / old >= ALERT_PERCENT
    ]
    from app.services import access

    if not big or not access.admins():
        return

    lines = "\n".join(
        f"├ {name}: <b>{fmt4(old) if name == 'Звезда' else fmt(old)}</b> → "
        f"<b>{fmt4(new) if name == 'Звезда' else fmt(new)}</b>"
        for name, old, new in big
    )
    await notify_admins(
        bot,
        "📈 <b>Цены заметно изменились</b>\n\n" + lines
        + f"\n\n<blockquote>Наценка {runtime.margin_percent()}% сохранена. "
          "Если рост не устраивает — выключите автоцены "
          "в /panel → Цены.</blockquote>",
    )


async def auto_price_loop(provider, bot: Bot) -> None:
    """Фоновая задача: держать наценку постоянной."""
    while True:
        try:
            minutes = max(runtime.get_int("auto_price_every", 60), 5)
            await asyncio.sleep(minutes * 60)
            if not runtime.auto_price_on():
                continue

            conn = await db.connect()
            try:
                result = await refresh_once(conn, provider, bot)
            finally:
                await conn.close()

            if not result["ok"]:
                log.info("Автоцены пропущены: %s", result["reason"])
            elif result["changed"]:
                log.info("Автоцены обновлены: %s",
                         ", ".join(name for name, *_ in result["changed"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фон не должен умирать
            log.exception("Автоцены: непредвиденная ошибка: %s", exc)
            await asyncio.sleep(60)
