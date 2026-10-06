"""Наценка в панели бота сразу поднимает цены: звёзды, Premium и игры."""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from app import db, runtime  # noqa: E402
from app.services import games as gsvc  # noqa: E402
from app.services import pricing  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"{'✅' if condition else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Provider:
    """Сервис выдачи: звезда $0.015, Premium 3 мес. $12 (уже с наценкой Donatix)."""
    def __init__(self):
        self.star = Decimal("0.015")

    async def cost_estimate(self, kind, qty):
        if kind == "stars":
            return SimpleNamespace(usd_per_unit=self.star, usd_total=self.star * qty)
        return SimpleNamespace(usd_per_unit=Decimal("12"), usd_total=Decimal("12"))


async def main() -> None:
    conn = await db.connect()
    await db.init(conn)
    await runtime.load(conn)
    await runtime.set_value(conn, "usd_auto", "0")
    await runtime.set_value(conn, "usd_rate_diram", "1000")   # 10 сомони за $
    await runtime.set_value(conn, "margin_percent", "10")
    prov = Provider()

    await pricing.apply_margin_now(conn, prov)
    check("звезда: 0.15 с. + 10%", runtime.star_price_e4() == 1650, str(runtime.star_price_e4()))
    p3 = next(p for p in runtime.premium_plans() if int(p["months"]) == 3)
    check("Premium 3 мес.: 120 с. + 10%", int(p3["price"]) == 13200, str(p3))

    await runtime.set_value(conn, "margin_percent", "30")
    lines = await pricing.apply_margin_now(conn, prov)
    check("наценка 30% — звезда дороже сразу", runtime.star_price_e4() == 1950, str(runtime.star_price_e4()))
    check("в ответе видно, что поменялось", any("Звезда" in x for x in lines), str(lines))
    check("игры: пакет по новой наценке", gsvc.offer_price(Decimal("1"), 30) > gsvc.offer_price(Decimal("1"), 10))

    # Админ Donatix поднял свою наценку — закупка дороже, наценка бота сохраняется
    prov.star = Decimal("0.0165")
    await pricing.apply_margin_now(conn, prov)
    check("закупка выросла — цена тоже", runtime.star_price_e4() == 2145, str(runtime.star_price_e4()))

    # Сервис не ответил — пересчёт от сохранённой закупки
    await runtime.set_value(conn, "margin_percent", "20")
    await pricing.apply_margin_now(conn, None)
    check("без сервиса — от последней закупки", runtime.star_price_e4() == 1980, str(runtime.star_price_e4()))
    await conn.close()


asyncio.run(main())
print(f"\nПройдено: {len(PASS)}   Провалено: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
