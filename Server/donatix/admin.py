"""Админка: клиенты, балансы, заказы, каталог, состояние поставщика."""

from __future__ import annotations

import html
import json
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from . import accounts, cache, catalog, db, orders, payments, shopbot, worker
from .config import PAY_METHODS, TIERS, Config
from .deps import Forbidden, LoginRequired, check_csrf, flash, get_config, get_conn, render, session_user
from .money import MoneyError, apply_markup, fmt, fmt_unit, to_decimal, to_micro
from .suppliers import KINDS

router = APIRouter(prefix="/admin", include_in_schema=False)


def admin_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Row:
    user = session_user(request, conn)
    if user is None:
        raise LoginRequired()
    if user["role"] != "admin":
        raise Forbidden("Нет доступа.")
    return user


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("")
def dashboard(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
              config: Config = Depends(get_config)):
    counts = {
        "pending_users": conn.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'").fetchone()[0],
        "clients": conn.execute("SELECT COUNT(*) FROM users WHERE role = 'client'").fetchone()[0],
        "attention": conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'attention'").fetchone()[0],
        "processing": conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'processing'").fetchone()[0],
        "pending_payments": conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'pending'").fetchone()[0],
        "failed_today": conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'failed' AND created_at >= date('now')"
                                     ).fetchone()[0],
        "products": conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0],
        "client_balances": conn.execute(
            "SELECT COALESCE(SUM(balance_micro), 0) FROM users WHERE role = 'client'").fetchone()[0],
    }
    return render(request, "admin/dashboard.html", {
        "user": admin,
        "counts": counts,
        "today": orders.stats(conn, 1),
        "shop": shopbot.stats(conn, 1),
        # Тяжёлые сводки за месяц/две недели — из кеша на минуту: админка не тормозит сайт клиентам
        "month": cache.get_or_set("admin:month", 60, lambda: orders.stats(conn, 30)),
        "days": cache.get_or_set("admin:days", 60, lambda: orders.daily(conn, 14)),
        "top": cache.get_or_set("admin:top", 60, lambda: orders.top_clients(conn)),
        "supplier_balance": worker.supplier_balance_cached(conn),
        "supplier_balance_at": db.get_setting(conn, "supplier_balance_at"),
        "supplier_balance_error": db.get_setting(conn, "supplier_balance_error") or "",
        "catalog_synced_at": db.get_setting(conn, "catalog_synced_at"),
        "extra_balances": _extra_balances(request.app.state.supplier),
        "supplier_name": config.supplier,
        "low": config.supplier_low_balance,
        "recent": orders.with_images(conn, conn.execute(
            "SELECT o.*, u.login FROM orders o JOIN users u ON u.id = o.user_id ORDER BY o.id DESC LIMIT 10"
        ).fetchall()),
    })


def _extra_balances(supplier) -> list:
    """Балансы дополнительных поставщиков (CoinDrop) для сводки — раз в 5 минут, чтобы не ловить их лимит."""
    if not hasattr(supplier, "extra_balances"):
        return []
    return cache.get_or_set("admin:extra_balances", 300, supplier.extra_balances)


@router.get("/stats")
def stats_page(request: Request, period: str = "30d", admin=Depends(admin_user), conn=Depends(get_conn),
               config: Config = Depends(get_config)):
    from . import analytics
    a = cache.get_or_set(f"admin:stats:{period}", 60, lambda: analytics.build(conn, period, tz_hours=config.tz_offset))
    supplier = worker.supplier_balance_cached(conn)
    owed = conn.execute("SELECT COALESCE(SUM(balance_micro), 0) FROM users WHERE role = 'client'").fetchone()[0]
    supplier_micro = int(supplier * 10_000) if supplier is not None else None
    return render(request, "admin/stats.html", {
        "user": admin, "a": a,
        "fin": {
            "cost": a["turnover"] - a["profit"], "supplier": supplier_micro, "owed": owed,
            # Свободно: деньги у поставщика сверх того, что вы должны клиентам
            "free": supplier_micro - owed if supplier_micro is not None else None,
            "margin": round(a["profit"] / a["turnover"] * 100, 1) if a["turnover"] else 0,
        },
    })


