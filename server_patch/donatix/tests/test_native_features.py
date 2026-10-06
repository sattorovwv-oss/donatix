"""Money, ownership and mobile OAuth regression tests, with isolated SQLite."""
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from conftest import balance, csrf_of, make_client, web_login
from donatix import accounts, google_auth, notify, orders


def login(client, conn):
    uid, _ = make_client(conn)
    web_login(client, 'shop1@example.com', 'password123')
    return uid, {'X-CSRF-Token': client.get('/api/v1/mobile-session').json()['csrf']}


def test_guest_catalog_and_private_routes(client):
    for path in ('categories', 'products', 'products/tg-stars'):
        r = client.get('/api/v1/mobile/public/' + path)
        assert r.status_code == 200, r.text
        assert 'base_price' not in r.text and 'supplier_ref' not in r.text
    for path in ('stats', 'keys', 'bots', 'dcoin', 'timezone'):
        assert client.get('/api/v1/mobile/' + path).status_code == 401


def test_quote_never_debits_and_matches_order(client, conn):
    uid, h = login(client, conn)
    body = {'product_id': 'tg-stars', 'quantity': 100, 'fields': {'telegram_username': '@player_one'}}
    before = balance(conn, uid)
    quote = client.post('/api/v1/mobile/orders/quote', json=body, headers=h)
    assert quote.status_code == 200, quote.text
    assert quote.json()['total_usd'] == '1.6605'
    assert balance(conn, uid) == before
    assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
    r = client.post('/api/v1/orders', json=body, headers={**h, 'Idempotency-Key': 'native-one'})
    assert r.status_code == 201 and r.json()['order']['total_usd'] == quote.json()['total_usd']


def test_cart_retries_after_low_remaining_balance_without_double_charge(client, conn):
    uid, h = login(client, conn)
    # A balance which is enough for the basket but cannot buy it twice.
    conn.execute('UPDATE users SET balance_micro=30000 WHERE id=?', (uid,))
    body = {'items': [{'product_id': 'topup-pubg-60', 'count': 2}], 'fields': {'player_id': '5123456789'}}
    headers = {**h, 'Idempotency-Key': 'cart-secure-operation-01'}
    a = client.post('/api/v1/mobile/cart', json=body, headers=headers)
    assert a.status_code == 200, a.text
    assert not a.json()['partial'] and len(a.json()['items']) == 2
    after = balance(conn, uid)
    b = client.post('/api/v1/mobile/cart', json=body, headers=headers)
    assert b.status_code == 200, b.text
    assert [o['order_id'] for o in a.json()['items']] == [o['order_id'] for o in b.json()['items']]
    assert balance(conn, uid) == after
    body['items'][0]['count'] = 1
    assert client.post('/api/v1/mobile/cart', json=body, headers=headers).status_code == 409
    assert balance(conn, uid) == after


def test_cart_partial_connection_recovery(client, conn, config, supplier):
    uid, h = login(client, conn)
    user = accounts.get_user(conn, uid)
    key = 'cart-disconnected-operation-02'
    first, _ = orders.create_order(conn, config, supplier, user,
        product_id='topup-pubg-60', quantity=1, fields={'player_id': '5123456789'},
        client_idem_key=f'mcart-{key}-0', source='panel')
    after_first = balance(conn, uid)
    body = {'items': [{'product_id': 'topup-pubg-60', 'count': 2}], 'fields': {'player_id': '5123456789'}}
    r = client.post('/api/v1/mobile/cart', json=body, headers={**h, 'Idempotency-Key': key})
    assert r.status_code == 200 and r.json()['items'][0]['order_id'] == first['public_id'], r.text
    assert conn.execute('SELECT COUNT(*) FROM orders WHERE user_id=?', (uid,)).fetchone()[0] == 2
    assert balance(conn, uid) == after_first - first['total_micro']


