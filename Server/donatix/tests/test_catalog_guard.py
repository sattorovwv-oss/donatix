from decimal import Decimal

from donatix import catalog
from donatix.suppliers.base import ProductData, SupplierUnavailable
from donatix.suppliers.mock import MockSupplier, demo_catalog
from donatix.suppliers.multi import MultiSupplier


class Extra:
    name, id_prefix = "Vendoria", "vd-"

    def __init__(self):
        self.down = False

    def fetch_catalog(self):
        if self.down:
            raise SupplierUnavailable("timeout")
        yield ProductData(id="vd-1-5", kind="topup", category_id="vd_1", category_name="Standoff 2",
                          name="100 Gold", base_price=Decimal("1"), fields=[{"key": "id", "label": "ID"}])


def _active(conn, like):
    return conn.execute("SELECT COUNT(*) FROM products WHERE active = 1 AND id LIKE ?", (like,)).fetchone()[0]


def test_extra_supplier_down_keeps_its_products(conn):
    extra = Extra()
    multi = MultiSupplier(MockSupplier(), [extra])
    catalog.sync_catalog(conn, multi)
    assert _active(conn, "vd-%") == 1
    extra.down = True                                   # Vendoria не ответила
    catalog.sync_catalog(conn, multi)
    assert _active(conn, "vd-%") == 1                   # Standoff 2 не выключился


def test_half_catalog_missing_is_not_disabled(conn):
    full = demo_catalog()
    for i in range(60):                                  # каталог побольше, чтобы сработала защита
        full.append(ProductData(id=f"x-{i}", kind="topup", category_id="x", category_name="Free Fire",
                                name=f"{i}", base_price=Decimal("1"), fields=[{"key": "id", "label": "ID"}]))
    catalog.sync_catalog(conn, MockSupplier(catalog=full))
    before = conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0]
    catalog.sync_catalog(conn, MockSupplier(catalog=full[:10]))   # поставщик отдал кусок — сбой
    assert conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0] == before
    catalog.sync_catalog(conn, MockSupplier(catalog=full[:-3]))   # пропало 3 товара — это нормально
    assert conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0] == before - 3