@router.get("/money")
def money_page(request: Request, period: str = "", date_from: str = "", date_to: str = "",
               admin=Depends(admin_user), conn=Depends(get_conn), config: Config = Depends(get_config)):
    """Учёт денег: все движения за период на одной странице — откуда пришли и куда ушли."""
    from . import finance, periods, timez
    pr = periods.resolve(timez.zone(timez.site_zone_name(conn)), period, date_from, date_to, default="30d")
    m = cache.get_or_set(f"admin:money:{pr.key}:{pr.date_from}:{pr.date_to}", 30,
                         lambda: finance.ledger(conn, config, pr))
    return render(request, "admin/money.html", {
        "user": admin, "m": m, "s": m["total"], "pr": pr, "presets": periods.PRESETS, "keep": "",
        "site_tz": timez.label(timez.site_zone_name(conn)), "tjs": finance.tjs, "usd": finance.usd,
    })


@router.get("/finance")
def finance_page(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)):
    from . import finance, timez
    days = cache.get_or_set("admin:finance", 30, lambda: finance.history(conn, config, 14))
    return render(request, "admin/finance.html", {
        "user": admin, "now": days[0], "days": days, "cutoff": finance.cutoff_hour(conn),
        "site_tz": timez.label(timez.site_zone_name(conn)), "tjs": finance.tjs, "usd": finance.usd,
    })


@router.post("/finance/send", dependencies=[Depends(check_csrf)])
def finance_send(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)):
    """Прислать отчёт за последние закрытые сутки в админ-бот прямо сейчас."""
    from . import finance
    from .worker import notify_admin
    start, end = finance.window(conn, days_back=1)
    notify_admin(config, finance.report_text(finance.summary(conn, config, start, end)), html=True)
    flash(request, "Отчёт отправлен в админ-бот.")
    return RedirectResponse("/admin/finance", status_code=303)


@router.get("/traffic")
def traffic_page(request: Request, period: str = "7d", admin=Depends(admin_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)):
    from . import traffic
    traffic.flush(config.db_path, force=True)  # свежие просмотры — сразу в отчёт
    return render(request, "admin/traffic.html", {
        "user": admin, "t": cache.get_or_set(f"admin:traffic:{period}", 60,
                                              lambda: traffic.report(conn, period, tz_hours=config.tz_offset)),
        "page_title": traffic.page_title, "duration": traffic.duration,
    })


@router.get("/pricelist")
def pricelist_page(request: Request, show: str = "all", fmt: str = "post", admin=Depends(admin_user),
                   conn=Depends(get_conn), config: Config = Depends(get_config)):
    from datetime import datetime, timedelta, timezone

    from . import pricelist
    data = pricelist.build(conn, config)
    keys = [s["key"] for s in data["sections"]]
    show = show if show in keys else "all"
    sections = data["sections"] if show == "all" else [s for s in data["sections"] if s["key"] == show]
    today = datetime.now(timezone(timedelta(hours=config.tz_offset))).strftime("%d.%m.%Y")
    return render(request, "admin/pricelist.html", {
        "user": admin, "data": data, "sections": sections, "show": show, "fmt": "story" if fmt == "story" else "post",
        # для рисования в браузере — только то, что попадёт на картинку
        "cards": [{"key": s["key"], "game": s["game"], "sub": s["sub"],
                   "packs": [{"short": p["short"], "price": p["price"], "group": p["group"]} for p in s["packs"]]}
                  for s in sections],
        "today": today, "site_host": config.base_url.split("://")[-1].rstrip("/"),
    })


@router.get("/settings")
def settings_page(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                  config: Config = Depends(get_config)):
    from . import sitecfg
    return render(request, "admin/settings.html", {
        "user": admin, "s": sitecfg.view(conn, config), "tier_titles": sitecfg.TIER_TITLES,
        "kind_titles": sitecfg.KIND_TITLES,
        "tg_on": bool(config.alert_telegram_token and config.alert_telegram_chat_id),
    })


