"""iOS regressions with synthetic Apple keys and blocked external networking."""
import hashlib
import json
import socket
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from conftest import make_client, web_login
from donatix import account_deletion, accounts, apple_auth, db, native_push, notify, security


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('External network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


@pytest.fixture
def apple_keys(monkeypatch, tmp_path):
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk.update(kid='synthetic-test-key', alg='RS256', use='sig')
    monkeypatch.setattr(apple_auth, 'fetch_keys', lambda *args, **kwargs: [jwk])
    private = ec.generate_private_key(ec.SECP256R1())
    key_file = tmp_path / 'synthetic-not-owner-key.p8'
    key_file.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for name, value in {'CLIENT_ID':'tj.donatix.app','TEAM_ID':'TESTTEAM01','KEY_ID':'TESTKEY001','PRIVATE_KEY_FILE':str(key_file)}.items():
        monkeypatch.setenv('DONATIX_APPLE_' + name, value)
    def sign(expected_nonce, subject='apple-subject', email='apple@example.com', **overrides):
        claims = {'iss':apple_auth.ISSUER,'aud':'tj.donatix.app','iat':int(time.time()),'exp':int(time.time())+300,
                  'sub':subject,'nonce':expected_nonce,'email':email,'email_verified':'true'}
        claims.update(overrides)
        return jwt.encode(claims, rsa_key, algorithm='RS256', headers={'kid':jwk['kid']})
    return sign


def start(client, headers=None, link=False):
    verifier = 'v' * 64
    response = client.post('/api/v1/mobile/apple/prepare', json={
        'challenge':hashlib.sha256(verifier.encode()).hexdigest(),'link_account':link}, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json(), verifier


def claim(client, flow, verifier, token, headers=None):
    return client.post('/api/v1/mobile/apple/claim', json={
        'ticket':flow['ticket'],'verifier':verifier,'identity_token':token,
        'authorization_code':'synthetic-authorization-code'}, headers=headers or {})


def test_apple_flow_verifies_signature_nonce_and_preserves_user_after_relogin(client, conn, config, apple_keys, monkeypatch):
    monkeypatch.setattr(apple_auth, 'exchange', lambda *args, **kwargs: 'synthetic-refresh-token')
    flow, verifier = start(client)
    token = apple_keys(flow['nonce'])
    response = claim(client, flow, verifier, token)
    assert response.status_code == 200 and response.json()['verification'] is False, response.text
    user = conn.execute('SELECT * FROM users WHERE email=?', ('apple@example.com',)).fetchone()
    stored = conn.execute('SELECT * FROM apple_identities').fetchone()
    assert stored['user_id'] == user['id']
    assert security.unseal(config.secret_key, stored['refresh_enc']) == 'synthetic-refresh-token'
    assert 'synthetic-refresh-token' not in stored['refresh_enc']
    assert client.get('/api/v1/mobile-session').json()['user_id'] == user['id']
    assert claim(client, flow, verifier, token).status_code == 410
    client.cookies.clear()
    second, verifier = start(client)
    assert claim(client, second, verifier, apple_keys(second['nonce'])).status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM apple_identities').fetchone()[0] == 1


@pytest.mark.parametrize('overrides', [ {'aud':'different.app'}, {'iss':'https://attacker.example'},
    {'exp':1}, {'nonce':'different'}, {'sub':''} ])
def test_apple_invalid_claims_rejected(apple_keys, overrides):
    token = apple_keys('expected', **overrides)
    with pytest.raises(Exception) as error:
        apple_auth.verify(token, 'expected')
    assert error.value.code == 'apple_invalid'


def test_apple_untrusted_signature_and_hmac_algorithm_rejected(apple_keys):
    other = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    token = jwt.encode({'nonce':'expected'},other,algorithm='RS256',headers={'kid':'synthetic-test-key'})
    with pytest.raises(Exception): apple_auth.verify(token,'expected')
    token = jwt.encode({'nonce':'expected'},'a'*32,algorithm='HS256',headers={'kid':'synthetic-test-key'})
    with pytest.raises(Exception): apple_auth.verify(token,'expected')


def test_apple_flow_wrong_verifier_and_expiration(client, conn, apple_keys, monkeypatch):
    monkeypatch.setattr(apple_auth,'exchange',lambda *args: pytest.fail('Invalid flow reached code exchange'))
    flow, verifier = start(client)
    token = apple_keys(flow['nonce'])
    assert claim(client,flow,'wrong-verifier'*5,token).status_code == 410
    conn.execute('UPDATE apple_flows SET expires=0 WHERE id=?',(flow['ticket'],))
    assert claim(client,flow,verifier,token).status_code == 410


def test_apple_email_collision_requires_authenticated_linking(client, conn, apple_keys, monkeypatch):
    uid,_ = make_client(conn,'existing',balance='0')
    monkeypatch.setattr(apple_auth,'exchange',lambda *args: 'synthetic-refresh-token')
    flow,verifier=start(client)
    assert claim(client,flow,verifier,apple_keys(flow['nonce'],email='existing@example.com')).status_code == 409
    assert not conn.execute('SELECT 1 FROM apple_identities').fetchone()
    web_login(client,'existing@example.com','password123')
    h={'X-CSRF-Token':client.get('/api/v1/mobile-session').json()['csrf']}
    flow,verifier=start(client,h,link=True)
    assert claim(client,flow,verifier,apple_keys(flow['nonce'],email='existing@example.com'),h).status_code == 200
    assert conn.execute('SELECT user_id FROM apple_identities').fetchone()[0] == uid
    assert client.get('/api/v1/mobile-session').json()['user_id'] == uid


def test_apple_link_requires_csrf_live_and_recent_session(client, conn, apple_keys):
    assert client.post('/api/v1/mobile/apple/prepare',json={'challenge':'a'*64,'link_account':True}).status_code == 403
    uid,_=make_client(conn,'existing',balance='0')
    web_login(client,'existing@example.com','password123')
    assert client.post('/api/v1/mobile/apple/prepare',json={'challenge':'a'*64,'link_account':True}).status_code == 403
    h={'X-CSRF-Token':client.get('/api/v1/mobile-session').json()['csrf']}
    conn.execute("UPDATE logins SET created_at='2000-01-01T00:00:00' WHERE user_id=?",(uid,))
    assert client.post('/api/v1/mobile/apple/prepare',json={'challenge':'a'*64,'link_account':True},headers=h).status_code == 403


def test_apple_closed_registration_and_blocked_account(client, conn, apple_keys, monkeypatch):
    monkeypatch.setattr(apple_auth,'exchange',lambda *args: 'synthetic-refresh-token')
    monkeypatch.setattr(__import__('donatix.sitecfg',fromlist=['registration_open']),'registration_open',lambda conn:False)
    flow,verifier=start(client)
    assert claim(client,flow,verifier,apple_keys(flow['nonce'])).status_code == 403
    assert not conn.execute('SELECT 1 FROM users WHERE email=?',('apple@example.com',)).fetchone()
    uid,_=make_client(conn,'blockedapple',balance='0',status='blocked')
    conn.execute('INSERT INTO apple_identities (subject,user_id,refresh_enc) VALUES (?,?,?)',('apple-subject',uid,'unused'))
    flow,verifier=start(client)
    monkeypatch.setattr(apple_auth,'exchange',lambda *args: pytest.fail('Blocked account exchanged Apple code'))
    assert claim(client,flow,verifier,apple_keys(flow['nonce'])).status_code == 403


def test_apple_login_preserves_admin_second_factor(client, conn, config, apple_keys, monkeypatch):
    admin=conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.execute('INSERT INTO apple_identities (subject,user_id,refresh_enc) VALUES (?,?,?)',('apple-subject',admin,'unused'))
    monkeypatch.setattr(apple_auth,'exchange',lambda *args: 'synthetic-refresh-token')
    from donatix import sitecfg, worker
    monkeypatch.setattr(sitecfg,'admin_2fa_active',lambda *args:True)
    monkeypatch.setattr(worker,'notify_admin',lambda *args,**kwargs:None)
    flow,verifier=start(client)
    response=claim(client,flow,verifier,apple_keys(flow['nonce']))
    assert response.status_code==200 and response.json()['verification'] is True
    assert client.get('/api/v1/mobile-session').status_code==401
    assert not conn.execute('SELECT 1 FROM logins WHERE user_id=? AND ended_at IS NULL',(admin,)).fetchone()


def test_apple_code_exchange_verifies_returned_identity_and_explicit_key(apple_keys):
    sent=[]
    def handler(request):
        sent.append(request)
        assert request.url == apple_auth.ISSUER+'/auth/token'
        return httpx.Response(200,json={'id_token':apple_keys('nonce'), 'refresh_token':'synthetic-refresh-token'})
    assert apple_auth.exchange('synthetic-code','nonce','apple-subject',httpx.MockTransport(handler))=='synthetic-refresh-token'
    assert len(sent)==1
    with pytest.raises(Exception): apple_auth.exchange('synthetic-code','nonce','different-subject',httpx.MockTransport(handler))


def test_apple_deletion_waits_for_revocation_and_retries_without_losing_funds(client, conn, config, apple_keys):
    uid,_=make_client(conn,'appledelete',balance='0')
    conn.execute('INSERT INTO apple_identities (subject,user_id,refresh_enc) VALUES (?,?,?)',('subject',uid,security.seal(config.secret_key,'synthetic-refresh-token')))
    result=account_deletion.request_deletion(conn,config,accounts.get_user(conn,uid))
    assert result['state']=='waiting' and accounts.get_user(conn,uid)['status']=='blocked'
    seen=[]
    def failure(request): return httpx.Response(503)
    apple_auth.revoke_pending(conn,config,httpx.MockTransport(failure),now=100)
    row=conn.execute('SELECT * FROM apple_identities').fetchone()
    assert row['state']=='pending' and row['attempts']==1 and row['error']=='HTTPStatusError'
    def success(request):
        seen.append(request)
        assert request.url == apple_auth.ISSUER+'/auth/revoke'
        assert b'synthetic-refresh-token' in request.content
        return httpx.Response(200)
    apple_auth.revoke_pending(conn,config,httpx.MockTransport(success),now=200)
    assert len(seen)==1 and conn.execute('SELECT state FROM apple_identities').fetchone()[0]=='revoked'
    account_deletion.finish_ready(conn,config)
    assert not accounts.get_user(conn,uid)
    assert not conn.execute('SELECT 1 FROM apple_identities').fetchone()


def test_ios_push_is_visible_generic_and_bound_to_session(client, conn, config):
    uid,_=make_client(conn,'iospush',balance='0')
    web_login(client,'iospush@example.com','password123')
    h={'X-CSRF-Token':client.get('/api/v1/mobile-session').json()['csrf']}
    result=client.post('/api/v1/mobile/push/register',headers=h,json={
        'device_id':'i'*32,'token':'synthetic-ios-token-0001','platform':'ios'})
    assert result.status_code==200 and len(result.json()['binding'])==64
    notify.notify(conn,None,uid,'PRIVATE ORDER AND BALANCE','/panel/orders')
    calls=[]
    native_push.dispatch(conn,config,lambda cfg,token,payload:calls.append(payload),now=100)
    assert calls[0]['platform']=='ios' and calls[0]['binding']==result.json()['binding']
    assert 'body' not in calls[0] and 'title' not in calls[0]
    assert 'PRIVATE' not in json.dumps(calls)


def test_repeated_push_registration_preserves_pending_messages(conn, client, config):
    uid,_=make_client(conn,'pushrepeat',balance='0')
    web_login(client,'pushrepeat@example.com','password123')
    sid=conn.execute('SELECT sid FROM logins WHERE user_id=?',(uid,)).fetchone()[0]
    native_push.register(conn,config,uid,sid,'x'*32,'synthetic-ios-token-0002','ios')
    notify.notify(conn,None,uid,'pending')
    native_push.register(conn,config,uid,sid,'x'*32,'synthetic-ios-token-0002','ios')
    assert conn.execute('SELECT COUNT(*) FROM native_push_outbox').fetchone()[0]==1
    native_push.register(conn,config,uid,sid,'x'*32,'synthetic-ios-token-0002','android')
    assert conn.execute('SELECT COUNT(*) FROM native_push_outbox').fetchone()[0]==0


def test_ios_platform_migrates_existing_device_table_without_losing_tokens(conn, config):
    conn.execute('DROP TABLE native_push_devices')
    conn.execute('CREATE TABLE native_push_devices (device_id TEXT PRIMARY KEY,user_id INTEGER,sid TEXT,token_enc TEXT,token_hash TEXT UNIQUE,updated_at TEXT)')
    conn.execute("INSERT INTO native_push_devices VALUES ('example',1,'sid','cipher','hash','date')")
    native_push.init(conn)
    native_push.init(conn)
    row=conn.execute('SELECT * FROM native_push_devices').fetchone()
    assert row['token_enc']=='cipher'
    assert len(conn.execute('PRAGMA table_info(native_push_devices)').fetchall())==6
    # Old v3 registrations still work after restoring the previous Python modules.
    conn.execute("INSERT INTO native_push_devices VALUES ('v3',1,'sid','cipher2','hash2','date')")


def test_sender_constructs_apns_alert_without_private_content(config, monkeypatch, tmp_path):
    import firebase_admin
    from firebase_admin import credentials, messaging
    from firebase_admin._messaging_encoder import MessageEncoder
    path=tmp_path/'synthetic-firebase.json'
    path.write_text('{}')
    monkeypatch.setenv('DONATIX_FIREBASE_CREDENTIALS',str(path))
    monkeypatch.setattr(native_push,'_app',None)
    monkeypatch.setattr(native_push,'_app_path',None)
    monkeypatch.setattr(credentials,'Certificate',lambda path:object())
    monkeypatch.setattr(firebase_admin,'initialize_app',lambda *args,**kwargs:object())
    sent=[]
    monkeypatch.setattr(messaging,'send',lambda message,**kwargs:sent.append(message))
    native_push.send(config,'synthetic-ios-token',{'platform':'ios','user_id':1,'notification_id':2,'binding':'b'*64,'link':'/panel/notifications'})
    encoded=MessageEncoder().encode(sent[0])
    assert 'Новое уведомление' in sent[0].apns.payload.aps.alert.body
    assert sent[0].apns.headers['apns-push-type']=='alert'
    assert sent[0].apns.headers['apns-priority']=='10'
    assert 'body' not in sent[0].data and 'platform' not in sent[0].data
    assert 'apns' in encoded
    native_push.send(config,'synthetic-android-token',{'platform':'android','body':'Android private text','notification_id':3,'user_id':1})
    assert sent[1].android.priority=='high' and sent[1].data['body']=='Android private text'


def test_disabled_apple_feature_is_not_advertised(client, monkeypatch):
    for key in ('CLIENT_ID','TEAM_ID','KEY_ID','PRIVATE_KEY_FILE'):
        monkeypatch.delenv('DONATIX_APPLE_'+key,raising=False)
    config=client.get('/api/v1/mobile/config').json()
    assert config['version']==4 and config['platforms']==['android','ios'] and config['apple_enabled'] is False
    assert client.post('/api/v1/mobile/apple/prepare',json={'challenge':'a'*64}).status_code==503
