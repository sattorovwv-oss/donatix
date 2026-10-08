from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Store:
    """Extension-owned state; never adds tables to the site's database."""

    def __init__(self, path, secret):
        self.path = Path(path).resolve()
        self.key = hashlib.sha256(b"donatix-android-extension-v1\0" + secret.encode()).digest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS flows (
              id TEXT PRIMARY KEY, challenge TEXT NOT NULL, expires INTEGER NOT NULL, user_id INTEGER);
            CREATE TABLE IF NOT EXISTS devices (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, sid TEXT NOT NULL,
              token TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, binding TEXT NOT NULL,
              after_id INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS devices_owner ON devices(user_id);
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
              notification_id INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              next_try REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued',
              UNIQUE(device_id, notification_id));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def transaction(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def encrypt(self, token):
        nonce = os.urandom(12)
        return base64.b64encode(nonce + AESGCM(self.key).encrypt(nonce, token.encode(), b"fcm-v1")).decode()

    def decrypt(self, token):
        raw = base64.b64decode(token, validate=True)
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], b"fcm-v1").decode()


@contextmanager
def site_readonly(path):
    """Bounded read-only access, no PRAGMA journal_mode or site write lock."""
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    try:
        yield c
    finally:
        c.close()
