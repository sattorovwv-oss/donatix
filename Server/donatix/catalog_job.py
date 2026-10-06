"""Обновление каталога и скачивание картинок — в фоне, с прогрессом для админки.

Каталог FazerCards большой: сотни запросов с паузами между ними. Если делать это прямо
в запросе «Обновить каталог», браузер и nginx не дождутся ответа. Поэтому кнопка только
запускает задачу, а страница раз в пару секунд спрашивает, как дела."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from . import catalog, db
from .config import Config
from .suppliers import Supplier

log = logging.getLogger(__name__)

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False}
_images: dict[str, str] = {}  # sha1(url)[:16] → имя файла в папке картинок
_images_dir: Path | None = None


# ── Картинки на своём сервере ────────────────────────────────


def images_dir(config: Config) -> Path:
    return Path(config.db_path).parent / "images"


def load_image_index(config: Config) -> None:
    """При запуске: какие картинки уже скачаны."""
    global _images_dir
    _images_dir = images_dir(config)
    _images_dir.mkdir(parents=True, exist_ok=True)
    _images.clear()
    for f in _images_dir.iterdir():
        if f.is_file() and "." in f.name:
            _images[f.name.split(".")[0]] = f.name


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def local_url(url: str | None) -> str | None:
    """Шаблоны показывают свою копию картинки, если она уже скачана, иначе — ссылку поставщика."""
    if not url:
        return url
    name = _images.get(_key(url))
    return f"/media/{name}" if name else url


def _download(client: httpx.Client, folder: Path, url: str) -> str:
    """'ok' | 'skip' | текст ошибки."""
    if _key(url) in _images:
        return "skip"
    if not url.startswith(("https://", "http://")):
        return "не http-ссылка"
    try:
        with client.stream("GET", url) as r:
            if r.status_code != 200:
                return f"HTTP {r.status_code}"
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = _EXT.get(ctype)
            if not ext:  # svg и прочее не берём: в svg может быть скрипт
                return f"не картинка ({ctype or '?'})"
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    return "больше 5 МБ"
    except httpx.HTTPError as exc:
        return f"сеть: {exc.__class__.__name__}"
    name = f"{_key(url)}.{ext}"
    tmp = folder / (name + ".part")
    tmp.write_bytes(data)
    tmp.replace(folder / name)
    _images[_key(url)] = name
    return "ok"


# ── Фоновая задача ──────────────────────────────────────────


_shared = {"path": None, "at": 0.0}
STALE = 60   # ведущий не отмечался минуту — значит, задача не идёт (процесс перезапустился)


def _persist(force: bool = False) -> None:
    """Прогресс — в базу (не чаще раза в 1,5 с): страницу прогресса может открыть другой процесс сайта."""
    import json
    now = time.monotonic()
    if not _shared["path"] or (not force and now - _shared["at"] < 1.5):
        return
    _shared["at"] = now
    with _lock:
        snap = {k: v for k, v in _state.items() if k != "log"}
        snap["log"] = list(_state.get("log", []))[-40:]
    snap["beat"] = time.time()
    try:
        c = db.connect(_shared["path"])
        try:
            db.set_setting(c, "catalog_job.state", json.dumps(snap, ensure_ascii=False, default=str))
        finally:
            c.close()
    except Exception:  # noqa: BLE001 — прогресс не должен ломать загрузку
        log.debug("каталог: не удалось сохранить прогресс", exc_info=True)


def _shared_state(conn: Any) -> dict[str, Any] | None:
    import json
    raw = db.get_setting(conn, "catalog_job.state") if conn is not None else None
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except ValueError:
        return None
    if snap.get("running") and time.time() - float(snap.get("beat") or 0) > STALE:
        snap["running"] = False
    return snap


def status(conn: Any = None) -> dict[str, Any]:
    with _lock:
        local_running = bool(_state.get("running"))
    shared = None if local_running else _shared_state(conn)
    with _lock:
        s = dict(shared) if shared and (shared.get("running") or not _state.get("started_at")) else dict(_state)
        s["log"] = list(s.get("log", []))[-40:]
    if s.get("started_at"):
        s["seconds"] = int((s.get("finished_at") or time.time()) - s["started_at"])
    s["images_local"] = len(_images)
    return s


def _say(line: str) -> None:
    with _lock:
        _state.setdefault("log", []).append(time.strftime("%H:%M:%S ") + line)
        _state["log"] = _state["log"][-200:]
    log.info("каталог: %s", line)
    _persist()


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)
    _persist(force="running" in kw or "stage" in kw)


def start(config: Config, supplier: Supplier, *, sync: bool = True, images: bool = True,
          provider: str = "") -> bool:
    """False — задача уже идёт (в этом или другом процессе сайта)."""
    _shared["path"] = config.db_path
    c = db.connect(config.db_path)
    try:
        other = _shared_state(c)
    finally:
        c.close()
    if other and other.get("running") and not _state.get("running"):
        return False
    with _lock:
        if _state.get("running"):
            return False
        _state.clear()
        _state.update(running=True, stage="start", started_at=time.time(), finished_at=None, products=0,
                      category="", images_total=0, images_done=0, images_ok=0, images_failed=0,
                      error=None, result=None, log=[], sync=sync, images=images, provider=provider)
    _persist(force=True)
    threading.Thread(target=_run, args=(config, supplier, sync, images, provider),
                     name="donatix-catalog", daemon=True).start()
    return True


def _run(config: Config, supplier: Supplier, sync: bool, images: bool, provider: str = "") -> None:
    conn = db.connect(config.db_path)
    id_prefix = ""
    if provider and hasattr(supplier, "get_extra"):
        extra = supplier.get_extra(provider)
        if extra is None:
            _set(stage="error", error=f"поставщик {provider} не подключён", running=False,
                 finished_at=time.time())
            conn.close()
            return
        supplier = extra
        id_prefix = getattr(extra, "id_prefix", "")
    try:
        if sync:
            _set(stage="catalog")
            _say(f"Загружаю каталог{' — ' + provider if provider else ''}…")
            if not catalog.SYNC_LOCK.acquire(timeout=600):
                raise RuntimeError("каталог уже обновляется фоновым процессом — попробуйте через пару минут")
            try:
                last = [0.0]

                def progress(n: int, category: str) -> None:
                    _set(products=n, category=category)
                    if time.monotonic() - last[0] > 3:
                        last[0] = time.monotonic()
                        _say(f"{n} товаров… сейчас: {category}")

                result = catalog.sync_catalog(conn, supplier, progress, id_prefix=id_prefix)
            finally:
                catalog.SYNC_LOCK.release()
            _set(result=result)
            _say(f"Каталог обновлён: {result['products']} товаров, скрыто пропавших {result['disabled']}.")
        if images:
            _download_all(config, conn)
        _set(stage="done")
        _say("Готово.")
    except Exception as exc:  # показываем в админке, сайт продолжает работать
        log.exception("обновление каталога")
        _set(stage="error", error=str(exc) or exc.__class__.__name__)
        _say(f"Ошибка: {exc}")
    finally:
        _set(running=False, finished_at=time.time())
        db.set_setting(conn, "catalog_job_last", f"{_state.get('stage')} {db.now()}")
        conn.close()


def _download_all(config: Config, conn) -> None:
    folder = images_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    urls = [r[0] for r in conn.execute(
        "SELECT DISTINCT image_url FROM products WHERE active = 1 AND image_url LIKE 'http%'")]
    _set(stage="images", images_total=len(urls))
    if not urls:
        _say("Ссылок на картинки в ответе поставщика не найдено. Если на сайте FazerCards картинки есть — "
             "пришлите разработчику вывод команды `python -m donatix inspect`, подключим их поле.")
        return
    _say(f"Скачиваю картинки: {len(urls)} шт.")
    headers = {"User-Agent": "Donatix/1.0 (+catalog images)"}
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client, \
            ThreadPoolExecutor(max_workers=4) as pool:
        for url, res in zip(urls, pool.map(lambda u: _download(client, folder, u), urls), strict=True):
            with _lock:
                _state["images_done"] += 1
                if res in ("ok", "skip"):
                    _state["images_ok"] += 1
                else:
                    _state["images_failed"] += 1
            if res not in ("ok", "skip"):
                _say(f"Не скачалась: {url[:80]} — {res}")
    s = status()
    _say(f"Картинки: сохранено {s['images_ok']}, не удалось {s['images_failed']}.")