def test_cart_validation_and_csrf_do_not_debit(client, conn):
    uid, h = login(client, conn)
    before = balance(conn, uid)
    body = {'items': [{'product_id': 'topup-pubg-60', 'count': 1}], 'fields': {'player_id': '5123456789'}}
    headers = {**h, 'Idempotency-Key': 'cart-validation-operation-03'}
    assert client.post('/api/v1/mobile/cart', json=body,
                       headers={'Idempotency-Key': headers['Idempotency-Key']}).status_code == 403
    body['items'].append({'product_id': 'tg-stars', 'count': 1})
    assert client.post('/api/v1/mobile/cart', json=body, headers=headers).status_code == 400
    assert balance(conn, uid) == before
    assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0


def test_notifications_mark_only_current_account(client, conn):
    uid, h = login(client, conn)
    other, _ = make_client(conn, 'other')
    notify.notify(conn, None, uid, 'Ваш заказ', '/panel/orders')
    notify.notify(conn, None, other, 'Другой заказ')
    assert client.get('/api/v1/mobile/notifications').json()['unread'] == 1
    assert client.post('/api/v1/mobile/notifications/read', json={}).status_code == 403
    assert client.post('/api/v1/mobile/notifications/read', json={}, headers=h).status_code == 200
    assert notify.unread_count(conn, uid) == 0 and notify.unread_count(conn, other) == 1


def test_keys_ownership_revoke_and_admin_guard(client, conn):
    uid, h = login(client, conn)
    other, _ = make_client(conn, 'other')
    kid = conn.execute('SELECT id FROM api_keys WHERE user_id=?', (other,)).fetchone()[0]
    assert client.post(f'/api/v1/mobile/keys/{kid}/reveal', json={}, headers=h).status_code == 404
    assert client.post(f'/api/v1/mobile/keys/{kid}/revoke', json={}, headers=h).status_code == 404
    assert conn.execute('SELECT revoked_at FROM api_keys WHERE id=?', (kid,)).fetchone()[0] is None
    own = conn.execute('SELECT id FROM api_keys WHERE user_id=?', (uid,)).fetchone()[0]
    assert client.post(f'/api/v1/mobile/keys/{own}/revoke', json={}, headers=h).status_code == 200
    assert client.get('/api/v1/mobile/admin/pricelist').status_code == 403
    assert client.post('/api/v1/mobile/bots/9999/delete', json={}, headers=h).status_code == 404


def test_native_report_timezone_filters(client, conn, supplier):
    uid, h = login(client, conn)
    assert client.post('/api/v1/mobile/timezone', json={'timezone':'Wrong/Zone'}, headers=h).status_code == 400
    assert client.post('/api/v1/mobile/timezone', json={'timezone':'Asia/Dushanbe'}, headers=h).status_code == 200
    assert client.get('/api/v1/mobile/timezone').json()['choice'] == 'Asia/Dushanbe'
    body = {'product_id':'tg-stars', 'quantity':100, 'fields':{'telegram_username':'@player_one'}}
    r = client.post('/api/v1/orders', json=body, headers={**h, 'Idempotency-Key':'filters-one'})
    orders.process_pending(conn, supplier)
    assert client.get('/api/v1/orders?q=nothing-here').json()['total'] == 0
    d = client.get('/api/v1/orders', params={'q':r.json()['order']['order_id'], 'period':'today'}).json()
    assert d['total'] == 1 and d['totals']['done'] == 1 and d['totals']['spent'] == '1.6605'
    assert len(client.get('/api/v1/transactions?type=debit').json()['items']) == 1
    assert len(client.get('/api/v1/transactions?type=credit').json()['items']) == 1
    for path in ('stats', 'dcoin', 'dcoin/chart', 'bots', 'referrals'):
        response = client.get('/api/v1/mobile/' + path)
        assert response.status_code == 200, response.text
        assert 'password_hash' not in response.text and 'token_enc' not in response.text


