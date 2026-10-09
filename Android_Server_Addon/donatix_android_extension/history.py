"""Read-only JSON history using the website's existing filters and queries."""
from donatix import orders, web
from donatix.money import fmt
from fastapi.responses import JSONResponse
from types import FunctionType


def _context(request, conn, user, **filters):
    # Reuse the audited website query without rendering its HTML. A separate
    # globals dictionary keeps the site's renderer untouched, including while
    # browser requests run concurrently with this JSON endpoint.
    handler = FunctionType(web.panel_orders.__code__,
                           {**web.panel_orders.__globals__, "render": lambda _r, _t, context: context},
                           name="android_orders_context", argdefs=web.panel_orders.__defaults__,
                           closure=web.panel_orders.__closure__)
    handler.__kwdefaults__ = web.panel_orders.__kwdefaults__
    return handler(request, user=user, conn=conn, **filters)


def history(request, conn, user, *, page=1, status="", q="", period="",
            date_from="", date_to=""):
    # Reuse the existing implementation, including timezone, attention orders,
    # search and 30-item pages. Never scrape translated HTML or create orders.
    ctx = _context(request, conn, user, status=status, q=q,
                   page=min(max(page, 1), 100_000), period=period,
                   date_from=date_from, date_to=date_to)
    rows, totals = ctx["orders"], ctx["totals"]
    result = {
        "ok": True,
        "items": [{**orders.public_view(row), "image_url": row.get("image_url")}
                  for row in rows],
        "total": ctx["total"], "page": ctx["page"], "limit": 30,
        "period": ctx["pr"].label,
        "totals": {"done": totals["done"], "failed": totals["failed"],
                   "spent": fmt(totals["spent"])},
    }
    return JSONResponse(result, headers={"Cache-Control": "no-store", "Vary": "Cookie"})
