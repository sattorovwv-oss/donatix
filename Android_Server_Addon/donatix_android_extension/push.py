from __future__ import annotations

import fcntl
import logging
import threading
import time

from .fcm import InvalidToken
from .store import site_readonly

LOG = logging.getLogger("donatix.android")


class Worker:
    def __init__(self, config, store, sender):
        self.config, self.store, self.sender = config, store, sender
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.sender.configured:
            # Establish the first cursor before the site begins accepting requests.
            try:
                self.scan()
            except Exception:
                LOG.warning("Android notification initialization will retry")
            self.thread = threading.Thread(target=self.run, name="donatix-android-push", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=22)

    def run(self):
        # Multiple uvicorn workers share one extension database and one sender leader.
        with open(str(self.store.path) + ".worker.lock", "a") as lock:
            leader = False
            while not self.stop_event.is_set():
                try:
                    if not leader:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        leader = True
                    self.scan()
                    self.dispatch()
                except BlockingIOError:
                    pass
                except Exception:
                    # Push faults must never escape into order handling or the site lifespan.
                    LOG.warning("Android notification worker will retry", exc_info=False)
                self.stop_event.wait(1 if leader else 5)

    def scan(self):
        with self.store.connect() as c:
            state = c.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
        with site_readonly(self.config.db_path) as site:
            if state is None:
                latest = site.execute("SELECT COALESCE(MAX(id),0) FROM notifications").fetchone()[0]
                rows = []
            else:
                rows = site.execute("SELECT id,user_id FROM notifications WHERE id>? ORDER BY id LIMIT 200",
                                    (int(state[0]),)).fetchall()
                latest = rows[-1]["id"] if rows else int(state[0])
        with self.store.transaction() as c:
            for row in rows:
                c.execute("INSERT OR IGNORE INTO outbox(device_id,notification_id) "
                          "SELECT id,? FROM devices WHERE user_id=? AND after_id<?",
                          (row["id"], row["user_id"], row["id"]))
            c.execute("INSERT INTO meta VALUES('cursor',?) ON CONFLICT(key) DO UPDATE SET "
                      "value=CAST(MAX(CAST(meta.value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)", (str(latest),))

    def dispatch(self, now=None):
        now = time.time() if now is None else now
        with self.store.connect() as c:
            rows = c.execute("SELECT o.id,o.notification_id,o.attempts,d.* FROM outbox o JOIN devices d ON d.id=o.device_id "
                             "WHERE o.next_try<=? ORDER BY o.id LIMIT 40", (now,)).fetchall()
        for row in rows:
            if self.stop_event.is_set():
                return
            oid = row["id"]  # The first id column is the outbox id.
            with self.store.transaction() as c:
                if c.execute("UPDATE outbox SET next_try=?,attempts=attempts+1,state='sending' WHERE id=? AND next_try<=?",
                             (now + 90, oid, now)).rowcount != 1:
                    continue
            with site_readonly(self.config.db_path) as site:
                live = site.execute("SELECT 1 FROM logins l JOIN users u ON u.id=l.user_id "
                                    "WHERE l.sid=? AND u.id=? AND l.ended_at IS NULL AND u.status<>'blocked'",
                                    (row["sid"], row["user_id"])).fetchone()
                event = site.execute("SELECT text,link FROM notifications WHERE id=? AND user_id=?",
                                     (row["notification_id"], row["user_id"])).fetchone()
            with self.store.connect() as c:
                current = c.execute("SELECT id FROM devices WHERE token_hash=? AND sid=? AND binding=?",
                                    (row["token_hash"], row["sid"], row["binding"])).fetchone()
            if not live or not event or not current:
                with self.store.connect() as c:
                    c.execute("DELETE FROM outbox WHERE id=?", (oid,))
                    if not live:
                        c.execute("DELETE FROM devices WHERE token_hash=? AND binding=?", (row["token_hash"], row["binding"]))
                continue
            try:
                self.sender(self.store.decrypt(row["token"]), {
                    "user_id": row["user_id"], "notification_id": row["notification_id"],
                    "binding": row["binding"], "title": self.config.site_name,
                    "body": event["text"][:500], "link": event["link"] or "/panel/notifications"})
            except InvalidToken:
                with self.store.connect() as c:
                    c.execute("DELETE FROM devices WHERE token_hash=? AND binding=?", (row["token_hash"], row["binding"]))
            except Exception:
                with self.store.connect() as c:
                    if row["attempts"] >= 7:
                        c.execute("DELETE FROM outbox WHERE id=?", (oid,))
                    else:
                        c.execute("UPDATE outbox SET state='queued',next_try=? WHERE id=?",
                                  (now + min(600, 5 * 2 ** row["attempts"]), oid))
                # Do not expose a token, private key or notification content in logs.
                LOG.warning("An Android notification will be retried")
            else:
                with self.store.connect() as c:
                    c.execute("DELETE FROM outbox WHERE id=?", (oid,))
