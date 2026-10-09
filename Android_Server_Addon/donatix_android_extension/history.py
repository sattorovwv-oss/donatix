"""Read-only JSON history using the website's existing filters and queries."""
from donatix import orders, web
from donatix.money import fmt
from fastapi.responses import JSONResponse


def history(request, conn, user, *, page=1, status="", q="", period="",
            date_from="", date_to=""):
    # Reuse the existing implementation, including timezone, attention orders,
    # search and 30-item pages. Never scrape translated HTML or create orders.
    response = web.panel_orders(request, status=status, q=q, page=page,
                                period=period, date_from=date_from,
                                date_to=date_to, user=user, conn=conn)
    ctx = response.context
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