@router.post("/settings", dependencies=[Depends(check_csrf)])
async def settings_save(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                        config: Config = Depends(get_config)):
    from . import sitecfg
    form = await request.form()
    data = {k: str(v) for k, v in form.items() if k != "csrf"}
    for flag in ("reg_open", "client_bots", "require_approval", "watch_on", "admin_2fa", "daily_report"):
        data[flag] = "1" if form.get(flag) == "1" else "0"
    try:
        sitecfg.save(conn, config, data)
    except sitecfg.SettingsError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, "Настройки сохранены — уже действуют на сайте и в ботах.")
    return _back("/admin/settings")


@router.post("/supplier-balance", dependencies=[Depends(check_csrf)])
def supplier_balance_refresh(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                             config: Config = Depends(get_config)):
    """Кнопка «Обновить» у баланса FazerCards: спросить прямо сейчас и показать причину, если не вышло."""
    bal = worker.check_supplier_balance(conn, config, request.app.state.supplier)
    if bal is None:
        flash(request, "Баланс не пришёл: " + (db.get_setting(conn, "supplier_balance_error") or "нет ответа"), "error")
    else:
        flash(request, f"Баланс FazerCards: ${bal}")
    return _back("/admin")


@router.post("/sync", dependencies=[Depends(check_csrf)])
def sync_now(request: Request, admin=Depends(admin_user), config: Config = Depends(get_config)):
    """Кнопка «Обновить каталог»: только запускает фоновую задачу — страница не ждёт поставщика."""
    from . import catalog_job
    catalog_job.start(config, request.app.state.supplier, sync=True, images=False)
    return _back("/admin/catalog-sync")


@router.get("/bots")
def bots_page(request: Request, admin=Depends(admin_user), conn=Depends(get_conn)):
    from . import bots
    clients = conn.execute(
        "SELECT id, login, email, project FROM users WHERE status = 'active' ORDER BY login").fetchall()
    return render(request, "admin/bots.html", {
        "user": admin, "bots": bots.listing(conn), "clients": clients,
        "runner": bots.runner_alive(conn), "template_ok": bots.TEMPLATE_DIR.exists(),
    })


@router.post("/bots", dependencies=[Depends(check_csrf)])
def bots_add(request: Request, token: str = Form(""), admin_ids: str = Form(""), user_id: int = Form(0),
             admin=Depends(admin_user), conn=Depends(get_conn), config: Config = Depends(get_config)):
    from . import bots
    try:
        username = bots.check_token(token)
        bots.create(conn, config, user_id=user_id or admin["id"], token=token, admin_ids=admin_ids, username=username)
    except bots.BotError as exc:
        flash(request, str(exc), "error")
        return _back("/admin/bots")
    if bots.RUNNER:
        bots.RUNNER.poke()
    flash(request, f"Бот @{username} подключён — запустится в течение минуты. "
                   "Откройте его в Telegram и нажмите /start.")
    return _back("/admin/bots")


@router.post("/bots/{bot_id}/{action}", dependencies=[Depends(check_csrf)])
def bots_action(bot_id: int, action: str, request: Request, admin_ids: str = Form(""), admin=Depends(admin_user),
                conn=Depends(get_conn)):
    from . import bots
    try:
        if action == "stop":
            bots.set_enabled(conn, bot_id, False, by_admin=True)
        elif action == "start":
            owner = conn.execute("SELECT user_id FROM bots WHERE id = ?", (bot_id,)).fetchone()
            bots.set_enabled(conn, bot_id, True)
            if owner:
                bots.unblock_user(conn, owner["user_id"])   # админ включил — запрет с клиента снят
        elif action == "restart":
            bots.set_enabled(conn, bot_id, True)  # updated_at меняется — процесс перезапустится
        elif action == "admins":
            bots.update_admins(conn, bot_id, admin_ids)
        elif action == "delete":
            bots.delete(conn, bot_id)
    except bots.BotError as exc:
        flash(request, str(exc), "error")
    if bots.RUNNER:
        bots.RUNNER.poke()
    return _back("/admin/bots")


