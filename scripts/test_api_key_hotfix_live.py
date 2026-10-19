import hashlib, json, subprocess, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding='utf-8')

BASE = 'http://127.0.0.1'
REMOTE = 'abood@127.0.0.1'
CONTAINER = 'aboodvideo_api_keyhotfix'
STAGE = 'provision'
created = []
raw_keys = []
page_errors = []
console_errors = []
csp_violations = []
http_5xx = []
alert_dialogs = []
list_responses = []
admin = requests.Session()
regular = requests.Session()

provision = r'''import json,secrets,uuid
from datetime import datetime,timezone,timedelta
from app.core.database import SyncSessionLocal
from app.models.user import User,UserRole
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.core.config import settings
result={}
with SyncSessionLocal() as db:
 admin=db.query(User).filter(User.email=='admin_live@aboodvideo.local').one()
 regular=db.query(User).filter(User.role==UserRole.USER,User.is_active.is_(True)).first()
 if regular is None: raise RuntimeError('no regular user available for role acceptance')
 for label,user in [('admin',admin),('regular',regular)]:
  sid=uuid.uuid4(); token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
  row=UserSession(id=sid,user_id=user.id,token_hash=hash_token(token),ip_address='127.0.0.1',user_agent='api-key-hotfix-acceptance',expires_at=datetime.now(timezone.utc)+timedelta(minutes=30))
  db.add(row); result[label]={'id':str(sid),'user_id':str(user.id),'token':token,'csrf':csrf,'session_name':settings.SESSION_COOKIE_NAME,'csrf_name':settings.CSRF_COOKIE_NAME}
 db.commit()
print(json.dumps(result))
'''

def ssh_python(code):
    return subprocess.run(['ssh','-o','BatchMode=yes',REMOTE,'docker','exec','-i',CONTAINER,'python'],input=code,text=True,capture_output=True,check=True,timeout=30)

def set_session(session, creds):
    session.cookies.set(creds['session_name'], creds['token'], domain='127.0.0.1', path='/')
    session.cookies.set(creds['csrf_name'], creds['csrf'], domain='127.0.0.1', path='/')

def key_headers(raw):
    return {'Authorization': 'Bearer ' + raw}

def create_in_ui(page, name):
    with page.expect_response(lambda r: r.request.method == 'POST' and r.url == BASE + '/api/v1/admin/keys') as response_info:
        page.locator('#btnCreateApiKey').click()
        page.locator('#key-name').fill(name)
        for scope in ['packages:create','packages:write','packages:publish']:
            box = page.locator(f'.key-scope[value="{scope}"]')
            if not box.is_checked(): box.check(force=True)
        page.locator('#key-create-btn').click()
    response = response_info.value
    if response.status != 201: raise AssertionError('create status ' + str(response.status))
    data = response.json()
    raw = data['raw_key']; key_id = data['id']
    raw_keys.append(raw); created.append({'id':key_id,'name':name,'prefix':data['prefix']})
    if not raw.startswith('ak_live_'): raise AssertionError('create response format')
    if page.locator('#key-raw').input_value() != raw: raise AssertionError('one-time secret not shown in reveal field')
    if not page.locator('#key-result').is_visible(): raise AssertionError('one-time secret modal not visible')
    page.locator('[data-modal-close="key-result"]').first.click()
    if page.locator('#key-raw').input_value() != '': raise AssertionError('secret field not cleared on close')
    page.wait_for_timeout(250)
    return key_id, raw

def revoke_in_ui(page, name):
    row = page.get_by_role('row').filter(has_text=name)
    row.get_by_role('button', name='إلغاء المفتاح').click()
    page.locator('#keys-confirm-yes').click()
    page.get_by_role('row').filter(has_text=name).get_by_text('ملغى').wait_for(timeout=10000)
    if page.get_by_role('row').filter(has_text=name).get_by_role('button', name='حذف').count() != 1:
        raise AssertionError('revoked row delete action missing')

def api_list(session, csrf=None):
    headers = {'X-CSRF-Token':csrf} if csrf else {}
    return session.get(BASE+'/api/v1/admin/keys?include_test=true&limit=100',headers=headers,timeout=20)

