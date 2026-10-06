"""D-коин — бонусная монета Donatix. Всё зависит только от покупок.

Как устроено и почему сайт никогда не уходит в минус:
  • Монеты. За каждый выполненный заказ клиент получает D-коины ровно по сумме заказа:
    100 D за $1, за $0.50 — 50 D.
  • Копилка. С того же заказа в копилку идёт часть НАШЕЙ ПРИБЫЛИ с него: обычный заказ — 10%,
    заказ крупнее обычного — больше (до 20%), мельче — меньше (от 5%). «Обычный» — средний
    заказ за последние сутки. Заказ без прибыли копилку не пополняет.
  • Цена 1 D = копилка ÷ все монеты (вместе с запасом сайта). Покупка больше предыдущей поднимает
    цену (зелёная свеча), меньше — опускает (красная), без покупок цена стоит. Одна покупка сдвигает цену не больше
    чем на 3%: рост сильнее — в копилку идёт меньше, падение сильнее — чуть больше (в пределах
    потолка), поэтому график идёт мягко. Ничего не подкручено: цена — это
    ровно те деньги, что лежат в копилке.
  • Обмен на баланс сайта по цене минус 5%: эти 5% остаются в копилке, и цена для остальных
    растёт. Больше, чем лежит в копилке, забрать невозможно, поэтому сайт отдаёт максимум
    20% прибыли заказа (в среднем около 10%), остальное всегда наше.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import accounts, db

UNIT = 100                        # монеты храним в сотых долях
DEFAULT_PER_USD = 100             # D за $1 в самом начале
DEFAULT_POOL_PCT = 10             # % прибыли обычного заказа — в копилку
MIN_SHARE, MAX_SHARE = 0.5, 2.0   # мелкий заказ — ×0.5 (5%), крупный — ×2 (20%)
MAX_STEP = 0.03                   # одна покупка двигает цену не больше чем на 3% — график мягкий
DEFAULT_REFUND_PCT = 2           # возврат денег за заказ — цена вниз на 2%
DEFAULT_OPEN_DAYS = 30            # обмен открывается через месяц после запуска
EXCHANGE_FEE_PCT = 5              # остаются в копилке — цена растёт для остальных
MIN_EXCHANGE = 10_000             # от $1 (микро-доллары)
TIMEFRAMES = {"1s": 1, "5s": 5, "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
CANDLES = 80
MAX_CANDLES = 400               # сколько свечей можно пролистать назад


# ── Настройки ───────────────────────────────────────────────────
def _int_setting(conn: sqlite3.Connection, key: str, default: int, lo: int, hi: int) -> int:
    raw = db.get_setting(conn, key)
    try:
        return max(lo, min(hi, int(raw))) if raw not in (None, "") else default
    except ValueError:
        return default


def per_usd(conn: sqlite3.Connection) -> int:
    return _int_setting(conn, "dcoin.per_usd", DEFAULT_PER_USD, 0, 10_000)


def pool_pct(conn: sqlite3.Connection) -> int:
    return _int_setting(conn, "dcoin.pool_pct", DEFAULT_POOL_PCT, 0, 25)


def refund_pct(conn: sqlite3.Connection) -> int:
    """На сколько % падает цена, когда заказ отменён и деньги вернулись. 0 — не падает."""
    return _int_setting(conn, "dcoin.refund_pct", DEFAULT_REFUND_PCT, 0, 10)


def open_days(conn: sqlite3.Connection) -> int:
    return _int_setting(conn, "dcoin.open_days", DEFAULT_OPEN_DAYS, 0, 365)


def enabled(conn: sqlite3.Connection) -> bool:
    return per_usd(conn) > 0 and pool_pct(conn) > 0


def started_at(conn: sqlite3.Connection) -> datetime:
    raw = db.get_setting(conn, "dcoin.started_at")
    if not raw:
        raw = db.now()
        db.set_setting(conn, "dcoin.started_at", raw)
    return _parse(raw)


def exchange_opens(conn: sqlite3.Connection) -> datetime:
    return started_at(conn) + timedelta(days=open_days(conn))


def exchange_open(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    return (now or _now()) >= exchange_opens(conn)


# ── Время и формат ──────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def fmt_d(units: int) -> str:
    """Сотые доли → «12 345.67»."""
    sign = "-" if units < 0 else ""
    units = abs(int(units))
    return f"{sign}{units // UNIT:,}".replace(",", " ") + f".{units % UNIT:02d}"


# ── Копилка и монеты ────────────────────────────────────────────
# Старт почти с нуля: у сайта есть запас монет, которых нет ни у кого на руках. Цена считается на
# все монеты вместе с запасом, и первая цена — не выше START_PRICE (≈ 0.0000218 с.). Каждая покупка двигает
# цену через запас сайта: больше предыдущей — вверх, меньше — вниз, от MIN_MOVE до MAX_STEP, плюс
# небольшой общий рост DRIFT. Запас не принадлежит людям, поэтому сколько бы его ни сжигали или
# ни добавляли, каждый получит не больше своей доли копилки.
# Доля копилки, которая «приходится» на запас, никому не выплачивается — она остаётся сайту.
START_PRICE = 0.000002            # $ за 1 D на старте (≈ 0.0000218 с.) — коротко и видно каждое движение
RESERVE0 = 1_000_000_000 * UNIT   # от этого считается предел запаса
MIN_MOVE = 0.01                   # каждая покупка двигает цену хотя бы на 1% — меняется последняя цифра
DRIFT = 0.005                     # общий рост на покупку, пока цена растёт с нуля
SIZE_MOVE = 0.012                 # вдвое больше предыдущей покупки — ещё +1.2%, вдвое меньше — −1.2%
KEEP = 0.10                       # держим запас около 10% всех монет — чтобы было чем двигать график
WICK = 0.4                        # тень свечи: цена «простреливает» на 40% дальше и откатывается назад


def _launch(conn: sqlite3.Connection) -> None:
    """Один раз: цена — со старта. Монеты клиентов и копилка сохраняются, график — заново."""
    if db.get_setting(conn, "dcoin.launch"):
        return
    db.set_setting(conn, "dcoin.launch", db.now())
    row = conn.execute("SELECT pool, supply FROM dcoin_points ORDER BY id DESC LIMIT 1").fetchone()
    conn.execute("DELETE FROM dcoin_points")
    pool, supply = (int(row["pool"]), int(row["supply"])) if row else (0, 0)
    _point(conn, pool, supply, "launch", reserve=_start_reserve(pool, supply))


def _start_reserve(pool: int, coins: int) -> int:
    """Сколько запаса нужно, чтобы цена была START_PRICE (если копилка уже больше — 0)."""
    per_unit = START_PRICE * 10_000 / UNIT             # микро-доллары за сотую долю монеты
    return max(0, int(pool / per_unit) - coins) if pool > 0 else 0


def _rebase(conn: sqlite3.Connection) -> None:
    """Один раз: если цена ушла слишком далеко в нули (0.000000000197 с.), поднять её к START_PRICE
    ровно в 10^k раз — вместе со всей историей графика, чтобы рост и падения остались теми же.
    Цена поднимается через запас сайта; выше «настоящей» (копилка ÷ монеты людей) — никогда."""
    if db.get_setting(conn, "dcoin.rebase1"):
        return
    db.set_setting(conn, "dcoin.rebase1", db.now())
    row = conn.execute("SELECT id, pool, supply, reserve FROM dcoin_points ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return
    now_price = price_of(int(row["pool"]), int(row["supply"]), int(row["reserve"]))
    if now_price <= 0 or now_price * 100 > START_PRICE:
        return
    factor = 10 ** round(math.log10(START_PRICE / now_price))
    per_unit = now_price * factor * 10_000 / UNIT
    reserve = int(int(row["pool"]) / per_unit) - int(row["supply"])
    if reserve < 0:
        return
    conn.execute("UPDATE dcoin_points SET price = price * ?", (factor,))
    conn.execute("UPDATE dcoin_points SET reserve = ? WHERE id = ?", (reserve, row["id"]))


def state(conn: sqlite3.Connection) -> dict[str, int]:
    _launch(conn)
    _rebase(conn)
    row = conn.execute("SELECT pool, supply, reserve FROM dcoin_points ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"pool": 0, "supply": 0, "reserve": 0}
    return {"pool": int(row["pool"]), "supply": int(row["supply"]), "reserve": int(row["reserve"])}


def price_of(pool: int, supply: int, reserve: int = 0) -> float:
    """$ за 1 D. pool — микро-доллары, supply и reserve — сотые доли монеты. До первой покупки — 0."""
    total = supply + reserve
    return (pool / 10_000) / (total / UNIT) if total > 0 else 0.0


def price(conn: sqlite3.Connection) -> float:
    s = state(conn)
    return price_of(s["pool"], s["supply"], s["reserve"])


def _point(conn: sqlite3.Connection, pool: int, supply: int, reason: str, when: str | None = None,
           *, reserve: int = 0) -> None:
    conn.execute("INSERT INTO dcoin_points (ts, pool, supply, reserve, price, reason) VALUES (?, ?, ?, ?, ?, ?)",
                 (when or db.now(), pool, supply, reserve, price_of(pool, supply, reserve), reason))


def reward_units(conn: sqlite3.Connection, usd: float) -> int:
    """Сколько монет (сотые) дать за покупку на usd: 100 D за $1, за $0.50 — 50 D."""
    return int(usd * per_usd(conn) * UNIT)


def on_created(conn: sqlite3.Connection, order_id: int) -> int:
    """Покупка оформлена — монеты сразу в истории и на счету, но «ждут выполнения»: обменять их
    нельзя, пока заказ не выполнен. Не выполнится — спишутся с пометкой «возврат»."""
    if not enabled(conn):
        return 0
    row = conn.execute("SELECT public_id, user_id, total_micro FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None or row["total_micro"] <= 0:
        return 0
    units = reward_units(conn, row["total_micro"] / 10_000)
    if units <= 0:
        return 0
    added = conn.execute(
        "INSERT OR IGNORE INTO dcoin_ledger (user_id, amount, pool_micro, reason, order_id, pending, created_at) "
        "VALUES (?, ?, 0, ?, ?, 1, ?)", (row["user_id"], units, f"Заказ {row['public_id']}", order_id, db.now())).rowcount
    if added:
        conn.execute("UPDATE users SET dcoin = dcoin + ? WHERE id = ?", (units, row["user_id"]))
    return units if added else 0


def on_cancel(conn: sqlite3.Connection, order_id: int) -> int:
    """Заказ не выполнен — монеты за него списываются, в истории строка «не выполнен — возврат»."""
    row = conn.execute("SELECT l.id, l.user_id, l.amount, o.public_id FROM dcoin_ledger l JOIN orders o "
                       "ON o.id = l.order_id WHERE l.order_id = ? AND l.pending = 1", (order_id,)).fetchone()
    if row is None:
        return 0
    if not conn.execute("UPDATE dcoin_ledger SET pending = 2 WHERE id = ? AND pending = 1", (row["id"],)).rowcount:
        return 0
    conn.execute("UPDATE users SET dcoin = dcoin - ? WHERE id = ?", (row["amount"], row["user_id"]))
    conn.execute("INSERT INTO dcoin_ledger (user_id, amount, pool_micro, reason, created_at) VALUES (?, ?, 0, ?, ?)",
                 (row["user_id"], -row["amount"], f"Заказ {row['public_id']} не выполнен — возврат", db.now()))
    return int(row["amount"])


def _award(conn: sqlite3.Connection, order_id: int) -> int:
    """За выполненный заказ: монеты клиенту (или подтверждение уже выданных при покупке) и часть
    прибыли в копилку. Один раз за заказ. Возвращает монеты (сотые) или 0."""
    if not enabled(conn):
        return 0
    row = conn.execute("SELECT id, public_id, user_id, total_micro, cost_micro, status FROM orders WHERE id = ?",
                       (order_id,)).fetchone()
    if row is None or row["status"] != "completed" or row["total_micro"] <= 0:
        return 0
    given = conn.execute("SELECT id, amount, pending FROM dcoin_ledger WHERE order_id = ?", (order_id,)).fetchone()
    if given is not None and given["pending"] != 1:
        return 0
    started_at(conn)
    usd = row["total_micro"] / 10_000
    cur = state(conn)
    units = int(given["amount"]) if given is not None else reward_units(conn, usd)
    if units <= 0:
        return 0
    profit = max(0, int(row["total_micro"]) - int(row["cost_micro"] or 0))
    ratio = share_ratio(conn, usd, order_id)
    # крупнее обычного — больше в копилку (до ×2), мельче — меньше (до ×0.5)
    to_pool = int(profit * pool_pct(conn) / 100 * max(MIN_SHARE, min(MAX_SHARE, math.sqrt(ratio))))
    cap = profit * pool_pct(conn) * 2 // 100                      # никогда больше 2× процента прибыли
    to_pool, reserve = steer(cur, units, min(to_pool, cap), cap, prev_ratio(conn, usd, order_id))
    if given is not None:   # монеты уже на счету с момента покупки — теперь они настоящие
        if not conn.execute("UPDATE dcoin_ledger SET pending = 0, pool_micro = ? WHERE id = ? AND pending = 1",
                            (to_pool, given["id"])).rowcount:
            return 0
    else:
        added = conn.execute(
            "INSERT OR IGNORE INTO dcoin_ledger (user_id, amount, pool_micro, reason, order_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["user_id"], units, to_pool, f"Заказ {row['public_id']}", order_id, db.now())).rowcount
        if not added:
            return 0
        conn.execute("UPDATE users SET dcoin = dcoin + ? WHERE id = ?", (units, row["user_id"]))
    _wick(conn, cur, cur["pool"] + to_pool, cur["supply"] + units, reserve)
    _point(conn, cur["pool"] + to_pool, cur["supply"] + units, "buy", reserve=reserve)
    return units


def share_ratio(conn: sqlite3.Connection, usd: float, order_id: int) -> float:
    """Во сколько раз заказ крупнее обычного. Обычный — типичный заказ последних суток (среднее
    геометрическое: пара крупных заказов не делает все остальные «мелкими»). Нет истории — 1."""
    since = _iso(_now() - timedelta(days=1))
    rows = conn.execute("SELECT total_micro FROM orders WHERE status = 'completed' AND id != ? AND total_micro > 0 "
                        "AND COALESCE(completed_at, created_at) >= ? ORDER BY id DESC LIMIT 500",
                        (order_id, since)).fetchall()
    if not rows or usd <= 0:
        return 1.0
    usual = math.exp(sum(math.log(r[0]) for r in rows) / len(rows)) / 10_000
    return usd / usual


def prev_ratio(conn: sqlite3.Connection, usd: float, order_id: int) -> float:
    """Во сколько раз покупка больше предыдущей: $5 после $4 — вверх, $4 после $5 — вниз."""
    row = conn.execute("SELECT total_micro FROM orders WHERE status = 'completed' AND id != ? AND total_micro > 0 "
                       "ORDER BY COALESCE(completed_at, created_at) DESC, id DESC LIMIT 1", (order_id,)).fetchone()
    return usd / (row[0] / 10_000) if row and usd > 0 else 1.0


def move_for(ratio: float, reserve_share: float = 1.0) -> float:
    """На сколько сдвинуть цену: больше предыдущей покупки — вверх, меньше — вниз, всегда заметно.
    Общий рост тем меньше, чем меньше осталось запаса: цена подходит к «настоящей» плавно и
    не упирается в неё, так что запаса хватает, чтобы график шевелился от каждой покупки."""
    m = DRIFT * (reserve_share - KEEP) / (1 - KEEP) + SIZE_MOVE * math.log2(max(ratio, 1e-6))
    # направление — всегда по сравнению с предыдущей покупкой; общий рост меняет только величину шага
    if ratio > 1.0001:
        m = max(m, MIN_MOVE)
    elif ratio < 0.9999:
        m = min(m, -MIN_MOVE)
    elif abs(m) < MIN_MOVE:
        m = MIN_MOVE if m >= 0 else -MIN_MOVE
    return max(-MAX_STEP, min(MAX_STEP, m))


def steer(cur: dict[str, int], units: int, to_pool: int, cap: int, ratio: float) -> tuple[int, int]:
    """Копилка и запас после покупки. Цена идёт на move_for(ratio) через запас сайта;
    если запаса уже нет — работает обычное сглаживание через копилку."""
    pool, supply, reserve = cur["pool"], cur["supply"], cur["reserve"]
    coins = supply + reserve
    if pool <= 0 or coins <= 0:                         # первая покупка — цена START_PRICE
        return to_pool, _start_reserve(to_pool, supply + units)
    target = pool / coins * (1 + move_for(ratio, reserve / coins))
    new_reserve = int((pool + to_pool) / target) - (supply + units)
    if new_reserve >= 0:
        limit = max(RESERVE0, supply) * 4               # запас не раздувается без предела
        if new_reserve <= limit:
            return to_pool, new_reserve
        # копилка растёт быстрее, чем запас успевает сдержать цену, — кладём меньше (экономия сайта)
        return max(0, min(to_pool, int(target * (supply + units + limit)) - pool)), limit
    return smooth(pool, supply, units, to_pool, cap), 0


def smooth(pool: int, coins: int, units: int, to_pool: int, cap: int) -> int:
    """Когда запаса нет: одна покупка двигает цену не больше чем на ±MAX_STEP.
    Рост сильнее — в копилку кладём меньше (экономия сайта); падение сильнее — добавляем,
    но не больше cap (потолок доли прибыли), так что минуса не бывает."""
    if pool <= 0 or coins <= 0:
        return to_pool
    total = coins + units
    hi = int(pool * (1 + MAX_STEP) * total / coins) - pool
    lo = math.ceil(pool * (1 - MAX_STEP) * total / coins) - pool
    if to_pool > hi:
        return max(0, hi)
    if to_pool < lo:
        return max(to_pool, min(lo, cap))
    return to_pool


def _wick(conn: sqlite3.Connection, before: dict[str, int], pool: int, supply: int, reserve: int) -> None:
    """Тень свечи, как на бирже: при покупке цена на миг уходит на WICK дальше и откатывается
    к новой цене. Точка «peak» даёт свече максимум (или минимум при падении); цена после — обычная.
    Тоже через запас сайта, поэтому и в этот миг каждому — не больше своей доли копилки."""
    old = price_of(before["pool"], before["supply"], before["reserve"])
    new = price_of(pool, supply, reserve)
    if old <= 0 or new <= 0 or pool <= 0:
        return
    peak = new + (new - old) * WICK
    per_unit = peak * 10_000 / UNIT                     # микро-доллары за сотую долю монеты
    peak_reserve = int(pool / per_unit) - supply if per_unit > 0 else -1
    if 0 <= peak_reserve <= max(RESERVE0, supply) * 4:
        _point(conn, pool, supply, "peak", reserve=peak_reserve)


def _on_refund(conn: sqlite3.Connection, order_id: int) -> bool:
    """Заказ отменён, деньги вернулись: монеты за него списываются, цена падает на refund_pct%
    (через запас сайта — копилка и монеты остальных не меняются). Зовётся там, где делается возврат."""
    on_cancel(conn, order_id)
    pct = refund_pct(conn)
    s = state(conn)
    coins = s["supply"] + s["reserve"]
    if pct <= 0 or s["pool"] <= 0 or coins <= 0:
        return False
    target = s["pool"] / coins * (1 - pct / 100)
    reserve = min(int(s["pool"] / target) - s["supply"], max(RESERVE0, s["supply"]) * 4)
    if reserve <= s["reserve"]:
        return False
    _wick(conn, s, s["pool"], s["supply"], reserve)
    _point(conn, s["pool"], s["supply"], "refund", reserve=reserve)
    return True


class ExchangeError(Exception):
    pass


def quote(conn: sqlite3.Connection, units: int) -> int:
    """Сколько микро-долларов дадут за units монет сейчас (с комиссией, округление в пользу копилки)."""
    s = state(conn)
    coins = s["supply"] + s["reserve"]
    if units <= 0 or coins <= 0:
        return 0
    return units * s["pool"] * (100 - EXCHANGE_FEE_PCT) // (coins * 100)


def exchange(conn: sqlite3.Connection, user_id: int, units: int, now: datetime | None = None) -> int:
    """Обменять монеты на баланс сайта. Возвращает зачисленное (микро)."""
    now = now or _now()
    if not exchange_open(conn, now):
        raise ExchangeError(f"Обмен откроется {exchange_opens(conn):%d.%m.%Y}.")
    with db.tx(conn):
        row = conn.execute("SELECT dcoin FROM users WHERE id = ?", (user_id,)).fetchone()
        have = (int(row["dcoin"]) if row else 0) - waiting(conn, user_id)   # за невыполненные заказы — нельзя
        if units <= 0 or units > have:
            raise ExchangeError("Столько D-коинов у вас нет." if units > (int(row["dcoin"]) if row else 0)
                                else "Часть монет ждёт выполнения заказов — их можно обменять позже.")
        last = conn.execute("SELECT created_at FROM dcoin_ledger WHERE user_id = ? AND amount < 0 "
                            "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if last and now - _parse(last["created_at"]) < timedelta(hours=24):
            raise ExchangeError("Обменивать можно раз в сутки.")
        s = state(conn)
        pay = quote(conn, units)
        if pay < MIN_EXCHANGE:
            raise ExchangeError("Обмен — от $1 по текущей цене.")
        if pay > s["pool"]:   # не бывает при цене = копилка ÷ монеты, но защищаемся
            raise ExchangeError("Сейчас столько обменять нельзя.")
        conn.execute("UPDATE users SET dcoin = dcoin - ? WHERE id = ?", (units, user_id))
        conn.execute("INSERT INTO dcoin_ledger (user_id, amount, pool_micro, reason, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", (user_id, -units, -pay, "Обмен на баланс", _iso(now)))
        _point(conn, s["pool"] - pay, s["supply"] - units, "exchange", reserve=s["reserve"])
        accounts.post_ledger(conn, user_id, pay, f"Обмен {fmt_d(units)} D-коинов на баланс")
    return pay


# ── График: свечи ───────────────────────────────────────────────
def candles(conn: sqlite3.Connection, tf: str = "5m", count: int = CANDLES,
            now: datetime | None = None) -> dict[str, Any]:
    """Свечи [время_мс, открытие, максимум, минимум, закрытие] за последние count интервалов
    (график листается назад — поэтому отдаём с запасом). Пустой интервал — ровная свеча."""
    tf = tf if tf in TIMEFRAMES else "5m"
    step = TIMEFRAMES[tf]
    count = max(10, min(MAX_CANDLES, int(count)))
    now = now or _now()
    end = int(now.timestamp()) // step * step
    start = end - (count - 1) * step
    since = _iso(datetime.fromtimestamp(start, timezone.utc))
    before = conn.execute("SELECT price FROM dcoin_points WHERE ts < ? ORDER BY id DESC LIMIT 1", (since,)).fetchone()
    last = float(before["price"]) if before else 0.0
    # свечи считает база: мин/макс и последняя цена в каждом интервале — не тянем все точки
    rows = conn.execute(
        "SELECT g.b, g.lo, g.hi, p.price AS close FROM (SELECT CAST(strftime('%s', substr(ts, 1, 19)) AS INTEGER) / ? "
        "AS b, MIN(price) AS lo, MAX(price) AS hi, MAX(id) AS last_id FROM dcoin_points WHERE ts >= ? GROUP BY b) g "
        "JOIN dcoin_points p ON p.id = g.last_id", (step, since)).fetchall()
    buckets = {int(r["b"]) * step: (float(r["lo"]), float(r["hi"]), float(r["close"])) for r in rows}
    out = []
    for t in range(start, end + step, step):
        o = last
        lo, hi, c = buckets.get(t, (o, o, o))
        out.append([t * 1000, o, max(o, hi), min(o, lo), c])
        last = c
    return {"tf": tf, "step": step, "candles": out}


def _site_midnight(conn: sqlite3.Connection, now: datetime | None = None) -> datetime:
    from . import timez
    local = (now or _now()).astimezone(timez.zone(timez.site_zone_name(conn)))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_stats(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict[str, Any] | None:
    """Сутки 00:00 → 24:00: цена на открытии, закрытии, максимум, минимум и изменение в %."""
    a, b = _iso(start.astimezone(timezone.utc)), _iso(end.astimezone(timezone.utc))
    before = conn.execute("SELECT price FROM dcoin_points WHERE ts < ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()
    row = conn.execute("SELECT MIN(price) AS lo, MAX(price) AS hi, COUNT(*) AS n FROM dcoin_points "
                       "WHERE ts >= ? AND ts < ? AND price > 0", (a, b)).fetchone()
    last = conn.execute("SELECT price FROM dcoin_points WHERE ts < ? ORDER BY id DESC LIMIT 1", (b,)).fetchone()
    first_today = conn.execute("SELECT price FROM dcoin_points WHERE ts >= ? AND ts < ? AND price > 0 "
                               "ORDER BY id LIMIT 1", (a, b)).fetchone()
    if before and before["price"] > 0:
        open_p = float(before["price"])
    else:
        open_p = float(first_today["price"]) if first_today else 0.0
    close_p = float(last["price"]) if last else 0.0
    if open_p <= 0 or close_p <= 0:
        return None
    hi = max(open_p, float(row["hi"] or 0))
    lo = min(open_p, float(row["lo"] or open_p))
    return {"day": start, "open": open_p, "close": close_p, "high": hi, "low": lo, "trades": int(row["n"] or 0),
            "change": round((close_p - open_p) / open_p * 100, 2)}


def change_today(conn: sqlite3.Connection, now: datetime | None = None) -> float:
    """Изменение цены за сегодня: в 00:00 (по времени сайта) счёт начинается с 0%."""
    start = _site_midnight(conn, now)
    d = _day_stats(conn, start, start + timedelta(days=1))
    return d["change"] if d else 0.0


def days(conn: sqlite3.Connection, n: int = 14, now: datetime | None = None) -> list[dict[str, Any]]:
    """Итоги прошлых дней: на сколько поднялась и опустилась цена с 00:00 до 24:00. Свежие сверху."""
    today = _site_midnight(conn, now)
    out = []
    for back in range(1, n + 1):
        start = today - timedelta(days=back)
        d = _day_stats(conn, start, start + timedelta(days=1))
        if d:
            out.append(d)
    return out


# ── Для страниц ─────────────────────────────────────────────────
def balance(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute("SELECT dcoin FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row["dcoin"]) if row else 0


def summary(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    bal, wait = balance(conn, user_id), waiting(conn, user_id)
    s = state(conn)
    return {"balance": bal, "balance_text": fmt_d(bal), "worth_micro": quote(conn, bal),
            "waiting": wait, "waiting_text": fmt_d(wait), "free": max(0, bal - wait),
            "price": price_of(s["pool"], s["supply"], s["reserve"]), "change": change_today(conn),
            "per_usd": per_usd(conn), "pool_pct": pool_pct(conn),
            "fee_pct": EXCHANGE_FEE_PCT, "open": exchange_open(conn), "opens": exchange_opens(conn),
            "enabled": enabled(conn)}


def history(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Все зачисления и списания: покупки, возвраты, обмены — ничего не пропадает."""
    return [{"amount": fmt_d(r["amount"]), "plus": r["amount"] > 0, "reason": r["reason"],
             "created_at": r["created_at"], "wait": r["pending"] == 1}
            for r in conn.execute("SELECT amount, reason, created_at, pending FROM dcoin_ledger WHERE user_id = ? "
                                  "ORDER BY id DESC LIMIT ?", (user_id, limit))]