@router.get("/bots/{bot_id}/log")
def bots_log(bot_id: int, admin=Depends(admin_user), config: Config = Depends(get_config)):
    from fastapi.responses import PlainTextResponse

    from . import bots
    return PlainTextResponse(bots.log_tail(config, bot_id, 200) or "Лог пока пуст.",
                             headers={"Cache-Control": "no-store"})


@router.get("/catalog-sync")
def catalog_sync_page(request: Request, admin=Depends(admin_user), conn=Depends(get_conn)):
    from . import catalog_job
    counts = conn.execute(
        "SELECT COUNT(*) AS products, COUNT(DISTINCT category_id) AS categories, "
        "COUNT(DISTINCT image_url) AS images FROM products WHERE active = 1").fetchone()
    return render(request, "admin/catalog_sync.html", {
        "user": admin, "job": catalog_job.status(conn), "counts": counts,
        "synced_at": db.get_setting(conn, "catalog_synced_at"),
        "providers": request.app.state.supplier.providers() if hasattr(request.app.state.supplier, "providers") else [],
    })


@router.post("/catalog-sync/start", dependencies=[Depends(check_csrf)])
def catalog_sync_start(request: Request, mode: str = Form("all"), admin=Depends(admin_user),
                       config: Config = Depends(get_config)):
    from . import catalog_job
    if mode.startswith("provider:"):
        started = catalog_job.start(config, request.app.state.supplier, sync=True, images=False,
                                    provider=mode.split(":", 1)[1])
    else:
        started = catalog_job.start(config, request.app.state.supplier,
                                    sync=mode in ("all", "catalog"), images=mode in ("all", "images"))
    if not started:
        flash(request, "Загрузка уже идёт — прогресс ниже.", "warn")
    return _back("/admin/catalog-sync")


@router.get("/catalog-sync/status")
def catalog_sync_status(admin=Depends(admin_user), conn=Depends(get_conn)):
    from . import catalog_job
    return catalog_job.status(conn)


# ── Клиенты ──────────────────────────────────────────────────


@router.get("/users")
def users(request: Request, status: str = "", q: str = "", admin=Depends(admin_user), conn=Depends(get_conn)):
    where, args = "1=1", []
    if status in ("pending", "active", "blocked"):
        where += " AND status = ?"
        args.append(status)
    if q:
        where += " AND (email LIKE ? OR login LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    rows = conn.execute(
        f"SELECT u.*, (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id AND o.status = 'completed') AS n_orders "
        f"FROM users u WHERE {where} ORDER BY (status = 'pending') DESC, id DESC LIMIT 200", args
    ).fetchall()
    return render(request, "admin/users.html", {"user": admin, "rows": rows, "status": status, "q": q})


@router.get("/users/{user_id}")
def user_detail(user_id: int, request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                config: Config = Depends(get_config)):
    client = accounts.get_user(conn, user_id)
    if client is None:
        flash(request, "Нет такого пользователя.", "error")
        return _back("/admin/users")
    return render(request, "admin/user.html", {
        "user": admin, "c": client, "tiers": TIERS, "markups": config.markups,
        "effective_markup": accounts.markup_for(client, config),
        "txs": conn.execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 30",
                            (user_id,)).fetchall(),
        "orders": conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 20",
                               (user_id,)).fetchall(),
        "pays": _pays_with_who(conn, "WHERE p.user_id = ? ORDER BY p.id DESC LIMIT 30", (user_id,)),
        "pay_titles": _pay_titles(conn, config),
    })


def _pays_with_who(conn, where: str, args: tuple) -> list[dict]:
    """Заявки на пополнение с «кто решил» — админ, кассир по имени, автоматически, клиент."""
    from .payments import who_label
    rows = conn.execute(f"SELECT p.*, u.login, u.balance_micro FROM payments p JOIN users u ON u.id = p.user_id "
                        f"{where}", args).fetchall()
    import json

    from .receipt_ai import summary
    return [{**dict(r), "who": who_label(r),
             "ai": summary(json.loads(r["receipt_ai"]), r["pay_amount"], r["pay_currency"] or "")
             if r["receipt_ai"] else ""} for r in rows]


