"""Фоновая загрузка каталога и картинок: не блокирует запрос, картинки подменяются на свои."""

import time

import httpx
from conftest import web_login
from fastapi.testclient import TestClient

from donatix import catalog_job


def _wait(timeout=10):
    end = time.time() + timeout
    while catalog_job.status().get("running") and time.time() < end:
        time.sleep(0.05)
    return catalog_job.status()


def test_sync_button_runs_in_background(app, conn):
    admin = TestClient(app)
    token = web_login(admin, "admin@example.com", "adminpass123")
    r = admin.post("/admin/catalog-sync/start", data={"csrf": token, "mode": "catalog"})
    assert r.status_code == 200 and "Загрузка каталога" in r.text
    s = _wait()
    assert s["stage"] == "done" and s["result"]["products"] > 5
    assert conn.execute("SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0] > 5
    assert admin.get("/admin/catalog-sync/status").json()["stage"] == "done"
    # старая кнопка на сводке тоже запускает фон и ведёт на страницу прогресса
    r = admin.post("/admin/sync", data={"csrf": token})
    assert "Ход загрузки" in r.text
    _wait()


def test_image_download_and_local_url(config, tmp_path):
    catalog_job.load_image_index(config)
    folder = catalog_job.images_dir(config)

    def handler(req):
        if req.url.path.endswith("a.png"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG....")
        if req.url.path.endswith("x.svg"):
            return httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert catalog_job._download(client, folder, "https://cdn.example/a.png") == "ok"
    assert catalog_job._download(client, folder, "https://cdn.example/a.png") == "skip"
    assert catalog_job._download(client, folder, "https://cdn.example/x.svg").startswith("не картинка")
    assert catalog_job._download(client, folder, "https://cdn.example/none.jpg") == "HTTP 404"
    local = catalog_job.local_url("https://cdn.example/a.png")
    assert local.startswith("/media/") and local.endswith(".png")
    assert catalog_job.local_url("https://cdn.example/other.png") == "https://cdn.example/other.png"