def waiting(conn: sqlite3.Connection, user_id: int) -> int:
    """Монеты за заказы, которые ещё выполняются: на счету, но обменять их пока нельзя."""
    return int(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM dcoin_ledger WHERE user_id = ? AND pending = 1",
                            (user_id,)).fetchone()[0])


def top(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT login, dcoin FROM users WHERE dcoin > 0 ORDER BY dcoin DESC, id LIMIT ?",
                        (limit,)).fetchall()
    return [{"login": (r["login"][:2] + "•••" + r["login"][-1:]) if len(r["login"]) > 3 else r["login"],
             "coins": fmt_d(r["dcoin"])} for r in rows]


def pool_added(conn: sqlite3.Connection, a: str, b: str) -> int:
    """Сколько прибыли ушло в копилку за [a, b) — для отчёта о финансах."""
    return int(conn.execute("SELECT COALESCE(SUM(pool_micro), 0) FROM dcoin_ledger WHERE pool_micro > 0 "
                            "AND created_at >= ? AND created_at < ?", (a, b)).fetchone()[0])


def _atomic(conn: sqlite3.Connection, fn, order_id: int):
    """Прочитать состояние графика и записать новую точку — под одной блокировкой.
    Иначе два заказа, выполненные одновременно в разных процессах, затёрли бы монеты друг друга."""
    if conn.in_transaction:
        return fn(conn, order_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn, order_id)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return result


def award(conn: sqlite3.Connection, order_id: int) -> int:
    """За выполненный заказ: монеты клиенту и доля прибыли в копилку (см. _award)."""
    return _atomic(conn, _award, order_id)


def on_refund(conn: sqlite3.Connection, order_id: int) -> bool:
    """Возврат за заказ: монеты списываются, цена чуть вниз (см. _on_refund)."""
    return _atomic(conn, _on_refund, order_id)
