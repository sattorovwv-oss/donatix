"""Isolated release regressions. No real credentials, suppliers or network sends."""
import socket

import pytest
from conftest import make_client, web_login
from donatix import account_deletion, accounts, db, native_push, notify, security, orders, bots, dcoin


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('External network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


def login(client, conn, balance='0', name='eraser'):
    uid, key = make_client(conn,name,balance=balance)
    web_login(client,name+'@example.com','password123')
    h={'X-CSRF-Token':client.get('/api/v1/mobile-session').json()['csrf']}
    sid=conn.execute('SELECT sid FROM logins WHERE user_id=? ORDER BY id DESC',(uid,)).fetchone()[0]
    return uid,key,h,sid


def test_delete_requires_session_csrf_confirmation_and_reauthentication(client,conn):
    uid,key,h,sid=login(client,conn)
    endpoint='/api/v1/mobile/account/deletion'
    body={'confirmation':'УДАЛИТЬ','password':'password123'}
    assert client.post(endpoint,json=body).status_code == 403
    assert client.post(endpoint,json={**body,'confirmation':'да'},headers=h).status_code == 400
    assert client.post(endpoint,json={**body,'password':'wrong'},headers=h).status_code == 403
    # A leaked API key cannot erase its owner.
    client.cookies.clear()
    assert client.post(endpoint,json=body,headers={'X-API-Key':key}).status_code == 403
    assert accounts.get_user(conn,uid)


def test_zero_balance_account_and_related_data_erased(client,conn,config):
    uid,key,h,sid=login(client,conn)
    other,_=make_client(conn,'survivor',balance='0')
    conn.execute('INSERT INTO support_links VALUES (?,?,?)',(10101,uid,db.now()))
    conn.execute('INSERT INTO support_history (tg_id,role,content,created_at) VALUES (?,?,?,?)',(10101,'user','private',db.now()))
    conn.execute('INSERT INTO support_memory (tg_id,fact,created_at) VALUES (?,?,?)',(10101,'private',db.now()))
    conn.execute('INSERT INTO shop_users (tg_id,user_id,name,created_at) VALUES (?,?,?,?)',(10101,uid,'Name',db.now()))
    db.set_setting(conn,f'tz.user.{uid}','Asia/Dushanbe')
    db.set_setting(conn,f'bots.block.{uid}','blocked')
    native_push.register(conn,config,uid,sid,'a'*32,'token-for-device-00000001')
    notify.notify(conn,None,uid,'private')
    r=client.post('/api/v1/mobile/account/deletion',json={'confirmation':'УДАЛИТЬ','password':'password123'},headers=h)
    assert r.status_code == 200,r.text
    assert r.json()['state']=='completed'
    assert accounts.get_user(conn,uid) is None
    assert accounts.get_user(conn,other)
    for table in ('api_keys','logins','notifications','native_push_devices','support_links','shop_users'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table} WHERE user_id=?',(uid,)).fetchone()[0]==0
    for table in ('support_history','support_memory','native_push_outbox'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0
    assert db.get_setting(conn,f'tz.user.{uid}') is None
    assert client.get('/api/v1/mobile-session').status_code==401


def test_positive_balance_waits_for_refund_then_purges(client,conn,config):
    uid,key,h,sid=login(client,conn,balance='12.50')
    r=client.post('/api/v1/mobile/account/deletion',json={'confirmation':'УДАЛИТЬ','password':'password123'},headers=h)
    assert r.json()['state']=='waiting'
    assert accounts.get_user(conn,uid)['balance_micro']==125000
    assert accounts.user_by_api_key(conn,key) is None
    assert conn.execute('SELECT ended_at FROM logins WHERE sid=?',(sid,)).fetchone()[0]
    account_deletion.finish_ready(conn,config)
    assert accounts.get_user(conn,uid)
    with db.tx(conn): accounts.post_ledger(conn,uid,-125000,'Фактический возврат остатка')
    account_deletion.finish_ready(conn,config)
    assert accounts.get_user(conn,uid) is None
    assert not conn.execute('SELECT 1 FROM account_deletions WHERE user_id=?',(uid,)).fetchone()


def test_admin_deletion_not_silently_removes_owner(client,conn):
    web_login(client,'admin@example.com','adminpass123')
    h={'X-CSRF-Token':client.get('/api/v1/mobile-session').json()['csrf']}
    r=client.post('/api/v1/mobile/account/deletion',json={'confirmation':'УДАЛИТЬ','password':'adminpass123'},headers=h)
    assert r.status_code==409
    assert conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]==1


def test_deletion_public_web_resource_and_admin_queue(client,conn):
    page=client.get('/account-deletion')
    assert page.status_code==200 and 'Войти для удаления' in page.text
    uid,key,h,sid=login(client,conn,balance='5')
    r=client.post('/account-deletion',data={'csrf':h['X-CSRF-Token'],'confirmation':'УДАЛИТЬ','password':'password123'},follow_redirects=False)
    assert r.status_code==303 and 'waiting' in r.headers['location']
    assert client.get('/admin/account-deletions',follow_redirects=False).status_code==303
    web_login(client,'admin@example.com','adminpass123')
    assert 'eraser' in client.get('/admin/account-deletions').text


def test_push_registration_uses_session_and_encrypted_token(client,conn,config):
    uid,key,h,sid=login(client,conn)
    body={'device_id':'b'*32,'token':'token-for-device-00000002'}
    assert client.post('/api/v1/mobile/push/register',json=body).status_code==403
    r=client.post('/api/v1/mobile/push/register',json=body,headers=h)
    assert r.status_code==200,r.text
    row=conn.execute('SELECT * FROM native_push_devices').fetchone()
    assert row['user_id']==uid and row['sid']==sid and body['token'] not in row['token_enc']
    assert security.unseal(config.secret_key,row['token_enc'])==body['token']


def test_push_delivers_once_and_retry_is_persistent(client,conn,config):
    uid,key,h,sid=login(client,conn)
    native_push.register(conn,config,uid,sid,'c'*32,'token-for-device-00000003')
    notify.notify(conn,None,uid,'Order finished','/panel/orders')
    called=[]
    def fail(*args): raise RuntimeError('temporary')
    assert native_push.dispatch(conn,config,fail,now=100)==0
    assert conn.execute('SELECT state,attempts FROM native_push_outbox').fetchone()['state']=='queued'
    def delivered(cfg,token,payload): called.append((token,payload))
    assert native_push.dispatch(conn,config,delivered,now=200)==1
    assert called[0][1]['user_id']==uid and called[0][1]['body']=='Order finished'
    assert native_push.dispatch(conn,config,delivered,now=300)==0
    assert len(called)==1


def test_push_revoked_session_and_read_messages_never_sent(client,conn,config):
    uid,key,h,sid=login(client,conn)
    native_push.register(conn,config,uid,sid,'d'*32,'token-for-device-00000004')
    notify.notify(conn,None,uid,'private')
    conn.execute('UPDATE logins SET ended_at=? WHERE sid=?',(db.now(),sid))
    def never(*args): pytest.fail('Revoked device received a push')
    native_push.dispatch(conn,config,never,now=100)
    assert not conn.execute('SELECT 1 FROM native_push_outbox').fetchone()


def test_push_device_switch_discards_previous_owner_outbox(client,conn,config):
    uid,key,h,sid=login(client,conn)
    native_push.register(conn,config,uid,sid,'e'*32,'token-for-device-00000005')
    notify.notify(conn,None,uid,'Old owner private data')
    client.cookies.clear()
    uid2,key2,h2,sid2=login(client,conn,name='owner2')
    native_push.register(conn,config,uid2,sid2,'e'*32,'token-for-device-00000005')
    assert not conn.execute('SELECT 1 FROM native_push_outbox').fetchone()
    assert conn.execute('SELECT user_id FROM native_push_devices').fetchone()[0]==uid2


def test_push_unregister_cannot_remove_another_users_device(client,conn,config):
    uid,key,h,sid=login(client,conn)
    native_push.register(conn,config,uid,sid,'f'*32,'token-for-device-00000006')
    uid2,_=make_client(conn,'owner2',balance='0')
    native_push.unregister(conn,uid2,'f'*32)
    assert conn.execute('SELECT COUNT(*) FROM native_push_devices').fetchone()[0]==1
    native_push.unregister(conn,uid,'f'*32)
    assert conn.execute('SELECT COUNT(*) FROM native_push_devices').fetchone()[0]==0


def test_erasure_cleans_receipt_and_bot_files_without_reusing_bot_directory(client,conn,config):
    uid,key,h,sid=login(client,conn)
    receipt=config.db_path.parent/'receipts'/'private.png'
    receipt.parent.mkdir()
    receipt.write_bytes(b'private receipt')
    conn.execute("INSERT INTO payments (user_id,method,amount_micro,pay_amount,pay_currency,receipt_file,status,created_at) "
                 "VALUES (?,'bank',0,'0','USD','private.png','rejected',?)",(uid,db.now()))
    first=bots.create(conn,config,user_id=uid,token='123456789:'+('A'*35),admin_ids='101',username='oldbot')
    folder=bots.bot_dir(config,first)
    folder.mkdir(parents=True)
    (folder/'bot.sqlite').write_bytes(b'private bot history')
    r=client.post('/api/v1/mobile/account/deletion',json={'confirmation':'УДАЛИТЬ','password':'password123'},headers=h)
    assert r.status_code==200 and r.json()['state']=='completed'
    assert not receipt.exists()
    assert conn.execute("SELECT 1 FROM account_erasure_files WHERE kind='bot' AND name=?",(str(first),)).fetchone()
    other,_=make_client(conn,'newbotowner',balance='0')
    second=bots.create(conn,config,user_id=other,token='987654321:'+('B'*35),admin_ids='202',username='newbot')
    assert second>first
    second_folder=bots.bot_dir(config,second)
    second_folder.mkdir(parents=True)
    conn.execute('UPDATE account_erasure_files SET after_ts=0')
    account_deletion.cleanup_pending(conn,config)
    assert not folder.exists() and second_folder.exists()
    assert not conn.execute('SELECT 1 FROM account_erasure_files').fetchone()


def test_erasure_keeps_other_users_dcoin_price(client,conn):
    from test_dcoin import _buy
    uid,key,h,sid=login(client,conn)
    other,_=make_client(conn,'othercoiner',balance='0')
    _buy(conn,uid,10_000)
    _buy(conn,other,20_000)
    before=dcoin.state(conn)
    price=dcoin.price(conn)
    units=dcoin.balance(conn,uid)
    r=client.post('/api/v1/mobile/account/deletion',json={'confirmation':'УДАЛИТЬ','password':'password123'},headers=h)
    assert r.status_code==200 and r.json()['state']=='completed',r.text
    after=dcoin.state(conn)
    assert after['pool']==before['pool'] and after['supply']==before['supply']-units
    assert after['reserve']==before['reserve']+units and dcoin.price(conn)==price
    assert conn.execute('SELECT COUNT(*) FROM orders WHERE user_id=?',(uid,)).fetchone()[0]==0
    assert accounts.get_user(conn,other)


def test_waiting_erasure_cannot_be_reactivated_by_status_change(client,conn):
    uid,key,h,sid=login(client,conn,balance='5')
    r=client.post('/api/v1/mobile/account/deletion',json={'confirmation':'УДАЛИТЬ','password':'password123'},headers=h)
    assert r.json()['state']=='waiting'
    conn.execute("UPDATE users SET status='active' WHERE id=?",(uid,))
    from conftest import csrf_of
    csrf=csrf_of(client.get('/login').text)
    r=client.post('/login',data={'csrf':csrf,'email':'eraser@example.com','password':'password123'},follow_redirects=False)
    assert r.status_code==403
    new_key=accounts.create_api_key(conn,uid,'admin-created')
    assert accounts.user_by_api_key(conn,new_key) is None
    assert client.get('/api/v1/me',headers={'X-API-Key':new_key}).status_code==401


def test_replaced_fcm_token_discards_old_device_queue(client,conn,config):
    uid,key,h,sid=login(client,conn)
    native_push.register(conn,config,uid,sid,'1'*32,'token-with-shared-device-0000001')
    notify.notify(conn,None,uid,'private old device message')
    native_push.register(conn,config,uid,sid,'2'*32,'token-with-shared-device-0000001')
    assert not conn.execute('SELECT 1 FROM native_push_outbox').fetchone()
    assert conn.execute('SELECT device_id FROM native_push_devices').fetchone()[0]=='2'*32


@pytest.mark.parametrize('path',['/admin','/admin/users','/admin/orders','/admin/products','/admin/payments',
    '/admin/stats','/admin/money','/admin/finance','/admin/traffic','/admin/bots','/admin/shopbot',
    '/admin/settings','/admin/pay-settings','/admin/catalog-sync','/admin/errors'])
def test_admin_source_pages_keep_authenticated_forms_and_layout(client,path):
    assert client.get(path,follow_redirects=False).status_code==303
    web_login(client,'admin@example.com','adminpass123')
    r=client.get(path)
    assert r.status_code==200,r.text[:300]
    assert 'main' in r.text and 'class="page-hero' in r.text