stage = 'provision'
try:
    auth = json.loads(ssh_python(provision).stdout.strip().splitlines()[-1])
    set_session(admin, auth['admin']); set_session(regular, auth['regular'])
    csrf = auth['admin']['csrf']
    browser_errors_start = 0
    STAGE = stage = 'open_admin'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1000})
        context.add_cookies([
            {'name':auth['admin']['session_name'],'value':auth['admin']['token'],'domain':'127.0.0.1','path':'/','httpOnly':True},
            {'name':auth['admin']['csrf_name'],'value':csrf,'domain':'127.0.0.1','path':'/'}
        ])
        page = context.new_page()
        page.on('pageerror', lambda exc: page_errors.append(str(exc)[:180]))
        page.on('console', lambda msg: console_errors.append(msg.text[:180]) if msg.type == 'error' else None)
        page.on('console', lambda msg: csp_violations.append(msg.text[:180]) if 'Content Security Policy' in msg.text or 'violates the following' in msg.text else None)
        page.on('dialog', lambda d: (alert_dialogs.append(d.type), d.dismiss()))
        page.on('response', lambda r: http_5xx.append({'status':r.status,'path':r.url.split('?')[0]}) if r.status >= 500 else None)
        page.on('response', lambda r: list_responses.append(r.text()) if '/api/v1/admin/keys?' in r.url and r.request.method == 'GET' else None)
        response = page.goto(BASE+'/admin/api-keys',wait_until='domcontentloaded',timeout=30000)
        page.wait_for_selector('#apiKeysTable',timeout=15000)
        if response.status != 200: raise AssertionError('admin page status '+str(response.status))
        if '/admin/login' in page.url: raise AssertionError('admin session rejected')
        STAGE = stage = 'security_authorization'
        arbitrary = str(uuid.uuid4())
        STAGE = stage = 'unauthenticated_delete'
        if requests.delete(BASE+'/api/v1/admin/keys/'+arbitrary,timeout=20).status_code != 401: raise AssertionError('unauthenticated delete status')
        STAGE = stage = 'non_admin_delete'
        denied = regular.delete(BASE+'/api/v1/admin/keys/'+arbitrary,headers={'X-CSRF-Token':auth['regular']['csrf']},timeout=20)
        if denied.status_code != 403: raise AssertionError('non-admin delete status '+str(denied.status_code))
        STAGE = stage = 'csrf_required'
        no_csrf = admin.delete(BASE+'/api/v1/admin/keys/'+arbitrary,timeout=20)
        if no_csrf.status_code != 403: raise AssertionError('missing CSRF status '+str(no_csrf.status_code))
        STAGE = stage = 'malformed_id'
        invalid = admin.delete(BASE+'/api/v1/admin/keys/not-a-uuid',headers={'X-CSRF-Token':csrf},timeout=20)
        if invalid.status_code != 404: raise AssertionError('invalid ID status '+str(invalid.status_code))
        STAGE = stage = 'unknown_id'
        missing = admin.delete(BASE+'/api/v1/admin/keys/'+arbitrary,headers={'X-CSRF-Token':csrf},timeout=20)
        if missing.status_code != 404: raise AssertionError('unknown ID status '+str(missing.status_code)+' detail='+missing.text[:120])

        unique = uuid.uuid4().hex[:10]
        STAGE = stage = 'create_first'
        first_name = 'Lifecycle Hotfix ' + unique
        first_id, first_raw = create_in_ui(page,first_name)
        verify = requests.get(BASE+'/api/v1/studio/packages/verify',headers=key_headers(first_raw),timeout=20)
        if verify.status_code != 200: raise AssertionError('active key verify status '+str(verify.status_code))
        first_active_delete = admin.delete(BASE+'/api/v1/admin/keys/'+first_id,headers={'X-CSRF-Token':csrf},timeout=20)
        first_active_json = first_active_delete.json()
        if first_active_delete.status_code != 409 or first_active_json.get('detail',{}).get('code') != 'KEY_MUST_BE_REVOKED_FIRST': raise AssertionError('active delete conflict missing')
        if requests.get(BASE+'/api/v1/studio/packages/verify',headers=key_headers(first_raw),timeout=20).status_code != 200: raise AssertionError('active key changed by blocked delete')

        STAGE = stage = 'create_second'
        second_name = 'Lifecycle Bulk ' + unique
        second_id, second_raw = create_in_ui(page,second_name)
        STAGE = stage = 'revoke_second'
        revoke_in_ui(page,second_name)
        second_verify = requests.get(BASE+'/api/v1/studio/packages/verify',headers=key_headers(second_raw),timeout=20)
        if second_verify.status_code not in (401,403): raise AssertionError('revoked key still authenticates')
        mixed = admin.post(BASE+'/api/v1/admin/keys/bulk-delete',json={'key_ids':[first_id,second_id]},headers={'X-CSRF-Token':csrf},timeout=20)
        if mixed.status_code != 409: raise AssertionError('bulk mixed active/revoked not blocked')
        still_there = api_list(admin,csrf)
        if still_there.status_code != 200 or not any(k['id']==second_id for k in still_there.json()['keys']): raise AssertionError('bulk conflict partially deleted revoked row')
        STAGE = stage = 'bulk_delete'
        second_row = page.get_by_role('row').filter(has_text=second_name)
        second_row.locator('.key-select-checkbox').check()
        page.locator('#keys-selected-count').wait_for()
        if page.locator('#keys-selected-count').inner_text() != '1 مفتاح محدد': raise AssertionError('bulk selection count')
        page.locator('#keys-bulk-delete').click()
        if 'حذف 1 مفاتيح API الملغاة؟' not in page.locator('#bulkDeleteQuestion').inner_text(): raise AssertionError('bulk confirmation text')
        page.locator('#keys-bulk-yes').click()
        page.get_by_role('row').filter(has_text=second_name).wait_for(state='detached',timeout=10000)

        STAGE = stage = 'revoke_first'
        revoke_in_ui(page,first_name)
        after_revoke = requests.get(BASE+'/api/v1/studio/packages/verify',headers=key_headers(first_raw),timeout=20)
        if after_revoke.status_code not in (401,403): raise AssertionError('revoked key still authenticates')
        STAGE = stage = 'delete_first'
        first_row = page.get_by_role('row').filter(has_text=first_name)
        first_row.get_by_role('button',name='حذف').click()
        modal = page.locator('#keys-delete-confirm')
        modal.wait_for(state='visible')
        if page.locator('#deleteKeyName').inner_text() != first_name: raise AssertionError('delete confirmation name')
        if page.locator('#deleteKeyStatus').inner_text() != 'ملغى': raise AssertionError('delete confirmation status')
        if first_raw in modal.inner_text(): raise AssertionError('secret appeared in delete confirmation')
        page.locator('#keys-delete-yes').click()
        page.get_by_role('row').filter(has_text=first_name).wait_for(state='detached',timeout=10000)
        STAGE = stage = 'refresh_check'
        with page.expect_response(lambda r: '/api/v1/admin/keys?' in r.url and r.request.method=='GET' and r.status==200, timeout=15000) as refresh_info:
            page.reload(wait_until='domcontentloaded',timeout=30000)
        refreshed_data = refresh_info.value.json()
        if any(k['id'] in {first_id,second_id} for k in refreshed_data.get('keys',[])):
            raise AssertionError('deleted keys remain in refreshed list response')
        refreshed_text = refresh_info.value.text()
        for raw in raw_keys:
            if raw in refreshed_text: raise AssertionError('secret appeared in refreshed API response')
        if 'key_hash' in refreshed_text: raise AssertionError('hash field appeared in refreshed API response')
        page.wait_for_selector('#apiKeysTable',timeout=15000)
        body = page.locator('body').inner_text()
        if first_name in body or second_name in body: raise AssertionError('deleted disposable keys reappeared after refresh')
        for raw in raw_keys:
            if raw in body: raise AssertionError('secret appeared in page after refresh')
        for response_body in list_responses:
            for raw in raw_keys:
                if raw in response_body: raise AssertionError('secret appeared in API list response')
            if 'key_hash' in response_body: raise AssertionError('hash field appeared in API list response')
        if page_errors or console_errors or csp_violations or http_5xx or alert_dialogs:
            raise AssertionError('browser diagnostics nonzero')
        browser.close()

    STAGE = stage = 'audit_and_secret'
    audit_code = r'''import json
from app.core.database import SyncSessionLocal
from app.models.audit import AuditLog
ids=set(%s)
actor=%s
with SyncSessionLocal() as db:
 rows=db.query(AuditLog).filter(AuditLog.action.in_(['api_key.create','api_key.revoke','api_key.delete'])).all()
 matched=[r for r in rows if (r.metadata_json or {}).get('key_id') in ids]
 details_ok=all(all((r.metadata_json or {}).get(f) for f in ['key_id','name','prefix']) and str(r.user_id)==actor and r.created_at is not None for r in matched)
 print(json.dumps({'events':{a:sum(1 for r in matched if r.action==a) for a in ['api_key.create','api_key.revoke','api_key.delete']},'has_secret_field':any('key_hash' in (r.metadata_json or {}) or 'raw_key' in (r.metadata_json or {}) for r in matched),'details_ok':details_ok}))
''' % (repr([k['id'] for k in created]),repr(auth['admin']['user_id']))
    audit = json.loads(ssh_python(audit_code).stdout.strip().splitlines()[-1])
    if audit['events'].get('api_key.create',0) < 2 or audit['events'].get('api_key.revoke',0) < 2 or audit['events'].get('api_key.delete',0) < 2 or audit['has_secret_field'] or not audit['details_ok']:
        raise AssertionError('audit events incomplete or contain secret fields')

    log_probe = 'import subprocess,sys; p=subprocess.run([\'docker\',\'logs\',\'--since\',\'20m\',\''+CONTAINER+'\'],capture_output=True,text=True); sys.stdout.write(p.stdout+p.stderr)'
    logs = subprocess.run(['ssh','-o','BatchMode=yes',REMOTE,'docker','exec','-i',CONTAINER,'python','-c',log_probe],capture_output=True,text=True,timeout=30)
    if any(raw in logs.stdout for raw in raw_keys): raise AssertionError('raw key found in application logs')

    STAGE = stage = 'key_inventory'
    snap = json.loads(ssh_python(Path('release/api_key_hotfix_snapshot.py').read_text(encoding='utf-8-sig')).stdout.strip().splitlines()[-1])
    print(json.dumps({'result':'PASS','stages':['admin page','create once','active verify','active delete 409','unauth 401','non-admin 403','CSRF 403','invalid/missing ID 404','revoke authentication failure','individual delete','bulk all-or-none','bulk delete success','refresh absence'],'created_disposables':len(created),'audit':audit,'secret_leak_audit':'PASS','page_errors':len(page_errors),'console_errors':len(console_errors),'csp_violations':len(csp_violations),'http_5xx':len(http_5xx),'alert_dialogs':len(alert_dialogs),'inventory_after':snap},ensure_ascii=False))