def _pay_titles(conn, config: Config) -> dict[str, str]:
    from .payments import settings
    return ({k: v[0] for k, v in PAY_METHODS.items()}
            | {m["code"]: m["title"] for m in settings(conn, config)["all_methods"]})


@router.post("/users/{user_id}", dependencies=[Depends(check_csrf)])
def user_update(user_id: int, request: Request, status: str = Form(...), tier: str = Form(...),
                markup_override: str = Form(""), admin=Depends(admin_user), conn=Depends(get_conn)):
    if user_id == admin["id"] and status != "active":
        flash(request, "Нельзя отключить самого себя.", "error")
        return _back(f"/admin/users/{user_id}")
    before = accounts.get_user(conn, user_id)
    try:
        accounts.update_user_admin(conn, user_id, status=status, tier=tier, markup_override=markup_override)
        flash(request, "Сохранено.")
        if before and before["status"] != "active" and status == "active":
            from .notify import notify
            notify(conn, request.app.state.config, user_id,
                   "Аккаунт активирован — можно пополнять баланс и делать заказы.", "/panel")
    except accounts.AccountError as exc:
        flash(request, str(exc), "error")
    return _back(f"/admin/users/{user_id}")


@router.post("/users/{user_id}/balance", dependencies=[Depends(check_csrf)])
def user_balance(user_id: int, request: Request, amount: str = Form(...), note: str = Form(""),
                 admin=Depends(admin_user), conn=Depends(get_conn)):
    try:
        micro = to_micro(amount.replace(",", ".").replace("$", ""))
    except MoneyError:
        flash(request, "Сумма — число, например 100 или -5.50", "error")
        return _back(f"/admin/users/{user_id}")
    if micro == 0:
        flash(request, "Сумма не может быть нулём.", "error")
        return _back(f"/admin/users/{user_id}")
    text = note.strip() or ("Пополнение баланса" if micro > 0 else "Списание")
    try:
        with db.tx(conn):
            accounts.post_ledger(conn, user_id, micro, text, created_by=admin["id"])
    except accounts.InsufficientBalance:
        flash(request, "Столько списать нельзя: баланс уйдёт в минус.", "error")
        return _back(f"/admin/users/{user_id}")
    from .notify import notify
    notify(conn, request.app.state.config, user_id,
           (f"Баланс пополнен на ${fmt(micro)}" if micro > 0 else f"С баланса списано ${fmt(-micro)}") + f": {text}.",
           "/panel/transactions")
    flash(request, f"Баланс изменён на ${fmt(micro)}.")
    return _back(f"/admin/users/{user_id}")


# ── Заказы ───────────────────────────────────────────────────


@router.get("/orders")
def orders_list(request: Request, status: str = "", q: str = "", period: str = "", date_from: str = "",
                date_to: str = "", admin=Depends(admin_user), conn=Depends(get_conn)):
    from urllib.parse import urlencode

    from . import periods, timez
    pr = periods.resolve(timez.zone(timez.site_zone_name(conn)), period, date_from, date_to)
    cond, where_args = pr.sql("o.created_at")
    where, args = cond, list(where_args)
    if status in ("processing", "completed", "failed", "attention"):
        where += " AND o.status = ?"
        args.append(status)
    if q:
        where += " AND (o.public_id LIKE ? OR o.supplier_order_id LIKE ? OR o.product_name LIKE ? OR u.login LIKE ?)"
        args += [f"%{q}%"] * 4
    rows = conn.execute(
        f"SELECT o.*, u.login FROM orders o JOIN users u ON u.id = o.user_id WHERE {where} "
        f"ORDER BY (o.status = 'attention') DESC, o.id DESC LIMIT 200", args
    ).fetchall()
    rows = orders.with_images(conn, rows)
    totals = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(o.status = 'completed'), 0) AS done, "
        "COALESCE(SUM(o.status = 'failed'), 0) AS failed, "
        "COALESCE(SUM(CASE WHEN o.status = 'completed' THEN o.total_micro END), 0) AS revenue, "
        "COALESCE(SUM(CASE WHEN o.status = 'completed' THEN o.total_micro - o.cost_micro END), 0) AS profit "
        f"FROM orders o JOIN users u ON u.id = o.user_id WHERE {where}", args).fetchone()
    return render(request, "admin/orders.html", {
        "user": admin, "rows": rows, "status": status, "q": q, "pr": pr, "presets": periods.PRESETS,
        "keep": urlencode({"status": status, "q": q}), "totals": totals})


