"""Persistent FCM outbox. Explicit service-account file; never ambient credentials."""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

from . import db
from .security import seal, unseal


def init(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS native_push_devices (
        device_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        sid TEXT NOT NULL, token_enc TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS native_push_owner ON native_push_devices(user_id);
    CREATE TABLE IF NOT EXISTS native_push_outbox (
        id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, notification_id INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_try REAL NOT NULL DEFAULT 0, claim TEXT, error TEXT,
        UNIQUE(device_id, notification_id));
    ''')
    # Separate metadata keeps the six-column v3 table compatible with a code rollback.
    conn.execute('CREATE TABLE IF NOT EXISTS native_push_platforms (device_id TEXT PRIMARY KEY, platform TEXT NOT NULL)')
    conn.execute('DELETE FROM native_push_platforms WHERE device_id NOT IN (SELECT device_id FROM native_push_devices)')


def binding(config, sid, user_id, device_id):
    import hashlib, hmac
    return hmac.new(config.secret_key.encode(),
                    f'ios-push:{sid}:{user_id}:{device_id}'.encode(), hashlib.sha256).hexdigest()


def enabled() -> bool:
    path = os.environ.get('DONATIX_FIREBASE_CREDENTIALS', '')
    return bool(path and Path(path).is_file())


def register(conn, config, user_id: int, sid: str, device_id: str, token: str, platform='android'):
    import hashlib
    if platform not in ('android', 'ios'):
        raise ValueError('Unknown push platform')
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db.tx(conn):
        # Registration after logout must not resurrect a revoked device.
        if not conn.execute('SELECT 1 FROM logins WHERE user_id=? AND sid=? AND ended_at IS NULL',
                            (user_id, sid)).fetchone():
            raise ValueError('Сессия истекла.')
        conn.execute('DELETE FROM native_push_outbox WHERE device_id IN '
                     '(SELECT device_id FROM native_push_devices WHERE token_hash=? AND device_id<>?)', (token_hash, device_id))
        conn.execute('DELETE FROM native_push_devices WHERE token_hash=? AND device_id<>?', (token_hash, device_id))
        conn.execute('DELETE FROM native_push_platforms WHERE device_id NOT IN (SELECT device_id FROM native_push_devices)')
        # Old account messages cannot be sent to a device whose owner changes.
        previous = conn.execute("SELECT d.user_id,d.sid,COALESCE(p.platform,'android') AS platform FROM native_push_devices d "
                                'LEFT JOIN native_push_platforms p ON p.device_id=d.device_id WHERE d.device_id=?', (device_id,)).fetchone()
        if previous and (previous['user_id'], previous['sid'], previous['platform']) != (user_id, sid, platform):
            conn.execute('DELETE FROM native_push_outbox WHERE device_id=?', (device_id,))
        conn.execute('INSERT INTO native_push_devices (device_id,user_id,sid,token_enc,token_hash,updated_at) VALUES (?,?,?,?,?,?) '
                     'ON CONFLICT(device_id) DO UPDATE SET user_id=excluded.user_id,sid=excluded.sid,'
                     'token_enc=excluded.token_enc,token_hash=excluded.token_hash,updated_at=excluded.updated_at',
                     (device_id, user_id, sid, seal(config.secret_key, token), token_hash, db.now()))
        conn.execute('INSERT INTO native_push_platforms VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET platform=excluded.platform', (device_id, platform))


def unregister(conn, user_id: int, device_id: str):
    with db.tx(conn):
        if conn.execute('DELETE FROM native_push_devices WHERE device_id=? AND user_id=?',
                        (device_id, user_id)).rowcount:
            conn.execute('DELETE FROM native_push_outbox WHERE device_id=?', (device_id,))
            conn.execute('DELETE FROM native_push_platforms WHERE device_id=?', (device_id,))


def enqueue(conn, user_id: int, notification_id: int):
    conn.execute("INSERT OR IGNORE INTO native_push_outbox (device_id,notification_id) "
                 "SELECT d.device_id,? FROM native_push_devices d JOIN logins l ON l.sid=d.sid "
                 "WHERE d.user_id=? AND l.user_id=d.user_id AND l.ended_at IS NULL", (notification_id, user_id))


_app = None
_app_path = None
_app_lock = threading.Lock()


def send(config, token: str, payload: dict):
    global _app, _app_path
    import firebase_admin
    from firebase_admin import credentials, messaging
    path = os.environ.get('DONATIX_FIREBASE_CREDENTIALS', '')
    if not path or not Path(path).is_file():
        raise RuntimeError('Explicit Firebase service-account file is required')
    with _app_lock:
        if _app is None or _app_path != path:
            if _app is not None:
                firebase_admin.delete_app(_app)
            # Do not use Application Default Credentials or metadata discovery.
            _app = firebase_admin.initialize_app(credentials.Certificate(path), name='donatix-native')
            _app_path = path
    data = {k: str(v) for k, v in payload.items() if k != 'platform'}
    if payload.get('platform') == 'ios':
        # iOS can show a queued alert after logout. Never put private order/balance text into APNs.
        message = messaging.Message(token=token, data=data, apns=messaging.APNSConfig(
            headers={'apns-push-type': 'alert', 'apns-priority': '10',
                     'apns-expiration': str(int(time.time()) + 86400),
                     'apns-collapse-id': 'dx-' + str(payload['notification_id'])},
            payload=messaging.APNSPayload(aps=messaging.Aps(
                alert=messaging.ApsAlert(title=config.site_name, body='Новое уведомление в Donatix'), sound='default'))))
    else:
        message = messaging.Message(token=token, data=data,
            android=messaging.AndroidConfig(priority='high', ttl=__import__('datetime').timedelta(hours=24)))
    messaging.send(message, app=_app)


def dispatch(conn, config, sender=send, now=None):
    now = time.time() if now is None else now
    rows = conn.execute("SELECT id FROM native_push_outbox WHERE state IN ('queued','sending') "
                        "AND next_try<=? ORDER BY id LIMIT 40", (now,)).fetchall()
    sent = 0
    for candidate in rows:
        claim = secrets.token_hex(16)
        with db.tx(conn):
            if not conn.execute("UPDATE native_push_outbox SET state='sending',claim=?,next_try=?,attempts=attempts+1 "
                                "WHERE id=? AND state IN ('queued','sending') AND next_try<=?",
                                (claim, now + 120, candidate['id'], now)).rowcount:
                continue
            row = conn.execute("SELECT o.*,d.user_id,d.sid,COALESCE(p.platform,'android') AS platform,d.token_enc,n.text,n.link,n.read_at "
                'FROM native_push_outbox o JOIN native_push_devices d ON d.device_id=o.device_id '
                'LEFT JOIN native_push_platforms p ON p.device_id=d.device_id '
                'JOIN notifications n ON n.id=o.notification_id AND n.user_id=d.user_id '
                'JOIN logins l ON l.sid=d.sid AND l.user_id=d.user_id AND l.ended_at IS NULL '
                'WHERE o.id=?', (candidate['id'],)).fetchone()
            if not row or row['read_at']:
                conn.execute("DELETE FROM native_push_outbox WHERE id=? AND claim=?", (candidate['id'], claim))
                continue
        try:
            token = unseal(config.secret_key, row['token_enc'])
            if not token:
                raise ValueError('Invalid encrypted token')
            payload = {'user_id': row['user_id'], 'notification_id': row['notification_id'],
                       'platform': row['platform'], 'link': row['link'] or '/panel/notifications'}
            if row['platform'] == 'ios':
                payload['binding'] = binding(config, row['sid'], row['user_id'], row['device_id'])
            else:
                payload.update(title=config.site_name, body=row['text'])
            sender(config, token, payload)
            conn.execute("DELETE FROM native_push_outbox WHERE id=? AND claim=?", (candidate['id'], claim))
            sent += 1
        except Exception as exc:
            # Persist only the exception type: SDK messages may contain registration tokens.
            invalid = type(exc).__name__ in ('UnregisteredError', 'SenderIdMismatchError')
            if invalid:
                conn.execute('DELETE FROM native_push_devices WHERE device_id=? AND token_enc=?',
                             (row['device_id'], row['token_enc']))
                conn.execute('DELETE FROM native_push_platforms WHERE device_id NOT IN (SELECT device_id FROM native_push_devices)')
            state = 'failed' if invalid or row['attempts'] >= 10 else 'queued'
            conn.execute('UPDATE native_push_outbox SET state=?,error=?,next_try=? WHERE id=? AND claim=?',
                         (state, type(exc).__name__, now + min(3600, 2 ** row['attempts'] * 5), candidate['id'], claim))
    return sent


class PushWorker:
    def __init__(self, config):
        self.config = config
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='donatix-fcm', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        import logging
        conn = db.connect(self.config.db_path)
        try:
            while not self.stop_event.is_set():
                try:
                    if enabled():
                        dispatch(conn, self.config)
                    from .account_deletion import finish_ready
                    finish_ready(conn, self.config)
                except Exception:
                    logging.getLogger(__name__).exception('Native background processing failed')
                self.stop_event.wait(2)
        finally:
            conn.close()
