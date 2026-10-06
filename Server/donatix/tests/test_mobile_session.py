# Copy to donatix/tests/test_mobile_session.py when applying the server patch.
from conftest import make_client, web_login, balance
from donatix import accounts


def test_mobile_session_requires_login(client):
    assert client.get('/api/v1/mobile-session').status_code == 401


def test_mobile_session_and_csrf(client, conn):
    uid, _ = make_client(conn)
    web_login(client, 'shop1@example.com', 'password123')
    d = client.get('/api/v1/mobile-session').json()
    assert d['ok'] and d['csrf'] and d['login'] == 'shop1'
    assert client.get('/api/v1/me').json()['login'] == 'shop1'
    payload = {'product_id': 'tg-stars', 'quantity': 100, 'fields': {'telegram_username': '@player_one'}}
    before = balance(conn, uid)
    assert client.post('/api/v1/orders', json=payload).status_code == 403
    assert client.post('/api/v1/orders', json=payload, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert balance(conn, uid) == before
    headers = {'X-CSRF-Token': d['csrf'], 'Idempotency-Key': 'mobile-1'}
    first = client.post('/api/v1/orders', json=payload, headers=headers)
    second = client.post('/api/v1/orders', json=payload, headers=headers)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()['order']['order_id'] == second.json()['order']['order_id']
    assert balance(conn, uid) == before - 16605


def test_logout_revokes_mobile_access(client, conn):
    make_client(conn)
    token = web_login(client, 'shop1@example.com', 'password123')
    client.post('/logout', data={'csrf': token})
    assert client.get('/api/v1/mobile-session').status_code == 401
    assert client.get('/api/v1/me').status_code == 401


def test_blocked_user_loses_session_access(client, conn):
    uid, _ = make_client(conn)
    web_login(client, 'shop1@example.com', 'password123')
    conn.execute("UPDATE users SET status = 'blocked' WHERE id = ?", (uid,))
    assert client.get('/api/v1/me').status_code == 401


def test_mobile_account_views_are_public_and_owned(client, conn):
    make_client(conn)
    web_login(client, 'shop1@example.com', 'password123')
    for path in ('home', 'notifications', 'referrals', 'logins', 'categories'):
        r = client.get(f'/api/v1/mobile/{path}')
        assert r.status_code == 200, r.text
        assert r.json()['ok']
        assert 'password_hash' not in r.text
        assert 'token_enc' not in r.text
        assert 'key_hash' not in r.text



def test_mobile_catalog_only_exposes_customer_prices(client, conn):
    make_client(conn)
    web_login(client, 'shop1@example.com', 'password123')
    r = client.get('/api/v1/mobile/categories', params={'kind': 'telegram_stars'})
    assert r.status_code == 200
    item = r.json()['items'][0]
    assert item['from_price'] == '0.016605'
    assert 'base_price' not in r.text and 'supplier_ref' not in r.text
