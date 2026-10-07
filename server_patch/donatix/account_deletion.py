"""Account erasure after settlement. No loss of unrefunded user funds."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import accounts, db, security
from .api import ApiError, api_user, _limit
from .deps import check_csrf, csrf_token, get_config, get_conn, render, session_user
from .money import fmt

router = APIRouter()


def init(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS account_deletions (user_id INTEGER PRIMARY KEY, '
                 'requested_at TEXT NOT NULL, completed_at TEXT, state TEXT NOT NULL, reason TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS account_erasure_files (kind TEXT NOT NULL,name TEXT NOT NULL, '
                 'after_ts REAL NOT NULL DEFAULT 0, PRIMARY KEY(kind,name))')


def blockers(conn, uid):
    user = accounts.get_user(conn, uid)
    if not user:
        return []
    reasons = []
    if user['role'] == 'admin':
        reasons.append('Администратор: сначала передайте управление сервисом другому владельцу.')
    if user['balance_micro'] != 0:
        reasons.append('Баланс ' + fmt(user['balance_micro']) + ' USD: сначала требуется расчёт и возврат остатка.')
    if conn.execute("SELECT 1 FROM orders WHERE user_id=? AND status IN ('processing','attention')", (uid,)).fetchone():
        reasons.append('Есть незавершённые заказы: сначала выдача или возврат.')
    if conn.execute("SELECT 1 FROM payments WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        reasons.append('Есть пополнения на проверке: сначала нужно завершить проверку.')
    if conn.execute("SELECT 1 FROM apple_identities WHERE user_id=? AND state<>'revoked'", (uid,)).fetchone():
        reasons.append('Разрешение Sign in with Apple будет автоматически отозвано перед удалением.')
    return reasons


def purge(conn, config, uid):
    """Called under an IMMEDIATE transaction, after re-reading blockers."""
    import time
    receipts = [r[0] for r in conn.execute('SELECT receipt_file FROM payments WHERE user_id=? AND receipt_file IS NOT NULL', (uid,))]
    for name in receipts:
        conn.execute("INSERT OR IGNORE INTO account_erasure_files (kind,name) VALUES ('receipt',?)", (name,))
    # Reserve IDs before deleting: delayed cleanup must never remove a new owner's bot.
    last_bot=conn.execute('SELECT COALESCE(MAX(id),0) FROM bots').fetchone()[0]
    db.set_setting(conn,'bots.id.sequence',str(max(last_bot,int(db.get_setting(conn,'bots.id.sequence','0')))))
    for row in conn.execute('SELECT id FROM bots WHERE user_id=?', (uid,)).fetchall():
        conn.execute("INSERT OR IGNORE INTO account_erasure_files (kind,name,after_ts) VALUES ('bot',?,?)", (str(row[0]),time.time()+30))
    from . import dcoin
    user=accounts.get_user(conn,uid)
    if user and user['dcoin']:
        current=dcoin.state(conn)
        units=min(max(0,user['dcoin']),current['supply'])
        # Forfeited coins move into reserve; other users' coin price does not change.
        if units:
            dcoin._point(conn,current['pool'],current['supply']-units,'account_erasure',reserve=current['reserve']+units)
    tg_ids = [r[0] for r in conn.execute('SELECT tg_id FROM support_links WHERE user_id=? UNION SELECT tg_id FROM shop_users WHERE user_id=?', (uid,uid))]
    devices = [r[0] for r in conn.execute('SELECT device_id FROM native_push_devices WHERE user_id=?', (uid,))]
    conn.execute('DELETE FROM native_push_outbox WHERE notification_id IN (SELECT id FROM notifications WHERE user_id=?)', (uid,))
    for device in devices:
        conn.execute('DELETE FROM native_push_outbox WHERE device_id=?', (device,))
        conn.execute('DELETE FROM native_push_platforms WHERE device_id=?', (device,))
    conn.execute('DELETE FROM native_push_devices WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM payment_msgs WHERE payment_id IN (SELECT id FROM payments WHERE user_id=?)', (uid,))
    conn.execute('DELETE FROM shop_watch WHERE (kind IN (\'order\',\'orders\') AND obj_id IN (SELECT id FROM orders WHERE user_id=?)) '
                 'OR (kind IN (\'pay\',\'payment\',\'payments\') AND obj_id IN (SELECT id FROM payments WHERE user_id=?))', (uid,uid))
    conn.execute('DELETE FROM referral_rewards WHERE referrer_id=? OR referred_id=? OR order_id IN (SELECT id FROM orders WHERE user_id=?)', (uid,uid,uid))
    conn.execute('DELETE FROM dcoin_ledger WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM payments WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM transactions WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM orders WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM bots WHERE user_id=?', (uid,))
    for table in ('api_keys','logins','notifications','push_subs','support_links','support_codes',
                  'support_link_codes','shop_users','support_tickets'):
        conn.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
    for tg_id in tg_ids:
        for table in ('support_history','support_memory','support_codes'):
            conn.execute(f'DELETE FROM {table} WHERE tg_id=?', (tg_id,))
    conn.execute('DELETE FROM visits WHERE user_id=?', (uid,))
    conn.execute('UPDATE users SET referred_by=NULL WHERE referred_by=?', (uid,))
    conn.execute('UPDATE transactions SET created_by=NULL WHERE created_by=?', (uid,))
    conn.execute('UPDATE payments SET resolved_by=NULL WHERE resolved_by=?', (uid,))
    for table in ('mobile_oauth','mobile_carts','apple_flows','apple_identities'):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            conn.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
    # Per-user settings (timezone, blocked bot eligibility, etc.) are not left behind.
    conn.execute('DELETE FROM settings WHERE key IN (?, ?, ?) OR key LIKE ?',
                 (f'tz.user.{uid}', f'bots.block.{uid}', f'bot_blocked:{uid}', f'user.{uid}.%'))
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    # No tombstone keyed only by user ID: SQLite may reuse a deleted ID.
    conn.execute('DELETE FROM account_deletions WHERE user_id=?', (uid,))
    from . import bots, cache
    cache.clear()
    if bots.RUNNER:
        bots.RUNNER.poke()
    return receipts


def cleanup_pending(conn,config):
    import shutil,time
    for row in conn.execute('SELECT kind,name FROM account_erasure_files WHERE after_ts<=?',(time.time(),)).fetchall():
        base=(config.db_path.parent / ('bots' if row['kind']=='bot' else 'receipts')).resolve()
        path=(base / row['name']).resolve()
        if path.parent != base:
            continue
        try:
            if row['kind']=='bot':
                if path.exists(): shutil.rmtree(path)
            else: path.unlink(missing_ok=True)
            conn.execute('DELETE FROM account_erasure_files WHERE kind=? AND name=?',(row['kind'],row['name']))
        except OSError:
            pass  # Persistent cleanup queue retries after permissions / transient I/O errors.


def finish_ready(conn, config):
    from . import apple_auth
    apple_auth.revoke_pending(conn, config)
    deleted = []
    for row in conn.execute("SELECT user_id FROM account_deletions WHERE state='waiting'").fetchall():
        with db.tx(conn):
            if not blockers(conn,row['user_id']):
                deleted.extend(purge(conn,config,row['user_id']))
    cleanup_pending(conn,config)


def request_deletion(conn, config, user):
    uid = user['id']
    if user['role'] == 'admin':
        raise ApiError('Удаление владельца сервиса требует передачи административных прав.', 'owner_account', 409)
    receipts = []
    with db.tx(conn):
        conn.execute("INSERT INTO account_deletions (user_id,requested_at,state) VALUES (?,?,'waiting') "
                     "ON CONFLICT(user_id) DO NOTHING", (uid,db.now()))
        from . import apple_auth
        apple_auth.queue_revocation(conn, uid)
        reasons = blockers(conn,uid)
        conn.execute('UPDATE logins SET ended_at=? WHERE user_id=? AND ended_at IS NULL', (db.now(),uid))
        conn.execute('UPDATE api_keys SET revoked_at=?,key_enc=NULL WHERE user_id=?', (db.now(),uid))
        conn.execute('UPDATE bots SET enabled=0,updated_at=? WHERE user_id=?', (db.now(),uid))
        conn.execute("UPDATE users SET status='blocked',webhook_url=NULL,webhook_secret=NULL WHERE id=?", (uid,))
        conn.execute('DELETE FROM native_push_platforms WHERE device_id IN (SELECT device_id FROM native_push_devices WHERE user_id=?)', (uid,))
        conn.execute('DELETE FROM native_push_devices WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM push_subs WHERE user_id=?', (uid,))
        if reasons:
            conn.execute('UPDATE account_deletions SET reason=? WHERE user_id=?', (' '.join(reasons),uid))
        else:
            receipts = purge(conn,config,uid)
    cleanup_pending(conn,config)
    from . import bots
    if bots.RUNNER:
        bots.RUNNER.poke()
    return {'ok':True,'state':'waiting' if reasons else 'completed','reasons':reasons,
            'message':'Заявка принята. После завершения расчётов данные удалятся автоматически.' if reasons else 'Аккаунт и связанные данные удалены.'}


def authenticate(request,conn,user,password):
    if not request.session.get('sid') or request.session.get('user_id') != user['id']:
        raise ApiError('Удаление доступно только после входа в аккаунт.', 'session_required', 403)
    if password:
        if not security.verify_password(password,user['password_hash']):
            raise ApiError('Неверный пароль.', 'reauth_required', 403)
        return
    from datetime import datetime, timezone
    row = conn.execute('SELECT created_at FROM logins WHERE sid=? AND user_id=? AND ended_at IS NULL',
                       (request.session['sid'],user['id'])).fetchone()
    recent = row and (datetime.now(timezone.utc)-datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)).total_seconds() <= 300
    if not recent:
        raise ApiError('Для подтверждения введите пароль или выйдите и войдите снова через Google / Apple.', 'reauth_required', 403)


class DeleteIn(BaseModel):
    confirmation: str = Field(max_length=40)
    password: str = Field(default='',max_length=512)


@router.get('/api/v1/mobile/account/deletion')
def deletion_info(user=Depends(api_user),conn=Depends(get_conn),config=Depends(get_config)):
    return {'ok':True,'balance_usd':fmt(user['balance_micro']),'blockers':blockers(conn,user['id']),
            'support_contact':config.support_contact,'web_url':config.base_url+'/account-deletion',
            'erased':'Профиль, входы, ключи API, боты, уведомления, чеки, заказы и история операций.',
            'coins':user['dcoin']}


@router.post('/api/v1/mobile/account/deletion')
def deletion_submit(body:DeleteIn,request:Request,user=Depends(api_user),conn=Depends(get_conn),config=Depends(get_config)):
    _limit(request,'login','delete:'+str(user['id']))
    if body.confirmation != 'УДАЛИТЬ':
        raise ApiError('Введите УДАЛИТЬ для подтверждения.', 'confirmation_required')
    authenticate(request,conn,user,body.password)
    result=request_deletion(conn,config,user)
    request.session.clear()
    return result


@router.get('/account-deletion')
def deletion_page(request:Request,conn=Depends(get_conn)):
    user=session_user(request,conn)
    return render(request,'account_deletion.html',{'user':user,'reasons':blockers(conn,user['id']) if user else [],
                                                  'done':request.query_params.get('done')})


@router.post('/account-deletion',dependencies=[Depends(check_csrf)])
def deletion_web(request:Request,confirmation:str=Form(''),password:str=Form(''),conn=Depends(get_conn),config=Depends(get_config)):
    user=session_user(request,conn)
    if not user:
        return RedirectResponse('/login?next=/account-deletion',303)
    _limit(request,'login','delete:'+str(user['id']))
    if confirmation != 'УДАЛИТЬ':
        raise ApiError('Введите УДАЛИТЬ.', 'confirmation_required')
    authenticate(request,conn,user,password)
    result=request_deletion(conn,config,user)
    request.session.clear()
    return RedirectResponse('/account-deletion?done='+result['state'],303)


@router.get('/admin/account-deletions')
def deletion_admin(request:Request,conn=Depends(get_conn)):
    from .admin import admin_user
    admin = admin_user(request,conn)
    rows=conn.execute("SELECT d.*,u.login,u.balance_micro FROM account_deletions d JOIN users u ON u.id=d.user_id "
                      "WHERE d.state='waiting' ORDER BY d.requested_at").fetchall()
    return render(request,'admin/account_deletions.html',{'user':admin,'rows':rows})
