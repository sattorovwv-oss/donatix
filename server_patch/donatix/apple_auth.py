"""Native Sign in with Apple. Explicit owner key, bound one-use flow and token revocation."""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

import httpx
import jwt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import accounts, db, security
from .api import ApiError, _limit
from .deps import csrf_token, get_config, get_conn, session_user

router = APIRouter(prefix='/apple', tags=['Sign in with Apple'])
ISSUER = 'https://appleid.apple.com'
_keys = None
_keys_until = 0
_lock = threading.Lock()


def init(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS apple_flows (
      id TEXT PRIMARY KEY, challenge TEXT NOT NULL, nonce TEXT NOT NULL,
      expires INTEGER NOT NULL, user_id INTEGER);
    CREATE TABLE IF NOT EXISTS apple_identities (
      subject TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
      refresh_enc TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
      attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, error TEXT);
    ''')


def settings():
    return {name: os.environ.get('DONATIX_APPLE_' + name, '')
            for name in ('CLIENT_ID', 'TEAM_ID', 'KEY_ID', 'PRIVATE_KEY_FILE')}


def enabled():
    s = settings()
    return all(s.values()) and s['CLIENT_ID'] == 'tj.donatix.app' and Path(s['PRIVATE_KEY_FILE']).is_file()


def client_secret():
    if not enabled():
        raise ApiError('Владелец ещё не настроил вход через Apple.', 'apple_disabled', 503)
    s = settings()
    now = int(time.time())
    return jwt.encode({'iss': s['TEAM_ID'], 'iat': now, 'exp': now + 300,
                       'aud': ISSUER, 'sub': s['CLIENT_ID']},
                      Path(s['PRIVATE_KEY_FILE']).read_bytes(), algorithm='ES256',
                      headers={'kid': s['KEY_ID']})


def fetch_keys(transport=None, force=False):
    global _keys, _keys_until
    with _lock:
        if _keys is not None and _keys_until > time.time() and not force:
            return _keys
        with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
            response = client.get(ISSUER + '/auth/keys')
            response.raise_for_status()
            keys = response.json()['keys']
        if not isinstance(keys, list) or not keys:
            raise ValueError('Empty Apple keys')
        _keys, _keys_until = keys, time.time() + 3600
        return keys


def verify(token, nonce, transport=None):
    try:
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256' or not header.get('kid'):
            raise ValueError('Invalid Apple algorithm')
        key = next((key for key in fetch_keys(transport) if key.get('kid') == header['kid']), None)
        if key is None:
            key = next((key for key in fetch_keys(transport, force=True) if key.get('kid') == header['kid']), None)
        if key is None:
            raise ValueError('Unknown Apple key')
        claims = jwt.decode(token, jwt.algorithms.RSAAlgorithm.from_jwk(key), algorithms=['RS256'],
            audience=settings()['CLIENT_ID'], issuer=ISSUER,
            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']})
        if not secrets.compare_digest(str(claims['nonce']), nonce) or not claims['sub']:
            raise ValueError('Wrong Apple nonce')
        return claims
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise ApiError('Apple не подтвердил этот запрос входа. Начните заново.', 'apple_invalid', 403) from None
    except httpx.HTTPError:
        raise ApiError('Сервер Apple сейчас недоступен. Попробуйте снова.', 'apple_unavailable', 503) from None


def exchange(code, nonce, subject, transport=None):
    try:
        with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
            response = client.post(ISSUER + '/auth/token', data={
                'client_id': settings()['CLIENT_ID'], 'client_secret': client_secret(),
                'code': code, 'grant_type': 'authorization_code'})
            if response.status_code != 200:
                raise ValueError('Apple rejected code')
            tokens = response.json()
        claims = verify(tokens['id_token'], nonce, transport)
        if claims['sub'] != subject or not tokens.get('refresh_token'):
            raise ValueError('Apple code belongs to another request')
        return tokens['refresh_token']
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise ApiError('Apple не подтвердил код. Начните вход заново.', 'apple_code_invalid', 403) from None


def recent_session(request, conn, uid):
    from datetime import datetime, timezone
    row = conn.execute('SELECT created_at FROM logins WHERE sid=? AND user_id=? AND ended_at IS NULL',
                       (request.session.get('sid', ''), uid)).fetchone()
    return bool(row and (datetime.now(timezone.utc) - datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)).total_seconds() <= 300)


def require_csrf(request):
    expected = request.session.get('csrf', '')
    supplied = request.headers.get('x-csrf-token', '')
    if not expected or not secrets.compare_digest(expected, supplied):
        raise ApiError('Обновите сессию и повторите запрос.', 'csrf_invalid', 403)


class Prepare(BaseModel):
    challenge: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
    link_account: bool = False


@router.post('/prepare')
def prepare(body: Prepare, request: Request, conn=Depends(get_conn)):
    _limit(request, 'login', request.client.host if request.client else '?')
    if not enabled():
        raise ApiError('Вход через Apple ещё не настроен.', 'apple_disabled', 503)
    user = session_user(request, conn) if body.link_account else None
    if body.link_account:
        require_csrf(request)
    if body.link_account and (not user or not recent_session(request, conn, user['id'])):
        raise ApiError('Для привязки Apple сначала войдите в аккаунт заново.', 'reauth_required', 403)
    ticket, nonce = secrets.token_urlsafe(32), secrets.token_hex(32)
    conn.execute('DELETE FROM apple_flows WHERE expires<?', (int(time.time()),))
    conn.execute('INSERT INTO apple_flows VALUES (?,?,?,?,?)',
                 (ticket, body.challenge, nonce, int(time.time()) + 300, user['id'] if user else None))
    return {'ok': True, 'ticket': ticket, 'nonce': nonce}


class Claim(BaseModel):
    ticket: str = Field(min_length=40, max_length=100)
    verifier: str = Field(min_length=43, max_length=128)
    identity_token: str = Field(min_length=100, max_length=12000)
    authorization_code: str = Field(min_length=10, max_length=4096)


@router.post('/claim')
def claim(body: Claim, request: Request, conn=Depends(get_conn), config=Depends(get_config)):
    _limit(request, 'login', request.client.host if request.client else '?')
    if not enabled():
        raise ApiError('Вход через Apple ещё не настроен.', 'apple_disabled', 503)
    # Consume before exchanging the single-use code. Concurrent replays never create a second session.
    with db.tx(conn):
        flow = conn.execute('SELECT * FROM apple_flows WHERE id=?', (body.ticket,)).fetchone()
        if not flow or flow['expires'] < time.time() or not secrets.compare_digest(
                flow['challenge'], hashlib.sha256(body.verifier.encode()).hexdigest()):
            raise ApiError('Запрос входа истёк или уже использован.', 'expired', 410)
        if flow['user_id'] is not None:
            require_csrf(request)
            user = session_user(request, conn)
            if not user or user['id'] != flow['user_id'] or not recent_session(request, conn, user['id']):
                raise ApiError('Повторите вход перед привязкой Apple.', 'reauth_required', 403)
        conn.execute('DELETE FROM apple_flows WHERE id=?', (body.ticket,))
    claims = verify(body.identity_token, flow['nonce'])
    known = conn.execute('SELECT user_id FROM apple_identities WHERE subject=?', (claims['sub'],)).fetchone()
    known_uid = flow['user_id'] if flow['user_id'] is not None else (known['user_id'] if known else None)
    if known_uid is not None:
        previous = accounts.get_user(conn, known_uid)
        if not previous or previous['status'] == 'blocked' or conn.execute(
                "SELECT 1 FROM account_deletions WHERE user_id=? AND state='waiting'", (known_uid,)).fetchone():
            raise ApiError('Аккаунт недоступен.', 'blocked', 403)
    refresh = exchange(body.authorization_code, flow['nonce'], claims['sub'])
    from . import sitecfg
    with db.tx(conn):
        if known_uid is not None:
            previous = accounts.get_user(conn, known_uid)
            if not previous or previous['status'] == 'blocked':
                raise ApiError('Аккаунт недоступен.', 'blocked', 403)
        identity = conn.execute('SELECT * FROM apple_identities WHERE subject=?', (claims['sub'],)).fetchone()
        if flow['user_id'] is not None:
            if identity and identity['user_id'] != flow['user_id']:
                raise ApiError('Этот Apple ID уже привязан к другому аккаунту.', 'apple_already_linked', 409)
            user = accounts.get_user(conn, flow['user_id'])
            another = conn.execute('SELECT subject FROM apple_identities WHERE user_id=?', (flow['user_id'],)).fetchone()
            if another and another['subject'] != claims['sub']:
                raise ApiError('К аккаунту уже привязан другой Apple ID.', 'apple_already_linked', 409)
        elif identity:
            user = accounts.get_user(conn, identity['user_id'])
        else:
            email = str(claims.get('email') or '').lower()
            if claims.get('email_verified') not in (True, 'true') or '@' not in email:
                raise ApiError('Apple не передал подтверждённую почту.', 'apple_email_required', 403)
            if conn.execute('SELECT 1 FROM users WHERE lower(email)=?', (email,)).fetchone():
                raise ApiError('Аккаунт с этой почтой уже существует. Войдите обычным способом и привяжите Apple в меню аккаунта.', 'account_exists', 409)
            if not sitecfg.registration_open(conn):
                raise ApiError('Регистрация новых партнёров временно закрыта.', 'registration_closed', 403)
            uid = accounts.create_user(conn, email=email, login='apple_' + secrets.token_hex(8),
                password=secrets.token_urlsafe(32), status='pending' if config.require_approval else 'active')
            user = accounts.get_user(conn, uid)
        if not user or user['status'] == 'blocked' or conn.execute(
                "SELECT 1 FROM account_deletions WHERE user_id=? AND state='waiting'", (user['id'],)).fetchone():
            raise ApiError('Аккаунт недоступен.', 'blocked', 403)
        conn.execute("INSERT INTO apple_identities (subject,user_id,refresh_enc) VALUES (?,?,?) "
                     "ON CONFLICT(subject) DO UPDATE SET refresh_enc=excluded.refresh_enc,state='active',attempts=0,next_try=0,error=NULL",
                     (claims['sub'], user['id'], security.seal(config.secret_key, refresh)))
    if flow['user_id'] is None:
        from .web import _finish_login
        _finish_login(request, conn, user)  # Preserves administrator second-factor checks.
    return {'ok': True, 'verification': bool(request.session.get('pending_2fa')), 'csrf': csrf_token(request)}


def queue_revocation(conn, uid):
    conn.execute("UPDATE apple_identities SET state='pending',next_try=0 WHERE user_id=? AND state<>'revoked'", (uid,))


def revoke_pending(conn, config, transport=None, now=None):
    now = time.time() if now is None else now
    if not enabled():
        return
    rows = conn.execute("SELECT a.* FROM apple_identities a JOIN account_deletions d ON d.user_id=a.user_id "
                        "WHERE a.state='pending' AND a.next_try<=? AND d.state='waiting' LIMIT 10", (now,)).fetchall()
    for row in rows:
        # A lease prevents two workers sending the same request. Revocation is idempotent at Apple.
        if not conn.execute("UPDATE apple_identities SET next_try=? WHERE subject=? AND state='pending' AND next_try<=?",
                            (now + 120, row['subject'], now)).rowcount:
            continue
        try:
            token = security.unseal(config.secret_key, row['refresh_enc'])
            if not token:
                raise ValueError('Invalid encrypted Apple token')
            with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
                response = client.post(ISSUER + '/auth/revoke', data={
                    'client_id': settings()['CLIENT_ID'], 'client_secret': client_secret(),
                    'token': token, 'token_type_hint': 'refresh_token'})
                response.raise_for_status()
            conn.execute("UPDATE apple_identities SET state='revoked',refresh_enc='',error=NULL WHERE subject=?", (row['subject'],))
        except Exception as exc:
            conn.execute('UPDATE apple_identities SET attempts=attempts+1,error=?,next_try=? WHERE subject=?',
                         (type(exc).__name__, now + min(3600, 5 * 2 ** min(10, row['attempts'] + 1)), row['subject']))