def test_mobile_oauth_verifier_confirmation_and_single_claim(client, app, config, conn, monkeypatch):
    config.google_client_id, config.google_client_secret = 'test-id', 'test-secret'
    monkeypatch.setattr(google_auth, 'fetch_profile', lambda *a: {
        'sub':'native-g1', 'email':'native@gmail.com', 'email_verified':True, 'name':'Native'})
    verifier = 'v' * 64
    prepare = client.post('/api/v1/mobile/oauth/prepare', json={
        'challenge':hashlib.sha256(verifier.encode()).hexdigest()})
    assert prepare.status_code == 200, prepare.text
    ticket = prepare.json()['ticket']
    claim = {'ticket':ticket, 'verifier':verifier}
    assert client.post('/api/v1/mobile/oauth/claim', json=claim).json()['pending']
    assert client.post('/api/v1/mobile/oauth/claim', json={**claim, 'verifier':'x'*64}).status_code == 410
    browser = TestClient(app)
    browser.get('/api/v1/mobile/oauth/start/' + ticket, follow_redirects=False)
    g = browser.get('/auth/google', follow_redirects=False)
    state = parse_qs(urlparse(g.headers['location']).query)['state'][0]
    callback = browser.get('/auth/google/callback', params={'state':state, 'code':'test'}, follow_redirects=False)
    assert callback.headers['location'] == '/api/v1/mobile/oauth/confirm/' + ticket
    assert client.post('/api/v1/mobile/oauth/claim', json=claim).json()['pending']
    confirmation = browser.get(callback.headers['location'])
    assert 'native@gmail.com' in confirmation.text
    assert browser.post(callback.headers['location'], data={'csrf':'wrong'}).status_code == 403
    assert browser.post(callback.headers['location'], data={'csrf':csrf_of(confirmation.text)}).status_code == 200
    claimed = client.post('/api/v1/mobile/oauth/claim', json=claim)
    assert claimed.status_code == 200 and not claimed.json()['pending'] and 'dx_session' in claimed.cookies
    assert client.get('/api/v1/me').json()['email'] == 'native@gmail.com'
    assert client.post('/api/v1/mobile/oauth/claim', json=claim).status_code == 410


def test_expired_mobile_oauth_never_claims(client, config, conn):
    config.google_client_id, config.google_client_secret = 'test-id', 'test-secret'
    verifier = 'a'*64
    d = client.post('/api/v1/mobile/oauth/prepare', json={'challenge':hashlib.sha256(verifier.encode()).hexdigest()}).json()
    conn.execute('UPDATE mobile_oauth SET expires=0 WHERE id=?', (d['ticket'],))
    assert client.post('/api/v1/mobile/oauth/claim', json={'ticket':d['ticket'], 'verifier':verifier}).status_code == 410


def test_confirmed_price_change_never_debits(client, conn):
    uid, h = login(client, conn)
    body = {'product_id':'tg-stars', 'quantity':100, 'fields':{'telegram_username':'@player_one'}}
    quote = client.post('/api/v1/mobile/orders/quote', json=body, headers=h).json()
    body['expected_total_usd'] = quote['total_usd']
    before = balance(conn, uid)
    conn.execute("UPDATE products SET base_price='0.02' WHERE id='tg-stars'")
    r = client.post('/api/v1/orders', json=body, headers={**h, 'Idempotency-Key':'price-changed-operation'})
    assert r.status_code == 409 and r.json()['code'] == 'price_changed', r.text
    assert balance(conn, uid) == before
    assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0


def test_cart_quote_price_binding(client, conn):
    uid, h = login(client, conn)
    body = {'items':[{'product_id':'topup-pubg-60','count':2}], 'fields':{'player_id':'5123456789'}}
    before = balance(conn, uid)
    q = client.post('/api/v1/mobile/cart/quote', json=body, headers=h)
    assert q.status_code == 200, q.text
    assert balance(conn, uid) == before and q.json()['total_usd'] == '2.1384'
    body['items'] = q.json()['items']
    conn.execute("UPDATE products SET base_price='2' WHERE id='topup-pubg-60'")
    r = client.post('/api/v1/mobile/cart', json=body, headers={**h,'Idempotency-Key':'cart-price-changed-operation'})
    assert r.status_code == 200 and r.json()['partial'] and r.json()['items'] == [], r.text
    assert balance(conn, uid) == before
    assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