@router.get("/orders/{order_id}")
def order_detail(order_id: int, request: Request, admin=Depends(admin_user), conn=Depends(get_conn)):
    o = conn.execute("SELECT o.*, u.login, u.email FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
                     (order_id,)).fetchone()
    if o is None:
        flash(request, "Нет такого заказа.", "error")
        return _back("/admin/orders")
    delivery = json.loads(o["delivery_json"]) if o["delivery_json"] else None
    return render(request, "admin/order.html", {
        "user": admin, "o": o, "fields": json.loads(o["fields_json"] or "{}"),
        "delivery_pretty": json.dumps(delivery, ensure_ascii=False, indent=2) if delivery else "",
    })


@router.post("/orders/{order_id}/refund", dependencies=[Depends(check_csrf)])
def order_refund(order_id: int, request: Request, reason: str = Form(""), admin=Depends(admin_user),
                 conn=Depends(get_conn)):
    done = orders.fail_and_refund(conn, order_id, reason.strip() or "Отменён администратором", by_admin=admin["id"])
    flash(request, "Заказ отменён, деньги возвращены." if done else "Заказ уже завершён — возврат не сделан.",
          "ok" if done else "error")
    return _back(f"/admin/orders/{order_id}")


@router.post("/orders/{order_id}/complete", dependencies=[Depends(check_csrf)])
def order_complete(order_id: int, request: Request, note: str = Form(""), admin=Depends(admin_user),
                   conn=Depends(get_conn)):
    done = orders.admin_complete(conn, order_id, note)
    flash(request, "Отмечен выполненным." if done else "Заказ уже завершён.", "ok" if done else "error")
    return _back(f"/admin/orders/{order_id}")


@router.post("/orders/{order_id}/recheck", dependencies=[Depends(check_csrf)])
def order_recheck(order_id: int, request: Request, admin=Depends(admin_user), conn=Depends(get_conn)):
    orders.admin_recheck(conn, order_id)
    row = orders.get_order_row(conn, order_id)
    if row and row["status"] == "processing":
        orders.refresh(conn, request.app.state.supplier, row, from_worker=False)
    flash(request, "Статус запрошен у поставщика.")
    return _back(f"/admin/orders/{order_id}")


# ── Каталог ──────────────────────────────────────────────────


@router.get("/products")
def products(request: Request, kind: str = "", q: str = "", admin=Depends(admin_user), conn=Depends(get_conn),
             config: Config = Depends(get_config)):
    items = catalog.list_products(conn, kind=kind if kind in KINDS else "", q=q[:100], include_hidden=True)
    for p in items:
        km = config.kind_markups.get(p["kind"])
        p["prices"] = {t: fmt_unit(apply_markup(to_decimal(p["base_price"]), km if km is not None else m))
                       for t, m in config.markups.items()}
    return render(request, "admin/products.html", {
        "user": admin, "items": items, "kind": kind, "q": q, "kinds": KINDS, "markups": config.markups,
    })


@router.post("/products/{product_id}/toggle", dependencies=[Depends(check_csrf)])
def product_toggle(product_id: str, request: Request, admin=Depends(admin_user), conn=Depends(get_conn)):
    conn.execute("UPDATE products SET hidden = 1 - hidden WHERE id = ?", (product_id,))
    from . import cache
    cache.clear_everywhere(conn)
    return _back(request.headers.get("referer") or "/admin/products")


