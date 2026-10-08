"""Конструктор ботов: админ вставляет токен бота и Telegram ID владельца — бот запускается сам.

Каждый бот — копия готового магазина из папки partner_bot/ (stars_bot_template) со своей
базой в data/bots/<id>/. Поставщик для него — сам Donatix через FazerCards-совместимый
API (/api/v2): цены уже с наценкой владельца, заказы списываются с его баланса в Donatix.
Для бота автоматически создаётся API-ключ владельца.

Процессы держит BotRunner — поток внутри сайта: запускает включённые боты, перезапускает
упавшие (с паузой), останавливает выключенные. Библиотеки ботов стоят в отдельном
окружении data/bots/venv — ставятся сами при первом запуске.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import accounts, db
from .config import ROOT, Config
from .security import seal, unseal

log = logging.getLogger(__name__)

TEMPLATE_DIR = ROOT.parent / "partner_bot"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
LOG_LIMIT = 2 * 1024 * 1024


class BotError(ValueError):
    pass


def bots_dir(config: Config) -> Path:
    return Path(config.db_path).parent / "bots"


def bot_dir(config: Config, bot_id: int) -> Path:
    return bots_dir(config) / str(bot_id)


# ── Данные ───────────────────────────────────────────────────


def check_token(token: str, transport: httpx.BaseTransport | None = None) -> str:
    """Проверить токен у Telegram (getMe). Возвращает @username бота."""
    token = token.strip()
    if not TOKEN_RE.match(token):
        raise BotError("Токен не похож на токен от @BotFather (вида 123456789:AAH…).")
    try:
        with httpx.Client(timeout=15, transport=transport) as c:
            data = c.get(f"https://api.telegram.org/bot{token}/getMe").json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BotError("Не удалось связаться с Telegram, попробуйте ещё раз.") from exc
    if not data.get("ok"):
        raise BotError("Telegram не принял токен — скопируйте его у @BotFather ещё раз.")
    username = str(data["result"].get("username") or "")
    # Этот бот уже где-то запущен? Тогда Telegram отвечает Conflict, и две программы
    # будут отвечать клиентам дважды. limit=1 без offset — чужие сообщения не забираем.
    try:
        with httpx.Client(timeout=15, transport=transport) as c:
            r = c.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"limit": 1, "timeout": 0})
        busy = r.status_code == 409
    except httpx.HTTPError:
        busy = False
    if busy:
        raise BotError(f"Бот @{username} уже запущен в другой программе (например, старый бот на сервере). "
                       "Остановите её или создайте для конструктора новый бот у @BotFather — иначе он будет "
                       "отвечать дважды.")
    return username


def parse_admin_ids(raw: str) -> str:
    ids = [p.strip() for p in raw.replace(";", ",").replace(" ", ",").split(",") if p.strip()]
    if not ids or not all(p.isdigit() for p in ids):
        raise BotError("Telegram ID — только цифры (узнать у @userinfobot). Несколько — через запятую.")
    return ",".join(ids)


def create(conn: sqlite3.Connection, config: Config, *, user_id: int, token: str, admin_ids: str,
           username: str) -> int:
    token, admins = token.strip(), parse_admin_ids(admin_ids)
    owner = accounts.get_user(conn, user_id)
    if owner is None:
        raise BotError("Клиент не найден.")
    for row in conn.execute("SELECT token_enc FROM bots"):
        if unseal(config.secret_key, row["token_enc"]) == token:
            raise BotError("Этот бот уже подключён.")
    from . import sitecfg
    limit = sitecfg.max_bots_total(conn)
    if limit and conn.execute("SELECT COUNT(*) FROM bots WHERE enabled = 1").fetchone()[0] >= limit:
        raise BotError(f"На сервере уже {limit} ботов — это предел. Напишите администратору.")
    with db.tx(conn):
        key = accounts.create_api_key(conn, user_id, f"Бот @{username}"[:64], config.secret_key)
        key_id = conn.execute("SELECT MAX(id) FROM api_keys WHERE user_id = ?", (user_id,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO bots (user_id, token_enc, username, admin_ids, api_key_id, key_enc, enabled, created_at, "
            "updated_at, active_since) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (user_id, seal(config.secret_key, token), username, admins, key_id, seal(config.secret_key, key),
             db.now(), db.now(), db.now()))
    return int(cur.lastrowid)


def set_enabled(conn: sqlite3.Connection, bot_id: int, enabled: bool, *, by_admin: bool = False) -> None:
    was = conn.execute("SELECT enabled FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.execute("UPDATE bots SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), db.now(), bot_id))
    if not enabled and by_admin:   # остановил админ — клиент сам обратно не включит
        conn.execute("UPDATE bots SET disabled_reason = 'admin' WHERE id = ?", (bot_id,))
    if enabled and was is not None and not was["enabled"]:  # перезапуск работающего бота отсчёт не сбрасывает
        from . import bot_watch
        bot_watch.mark_enabled(conn, bot_id)  # включили — отсчёт «без продаж» заново


def update_admins(conn: sqlite3.Connection, bot_id: int, admin_ids: str) -> None:
    conn.execute("UPDATE bots SET admin_ids = ?, updated_at = ? WHERE id = ?",
                 (parse_admin_ids(admin_ids), db.now(), bot_id))


def delete(conn: sqlite3.Connection, bot_id: int) -> None:
    """Удалить бота и отозвать его ключ. База бота остаётся на диске — на случай, если передумаете."""
    row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    if row is None:
        return
    with db.tx(conn):
        if row["api_key_id"]:
            accounts.revoke_api_key(conn, row["user_id"], row["api_key_id"])
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))


#: Сколько ботов может подключить один клиент в своём кабинете
MAX_PER_CLIENT = 3
MIN_ORDERS_DEFAULT = 5


# ── Кто может подключать ботов ───────────────────────────────
#
# Правила строгие, и одни для кабинета и для бота поддержки:
# 1) свой бот — только после N выполненных заказов на сайте (по умолчанию 5);
# 2) бот, отключённый алгоритмом за отсутствие продаж, клиент сам не включит, и нового
#    не подключит — даже удалив старого: запрет висит на клиенте, снимает его только админ.


def min_orders(conn: sqlite3.Connection) -> int:
    try:
        return max(0, int(db.get_setting(conn, "bots.min_orders") or MIN_ORDERS_DEFAULT))
    except ValueError:
        return MIN_ORDERS_DEFAULT


def completed_orders(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'completed'",
                        (user_id,)).fetchone()[0]


def blocked_for_inactivity(conn: sqlite3.Connection, user_id: int) -> bool:
    return bool(db.get_setting(conn, f"bots.block.{user_id}"))


def block_user(conn: sqlite3.Connection, user_id: int) -> None:
    db.set_setting(conn, f"bots.block.{user_id}", db.now())


def unblock_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (f"bots.block.{user_id}",))


BLOCKED_TEXT = ("Ваш бот был отключён автоматически: долго не было продаж. Включить бота или подключить "
                "нового может только администратор — напишите в поддержку.")


def eligibility(conn: sqlite3.Connection, user: sqlite3.Row) -> dict[str, Any]:
    """Можно ли клиенту подключить ещё бота, и если нет — почему (текст для человека)."""
    from . import sitecfg
    done, need = completed_orders(conn, user["id"]), min_orders(conn)
    have = conn.execute("SELECT COUNT(*) FROM bots WHERE user_id = ?", (user["id"],)).fetchone()[0]
    limit = sitecfg.max_bots(conn)
    reason = ""
    if not sitecfg.client_bots_enabled(conn):
        reason = "Конструктор ботов сейчас выключен."
    elif user["status"] != "active":
        reason = "Бот можно подключить после подтверждения аккаунта."
    elif blocked_for_inactivity(conn, user["id"]):
        reason = BLOCKED_TEXT
    elif done < need:
        reason = (f"Свой бот открывается после {need} выполненных заказов на сайте. "
                  f"У вас {done} из {need} — осталось {need - done}.")
    elif have >= limit:
        reason = f"Можно подключить до {limit} ботов. Удалите ненужного."
    return {"ok": not reason, "reason": reason, "done": done, "need": need, "have": have, "limit": limit,
            "blocked": blocked_for_inactivity(conn, user["id"])}


def can_enable(conn: sqlite3.Connection, bot_id: int) -> tuple[bool, str]:
    """Клиент сам включает бота. Отключённого за отсутствие продаж — нельзя."""
    row = conn.execute("SELECT user_id, disabled_reason FROM bots WHERE id = ?", (bot_id,)).fetchone()
    if row is None:
        return False, "Бот не найден."
    if row["disabled_reason"] == "inactive" or blocked_for_inactivity(conn, row["user_id"]):
        return False, BLOCKED_TEXT
    if row["disabled_reason"] == "admin":
        return False, "Бота остановил администратор — напишите в поддержку."
    return True, ""


def owned(conn: sqlite3.Connection, bot_id: int, user_id: int) -> bool:
    return conn.execute("SELECT 1 FROM bots WHERE id = ? AND user_id = ?", (bot_id, user_id)).fetchone() is not None


def listing(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT b.*, u.login, u.balance_micro FROM bots b JOIN users u ON u.id = b.user_id"
    args: tuple = ()
    if user_id is not None:
        sql, args = sql + " WHERE b.user_id = ?", (user_id,)
    rows = conn.execute(sql + " ORDER BY b.id DESC", args).fetchall()
    out = []
    shared = {} if RUNNER else _shared_state(conn).get("bots", {})
    for r in rows:
        st = RUNNER.state(r["id"]) if RUNNER else shared.get(str(r["id"]), {})
        out.append({**dict(r), **st})
    return out


def _shared_state(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        return json.loads(db.get_setting(conn, "bots.state") or "{}")
    except ValueError:
        return {}


def runner_alive(conn: sqlite3.Connection) -> bool:
    """Запускатель ботов работает — в этом процессе или в ведущем (отмечается каждые 10 с)."""
    if RUNNER is not None:
        return True
    return time.time() - float(_shared_state(conn).get("beat") or 0) < 90


def env_for(config: Config, row: sqlite3.Row, base_url: str) -> dict[str, str]:
    """Настройки процесса бота: всё через переменные окружения, .env в папке шаблона не нужен."""
    folder = bot_dir(config, row["id"])
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "HOME", "TZ", "SSL_CERT_FILE",
                                                         "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")}
    env.update({
        "BOT_TOKEN": unseal(config.secret_key, row["token_enc"]) or "",
        "ADMIN_IDS": row["admin_ids"],
        "FRAGMENT_MODE": "fazer",
        "FAZER_API_KEY": unseal(config.secret_key, row["key_enc"] or "") or "",
        "FAZER_BASE_URL": base_url,
        "DONATIX_URL": base_url,
        "DB_PATH": str(folder / "bot.sqlite3"),
        "USERBOT_SESSION": str(folder / "userbot.session"),
        # Контакт поддержки у каждого партнёра свой — он задаёт его в боте (/panel → 📞 Мой контакт)
        "SUPPORT_USERNAME": "",
        "BOT_USERNAME": row["username"] or "",
        "PYTHONUNBUFFERED": "1",
    })
    return env


# ── Процессы ─────────────────────────────────────────────────


class BotRunner:
    """Держит процессы ботов в соответствии с таблицей bots."""

    def __init__(self, config: Config, base_url: str, *, python: str | None = None, check_every: float = 10):
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.python = python  # для тестов; иначе — окружение data/bots/venv
        self.check_every = check_every
        self.procs: dict[int, subprocess.Popen] = {}
        self.meta: dict[int, dict[str, Any]] = {}
        self.venv_state = "ok" if python else "unknown"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()  # одна проверка за раз — иначе бот мог запуститься дважды
        self._thread: threading.Thread | None = None

    # окружение с aiogram и прочим — отдельно от сайта, чтобы версии не мешали друг другу
    def _venv_python(self) -> str | None:
        if self.python:
            return self.python
        venv = bots_dir(self.config) / "venv"
        py = venv / "bin" / "python"
        req = TEMPLATE_DIR / "requirements.txt"
        stamp = venv / ".req"
        want = req.read_text(encoding="utf-8") if req.exists() else ""
        if py.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == want:
            self.venv_state = "ok"
            return str(py)
        self.venv_state = "installing"
        try:
            bots_dir(self.config).mkdir(parents=True, exist_ok=True)
            if not py.exists():
                subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=300)
            subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req)], check=True,
                           capture_output=True, timeout=900)
            stamp.write_text(want, encoding="utf-8")
        except (subprocess.SubprocessError, OSError) as exc:
            err = getattr(exc, "stderr", b"") or b""
            self.venv_state = "error: " + (err.decode(errors="replace")[-300:] if err else str(exc))
            log.error("боты: не удалось поставить библиотеки: %s", self.venv_state)
            return None
        self.venv_state = "ok"
        return str(py)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="donatix-bots", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for bot_id in list(self.procs):
                self._kill(bot_id)

    def poke(self) -> None:
        """Проверить таблицу сейчас, не дожидаясь очередного круга."""
        threading.Thread(target=self.sync, daemon=True).start()

    def state(self, bot_id: int) -> dict[str, Any]:
        with self._lock:
            p = self.procs.get(bot_id)
            m = dict(self.meta.get(bot_id, {}))
        running = p is not None and p.poll() is None
        return {"running": running, "conflict": _token_conflict(self.config, bot_id),
                "pid": p.pid if running else None, "started_at": m.get("started_at"),
                "restarts": m.get("restarts", 0), "last_exit": m.get("last_exit"), "venv": self.venv_state}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception:  # noqa: BLE001 — поток не должен умирать
                log.exception("боты: проверка")
            self._stop.wait(self.check_every)

    def sync(self) -> None:
        with self._sync_lock:
            self._sync()
            try:
                self._publish()
            except Exception:  # noqa: BLE001
                log.debug("боты: не удалось сохранить состояние", exc_info=True)

    def _publish(self) -> None:
        """Состояние ботов — в базу: сайт работает в нескольких процессах, а ботами управляет один."""
        conn = db.connect(self.config.db_path)
        try:
            ids = [r[0] for r in conn.execute("SELECT id FROM bots")]
            snap = {str(i): self.state(i) for i in ids}
            db.set_setting(conn, "bots.state", json.dumps({"beat": time.time(), "bots": snap}, default=str))
        finally:
            conn.close()

    def _sync(self) -> None:
        conn = db.connect(self.config.db_path)
        try:
            rows = {r["id"]: r for r in conn.execute("SELECT * FROM bots")}
        finally:
            conn.close()
        with self._lock:
            # выключенные и удалённые — остановить
            for bot_id in list(self.procs):
                row = rows.get(bot_id)
                if row is None or not row["enabled"] or self.meta.get(bot_id, {}).get("version") != row["updated_at"]:
                    self._kill(bot_id)
            wanted = [r for r in rows.values() if r["enabled"] and self.procs.get(r["id"]) is None]
        if not wanted:
            return
        py = self._venv_python()
        if py is None:
            return
        with self._lock:
            for row in wanted:
                p = self.procs.get(row["id"])
                if p is not None and p.poll() is None:
                    continue  # уже работает — второй копии не будет
                m = self.meta.setdefault(row["id"], {"restarts": 0})
                if time.time() < m.get("retry_at", 0):
                    continue
                self._spawn(py, row)

    def _spawn(self, py: str, row: sqlite3.Row) -> None:
        folder = bot_dir(self.config, row["id"])
        folder.mkdir(parents=True, exist_ok=True)
        _kill_stale(folder / "bot.pid")
        logfile = folder / "bot.log"
        if logfile.exists() and logfile.stat().st_size > LOG_LIMIT:
            logfile.replace(folder / "bot.log.1")
        out = open(logfile, "ab")  # noqa: SIM115 — дескриптор живёт вместе с процессом
        out.write(f"\n=== {db.now()} запуск ===\n".encode())
        out.flush()
        env = env_for(self.config, row, self.base_url)
        p = subprocess.Popen([py, "-m", "app.main"], cwd=str(TEMPLATE_DIR), env=env,
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        self.procs[row["id"]] = p
        (folder / "bot.pid").write_text(str(p.pid))
        m = self.meta.setdefault(row["id"], {"restarts": 0})
        m.update(started_at=time.time(), version=row["updated_at"])
        threading.Thread(target=self._watch, args=(row["id"], p), daemon=True).start()
        log.info("боты: запущен @%s (pid %s)", row["username"], p.pid)

    def _watch(self, bot_id: int, p: subprocess.Popen) -> None:
        code = p.wait()
        with self._lock:
            if self.procs.get(bot_id) is not p:
                return  # остановили сами
            self.procs.pop(bot_id, None)
            m = self.meta.setdefault(bot_id, {"restarts": 0})
            m["restarts"] = m.get("restarts", 0) + 1
            m["last_exit"] = code
            # чем чаще падает, тем дольше ждём (до 5 минут), чтобы не долбить Telegram
            quick = time.time() - m.get("started_at", 0) < 60
            m["retry_at"] = time.time() + (min(300, 10 * 2 ** min(m["restarts"], 5)) if quick else 5)
        log.warning("боты: бот %s завершился с кодом %s", bot_id, code)
        self._alert_crash(bot_id, code, m)

    def _alert_crash(self, bot_id: int, code: int, meta: dict) -> None:
        """Бот падает снова и снова — сказать админу (не чаще раза в час на бота)."""
        if meta.get("restarts", 0) < 3 or time.time() - meta.get("alerted_at", 0) < 3600:
            return
        meta["alerted_at"] = time.time()
        try:
            conn = db.connect(self.config.db_path)
            try:
                row = conn.execute("SELECT b.username, u.login FROM bots b JOIN users u ON u.id = b.user_id "
                                   "WHERE b.id = ?", (bot_id,)).fetchone()
            finally:
                conn.close()
            from .worker import notify_admin
            name = f"@{row['username']} (клиент {row['login']})" if row else f"#{bot_id}"
            notify_admin(self.config, f"🔴 Бот {name} падает: код {code}, перезапусков {meta['restarts']}. "
                                      f"Лог: {self.config.base_url}/admin/bots/{bot_id}/log")
        except Exception:  # noqa: BLE001 — оповещение не должно ронять наблюдателя
            log.exception("боты: не удалось оповестить о падении")

    def _kill(self, bot_id: int) -> None:
        p = self.procs.pop(bot_id, None)
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def _kill_stale(pidfile: Path) -> None:
    """Копия бота от прошлого запуска сайта ещё жива — остановить, иначе ответы придут дважды."""
    try:
        pid = int(pidfile.read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return
    if b"app.main" not in cmd:
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _token_conflict(config: Config, bot_id: int) -> bool:
    """Telegram пишет Conflict, когда тот же токен опрашивает ещё одна программа."""
    tail = log_tail(config, bot_id, 40)
    return "Conflict" in tail and "getUpdates" in tail


RUNNER: BotRunner | None = None


def log_tail(config: Config, bot_id: int, lines: int = 80) -> str:
    f = bot_dir(config, bot_id) / "bot.log"
    if not f.exists():
        return ""
    data = f.read_bytes()[-40_000:].decode(errors="replace")
    text = "\n".join(data.splitlines()[-lines:])
    # токены и ключи в логах не показываем
    text = re.sub(r"\d{6,12}:[A-Za-z0-9_-]{30,}", "<токен скрыт>", text)
    return re.sub(r"dx_live_[A-Za-z0-9_-]+", "dx_live_<скрыт>", text)
