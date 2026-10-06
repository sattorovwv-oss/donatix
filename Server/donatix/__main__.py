"""Команды:

    python -m donatix serve [--host 0.0.0.0] [--port 8000]   запустить сайт и API
    python -m donatix sync                                   обновить каталог у поставщика
    python -m donatix create-admin EMAIL                     создать админа (пароль спросит)
    python -m donatix check                                  проверить ключ поставщика и баланс
    python -m donatix prices                                 цены поставщика и ваши цены с наценкой
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="donatix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sync = sub.add_parser("sync")
    sync.add_argument("--provider", default="", help="загрузить только этого доп. поставщика (CoinDrop, Vendoria)")
    sub.add_parser("check")
    sub.add_parser("prices")
    sub.add_parser("inspect")
    admin = sub.add_parser("create-admin")
    admin.add_argument("email")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore"):   # в адресах Telegram API — токены ботов, в журнал им нельзя
        logging.getLogger(name).setLevel(logging.WARNING)

    from . import accounts, catalog, db
    from .config import Config
    from .suppliers import SupplierError, make_supplier

    config = Config.from_env()

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app

        # Несколько процессов — сайт работает на всех ядрах. По умолчанию: ядер − 1, от 1 до 4.
        cores = os.cpu_count() or 1
        raw = os.environ.get("DONATIX_WEB_WORKERS", "").strip()
        workers = int(raw) if raw.isdigit() and int(raw) > 0 else max(1, min(4, cores - 1))
        if workers > 1:
            logging.getLogger("donatix").info("сайт: %s процесса(ов)", workers)
            uvicorn.run("donatix.app:factory", factory=True, workers=workers, host=args.host, port=args.port,
                        proxy_headers=True, forwarded_allow_ips="127.0.0.1")
        else:
            uvicorn.run(create_app(config), host=args.host, port=args.port, proxy_headers=True,
                        forwarded_allow_ips="127.0.0.1")
        return 0

    db.init(config.db_path)
    conn = db.connect(config.db_path)
    try:
        if args.cmd == "sync":
            import time as _t
            catalog.SYNC_LOCK.bind(config)   # не столкнуться с фоновой загрузкой сайта (тот же замок)
            # Одного поставщика не ждём: если идёт полная загрузка сайта, он загрузится в ней сам
            if not catalog.SYNC_LOCK.acquire(timeout=0 if args.provider else 900):
                if args.provider:
                    print(f"Сейчас сайт сам загружает весь каталог — {args.provider} загрузится в нём "
                          "автоматически (в самом конце). Ждать не нужно.", flush=True)
                    return 0
                print("Каталог уже загружается на сайте — подождите пару минут и повторите.")
                return 1
            _last = [0.0]

            def _progress(n: int, category: str) -> None:
                if _t.monotonic() - _last[0] > 2:
                    _last[0] = _t.monotonic()
                    print(f"  {n} товаров… сейчас: {category}", flush=True)

            try:
                supplier = make_supplier(config)
                if args.provider:
                    extra = getattr(supplier, "get_extra", lambda _n: None)(args.provider)
                    if extra is None:
                        print(f"Поставщик {args.provider} не подключён (нет ключа в .env).", flush=True)
                        return 1
                    print(f"Поставщик: {extra.name}. Загружаю только его каталог…", flush=True)
                    result = catalog.sync_catalog(conn, extra, _progress, id_prefix=extra.id_prefix)
                else:
                    print(f"Поставщик: {supplier.name}. Загружаю весь каталог…", flush=True)
                    result = catalog.sync_catalog(conn, supplier, _progress)
            finally:
                catalog.SYNC_LOCK.release()
            vd = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1 AND id LIKE 'vd-%'").fetchone()[0]
            if vd:
                print(f"Vendoria: {vd} активных товаров.", flush=True)
            cd = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1 AND id LIKE 'cd-%'").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0]
            print(f"Готово и сохранено в базу: всего {total} активных товаров "
                  f"(из них CoinDrop {cd}); за этот раз загружено {result['products']}, "
                  f"выключено пропавших {result['disabled']}.", flush=True)
        elif args.cmd == "check":
            supplier = make_supplier(config)
            try:
                print(f"Поставщик: {supplier.name}. Баланс: ${supplier.balance()}")
            except SupplierError as exc:
                print(f"Ошибка: {exc}")
                return 1
        elif args.cmd == "prices":
            from decimal import Decimal

            from .money import apply_markup, fmt_unit
            supplier = make_supplier(config)
            result = catalog.sync_catalog(conn, supplier)
            print(f"Каталог загружен: {result['products']} позиций. "
                  f"Наценка по умолчанию: +{config.markups['bronze']}%\n")
            print(f"{'Товар':<46} {'У поставщика':>14} {'Ваша цена':>12} {'Прибыль':>10}")
            for p in catalog.list_products(conn, include_hidden=True, limit=5000):
                m = config.kind_markups.get(p["kind"], config.markups["bronze"])
                base = Decimal(p["base_price"])
                mine = apply_markup(base, m)
                name = f"{p['category_name']} — {p['name']}"[:45]
                cols = ["$" + fmt_unit(v) for v in (base, mine, mine - base)]
                print(f"{name:<46} {cols[0]:>14} {cols[1]:>12} {cols[2]:>10}")
            print("\nSteam-гифты: цена берётся у поставщика на каждый заказ + наценка.")
        elif args.cmd == "inspect":
            # Показать, какие поля отдаёт поставщик у категорий и пакетов (для регионов и картинок)
            import json

            supplier = make_supplier(config)
            if not hasattr(supplier, "_catalog_get"):
                print("Работает только с DONATIX_SUPPLIER=fazer.")
                return 1
            for path, sub_path, key in (("/topups", "/topups/offers", "offers"),
                                        ("/giftcards", "/giftcards/cards", "offers"),
                                        ("/gamekeys", "/gamekeys/keys", "keys")):
                data = supplier._catalog_get(path, limit=3, include_ui=1) or {}
                cats = data.get("items", [])
                print(f"== {path}: пример категории\n{json.dumps(cats[:1], ensure_ascii=False, indent=1)[:1500]}")
                if cats:
                    ident = {"game_id": cats[0]["game_id"]} if "game_id" in cats[0] else \
                        {"category_id": cats[0]["category_id"]}
                    detail = supplier._catalog_get(sub_path, include_ui=1, **ident) or {}
                    offers = detail.get(key, [])
                    head = {k: v for k, v in detail.items() if k != key}
                    print(f"== {sub_path}: поля ответа\n{json.dumps(head, ensure_ascii=False, indent=1)[:1500]}")
                    sample = json.dumps(offers[:2], ensure_ascii=False, indent=1)[:1500]
                    print(f"== пакеты (2 из {len(offers)})\n{sample}")
            print("\nРегионы и картинки в каталоге:")
            catalog.sync_catalog(conn, supplier)
            for row in conn.execute("SELECT kind, category_name, COUNT(*) n, GROUP_CONCAT(DISTINCT region) r, "
                                    "MAX(image_url) img FROM products WHERE kind IN ('topup', 'gift_card', 'game_key') "
                                    "GROUP BY kind, category_id ORDER BY kind, category_name LIMIT 40"):
                print(f"  {row['category_name'][:30]:<30} пакетов {row['n']:>3}  регионы: {row['r'] or '—':<20} "
                      f"картинка: {'есть' if row['img'] else 'нет'}")
        elif args.cmd == "create-admin":
            password = getpass.getpass("Пароль (мин. 8 символов): ")
            login = args.email.split("@")[0][:32]
            accounts.create_user(conn, email=args.email, login=login, password=password,
                                 role="admin", status="active")
            print(f"Админ {args.email} создан. Вход: /login")
    except accounts.AccountError as exc:
        print(f"Ошибка: {exc}")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