# ── Пополнения ───────────────────────────────────────────────


@router.get("/payments")
def payments_list(request: Request, status: str = "pending", admin=Depends(admin_user), conn=Depends(get_conn)):
    where, args = "1=1", []
    if status in ("pending", "paid", "rejected", "cancelled", "fake"):
        where += " AND p.status = ?"
        args.append(status)
    rows = _pays_with_who(conn, f"WHERE {where} ORDER BY p.id DESC LIMIT 200", tuple(args))
    return render(request, "admin/payments.html", {
        "user": admin, "rows": rows, "status": status,
        "titles": {k: v[0] for k, v in PAY_METHODS.items()}
        | {m["code"]: m["title"] for m in payments.settings(conn, request.app.state.config)["all_methods"]},
    })


@router.get("/pay-settings")
def pay_settings(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)):
    from . import payments
    return render(request, "admin/pay_settings.html", {
        "user": admin, "conf": payments.settings(conn, config), "currencies": payments.CURRENCY_CHOICES,
        "binance_ready": bool(config.binance_pay_key and config.binance_pay_secret),
        "bybit_ready": bool(config.bybit_key and config.bybit_secret),
        "rate": _rate_status(conn, config),
    })


def _rate_status(conn, config: Config) -> dict:
    from . import rates
    rates.refresh(conn, config, rates.WORKER_SECONDS)
    return rates.status(conn, config)


@router.post("/pay-settings", dependencies=[Depends(check_csrf)])
async def pay_settings_save(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                            config: Config = Depends(get_config)):
    from . import payments
    form = await request.form()
    old_icons = {m["code"]: m.get("icon", "") for m in payments.settings(conn, config)["all_methods"]}
    rows = []
    try:
        for i in range(int(form.get("n", 0) or 0) + 1):  # +1 — строка «новый способ»
            row = ({k: str(form.get(f"m{i}_{k}", "")) for k in ("code", "title", "currency", "details", "auto")}
                   | {"enabled": form.get(f"m{i}_enabled") == "1", "delete": form.get(f"m{i}_delete") == "1"})
            row["icon"] = "" if form.get(f"m{i}_icon_del") == "1" else old_icons.get(row["code"], "")
            upload = form.get(f"m{i}_icon")
            if upload is not None and getattr(upload, "filename", ""):
                data = await upload.read(payments.MAX_ICON_BYTES + 1)
                if data:
                    row["icon"] = payments.save_icon(config, data, upload.content_type or "")
            rows.append(row)
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
        return _back("/admin/pay-settings")
    try:
        payments.save_settings(conn, config, rows, str(form.get("tjs_rate", "")), str(form.get("min_tjs", "")),
                               str(form.get("low_usd", "")), rate_auto=form.get("rate_auto") == "1",
                               margin_pct=str(form.get("rate_margin", "")))
        from . import rates
        if form.get("rate_auto") == "1":
            rates.refresh(conn, config, force=True)
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, "Реквизиты сохранены — клиенты уже видят их на странице пополнения.")
    return _back("/admin/pay-settings")