except Exception as exc:
    safe_error = str(exc)
    for raw in raw_keys: safe_error = safe_error.replace(raw, '[redacted]')
    print(json.dumps({'result':'FAIL','stage':stage,'error_type':type(exc).__name__,'detail':safe_error[:180],'page_errors':len(page_errors),'console_errors':len(console_errors),'csp_violations':len(csp_violations),'http_5xx':len(http_5xx),'alerts':len(alert_dialogs)},ensure_ascii=False))
    sys.exit(1)
finally:
    # Best-effort cleanup for only the keys created by this script.
    for item in created:
        try:
            row = admin.get(BASE+'/api/v1/admin/keys?include_test=true&limit=100',timeout=15)
            if row.ok:
                key = next((k for k in row.json()['keys'] if k['id']==item['id']),None)
                if key:
                    if key['is_active']:
                        admin.post(BASE+f"/api/v1/admin/keys/{item['id']}/revoke",headers={'X-CSRF-Token':auth['admin']['csrf']},timeout=15)
                    admin.delete(BASE+f"/api/v1/admin/keys/{item['id']}",headers={'X-CSRF-Token':auth['admin']['csrf']},timeout=15)
        except Exception:
            pass
    if 'auth' in locals():
        cleanup = r'''import json,uuid
from app.core.database import SyncSessionLocal
from app.models.session import UserSession
ids=%r
with SyncSessionLocal() as db:
 db.query(UserSession).filter(UserSession.id.in_([uuid.UUID(x) for x in ids])).delete(synchronize_session=False); db.commit()
print('cleaned')
''' % [auth['admin']['id'],auth['regular']['id']]
        try: ssh_python(cleanup)
        except Exception: pass