@router.get("/payments/{payment_id}/receipt")
def payment_receipt_file(payment_id: int, admin=Depends(admin_user), conn=Depends(get_conn),
                         config: Config = Depends(get_config)):
    from fastapi.responses import FileResponse, Response
    row = conn.execute("SELECT receipt_file FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not row or not row["receipt_file"]:
        return Response("Чека нет", status_code=404)
    path = payments.receipts_dir(config) / row["receipt_file"]
    return FileResponse(path, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/payments/{payment_id}/confirm", dependencies=[Depends(check_csrf)])
def payment_confirm(payment_id: int, request: Request, credit: str = Form(""), admin=Depends(admin_user),
                    conn=Depends(get_conn), config: Config = Depends(get_config)):
    from . import payments
    try:
        ok = payments.confirm(conn, config, payment_id, admin["id"], credit.strip() or None,
                              who=f"админ (сайт: {admin['login']})")
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
        return _back("/admin/payments")
    if ok:
        from .cashiers import settle
        from .money import fmt
        amount = conn.execute("SELECT amount_micro FROM payments WHERE id = ?", (payment_id,)).fetchone()[0]
        settle(conn, config, payment_id, f"✅ Зачислено ${fmt(amount)} — админ (сайт)")
    flash(request, "Баланс зачислен, клиент уведомлён." if ok else "Заявка уже обработана.", "ok" if ok else "error")
    return _back("/admin/payments")


@router.post("/payments/{payment_id}/fake", dependencies=[Depends(check_csrf)])
def payment_fake(payment_id: int, request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)):
    from . import payments
    from .money import fmt
    try:
        debt = payments.mark_fake(conn, config, payment_id, admin["id"], who=f"админ (сайт: {admin['login']})")
    except payments.PaymentError as exc:
        flash(request, str(exc), "error")
        return _back("/admin/payments?status=paid")
    flash(request, "Сумма списана, клиент помечен." + (f" Долг ${fmt(debt)} спишется из следующего пополнения."
                                                        if debt else ""))
    return _back("/admin/payments?status=fake")


@router.post("/payments/{payment_id}/reject", dependencies=[Depends(check_csrf)])
def payment_reject(payment_id: int, request: Request, reason: str = Form(""), admin=Depends(admin_user),
                   conn=Depends(get_conn), config: Config = Depends(get_config)):
    from . import payments
    ok = payments.reject(conn, config, payment_id, admin["id"], reason, who=f"админ (сайт: {admin['login']})")
    if ok:
        from .cashiers import settle
        settle(conn, config, payment_id, "❌ Отклонено — админ (сайт)")
    flash(request, "Заявка отклонена, клиент уведомлён." if ok else "Заявка уже обработана.", "ok" if ok else "error")
    return _back("/admin/payments")


# ── Ошибки ───────────────────────────────────────────────────


@router.get("/errors")
def errors(request: Request, admin=Depends(admin_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT o.*, u.login FROM orders o JOIN users u ON u.id = o.user_id "
        "WHERE o.status IN ('failed', 'attention') OR (o.status = 'processing' AND o.error IS NOT NULL) "
        "OR o.webhook_state = 'failed' ORDER BY (o.status = 'attention') DESC, o.id DESC LIMIT 200"
    ).fetchall()
    return render(request, "admin/errors.html", {"user": admin, "rows": rows})


# ── Бот-магазин проекта ──

@router.get("/shopbot")
def shopbot_page(request: Request, admin=Depends(admin_user), conn=Depends(get_conn),
                 config: Config = Depends(get_config)):
    games = conn.execute(
        "SELECT MIN(rowid) AS rid, category_name AS name FROM products WHERE active = 1 AND hidden = 0 "
        "AND kind IN ('topup', 'game_key', 'telegram_stars', 'telegram_premium', 'gift_card', 'steam_topup') "
        "GROUP BY category_id ORDER BY category_name").fetchall()
    last = db.get_setting(conn, "shop.last_broadcast")
    return render(request, "admin/shopbot.html", {
        "user": admin, "on": bool(config.shop_bot_token), "name": shopbot.bot_username(conn),
        "o": shopbot.overview(conn), "games": games, "last": json.loads(last) if last else None})


@router.post("/shopbot/broadcast", dependencies=[Depends(check_csrf)])
def shopbot_broadcast(request: Request, text: str = Form(""), admin=Depends(admin_user),
                      config: Config = Depends(get_config)):
    text = text.strip()
    if not config.shop_bot_token or not text:
        flash(request, "Напишите текст. Бот-магазин должен быть подключён.", "error")
        return RedirectResponse("/admin/shopbot", status_code=303)
    shopbot.broadcast(config, html.escape(text[:3500], quote=False))
    flash(request, "Рассылка пошла — по 20 сообщений в секунду. Итог появится на этой странице.")
    return RedirectResponse("/admin/shopbot", status_code=303)
