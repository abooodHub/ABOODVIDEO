"""Production capabilities exercised over real TCP against owned staging."""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from real_platform_support import RealPlatform
from app.models.user import User, UserRole
from app.models.audit import AuditLog
from app.models.video import TranscodeJob, Video

pytestmark = pytest.mark.skipif(os.getenv('ABV_RUN_REAL_INTEGRATION') != '1', reason='Owned isolated real stack required')


@pytest.fixture(scope='module')
def platform():
    service = RealPlatform()
    try:
        yield service.start()
    finally:
        service.close()


def test_users_audit_permissions_and_native_admin_pages(platform):
    with platform.client('OWNER') as owner, platform.client('ADMIN') as admin:
        for path in ['/admin/users', '/admin/audit', '/admin/storage']:
            response = owner.get(path)
            assert response.status_code == 200, response.text
        legacy_system = owner.get('/admin/system')
        assert legacy_system.status_code == 302
        assert legacy_system.headers['location'] == '/admin#system-diagnostics'
        assert 'id="system-diagnostics"' in owner.get('/admin').text
        created = owner.post('/api/v1/admin/users', json={
            'email': 'merged-'+uuid.uuid4().hex+'@example.test', 'username': 'merged_'+uuid.uuid4().hex[:12],
            'password': 'Disposable-account-password-2026!', 'role': 'VIP'})
        assert created.status_code == 201, created.text
        identity = created.json(); user_id = identity['id']
        assert identity['role'] == 'VIP'
        assert owner.get('/api/v1/admin/users', params={'q': identity['username']}).json()['total'] == 1
        assert admin.patch('/api/v1/admin/users/'+user_id+'/role', json={'role':'ADMIN'}).status_code == 403
        assert owner.patch('/api/v1/admin/users/'+user_id+'/role', json={'role':'USER'}).status_code == 200
        audit = owner.get('/api/v1/admin/audit', params={'target':'user'})
        assert audit.status_code == 200
        assert len([row for row in audit.json()['items'] if row['target_id']==user_id]) >= 2
        assert 'password' not in json.dumps(audit.json()).lower()
        csrf = owner.headers.pop('X-CSRF-Token')
        assert owner.patch('/api/v1/admin/users/'+user_id+'/status', json={'active':False}).status_code == 403
        owner.headers['X-CSRF-Token'] = csrf
        with httpx.Client(base_url=platform.base, timeout=30) as viewer:
            login=viewer.post('/api/v1/auth/login', json={'username_or_email':identity['username'],'password':'Disposable-account-password-2026!'})
            assert login.status_code == 200
            assert viewer.get('/api/v1/admin/users').status_code == 403
            assert owner.patch('/api/v1/admin/users/'+user_id+'/status', json={'active':False}).status_code == 200
            assert viewer.get('/api/v1/auth/me').status_code == 401


def test_storage_metrics_and_health_are_protected_and_non_destructive(platform):
    with platform.client() as owner:
        before=owner.get('/api/v1/admin/storage/summary')
        assert before.status_code == 200, before.text
        assert before.json()['storage_cleanup_automatic'] is False
        assert owner.get('/api/v1/health/metrics').status_code == 200
        assert owner.get('/api/v1/health/live').status_code == 200
        assert owner.get('/api/v1/health/reconciler').status_code in {200,503}
    with httpx.Client(base_url=platform.base) as guest:
        for path in ['/api/v1/admin/storage/summary','/api/v1/admin/audit','/api/v1/health/metrics','/api/v1/health/db']:
            assert guest.get(path).status_code == 401


def test_external_identity_replay_using_real_redis(monkeypatch):
    import asyncio
    import redis.asyncio as redis
    from app.core.config import settings
    from app.security.external_identity import ExternalIdentityError, verify_external_identity
    secret='Disposable-external-HMAC-material-2026!'
    def encode(value):return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).rstrip(b'=').decode()
    now=int(time.time());header=encode({'alg':'HS256','typ':'JWT'})
    claims=encode({'iss':'isolated-issuer','aud':'isolated-audience','sub':'synthetic-user','iat':now,'exp':now+120,'jti':uuid.uuid4().hex,'plan':'VIP','entitlements':{'max_quality':'1080p','download':False}})
    signature=base64.urlsafe_b64encode(hmac.new(secret.encode(),(header+'.'+claims).encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
    token=header+'.'+claims+'.'+signature
    async def run():
        async with redis.from_url(settings.REDIS_SESSION_URL) as client:
            kwargs=dict(secret=secret,issuer='isolated-issuer',audience='isolated-audience',replay_store=client)
            assert (await verify_external_identity(token,**kwargs))['plan']=='VIP'
            with pytest.raises(ExternalIdentityError,match='replay'):
                await verify_external_identity(token,**kwargs)
            with pytest.raises(ExternalIdentityError):
                await verify_external_identity(token[:-1]+('A' if token[-1]!='A' else 'B'),**kwargs)
    asyncio.run(run())


def test_immutable_job_plan_preserves_local_source_api_and_recovery(platform):
    from test_real_platform_integration import upload_source, wait_ready
    source=platform.synthetic(seconds=4,width=640,height=360)
    video=upload_source(platform,source,title='Unified snapshot episode')
    with platform.client() as owner:
        response=owner.post('/api/v1/studio/source-uploads/videos/'+video['video_id']+'/transcode', headers={'Authorization':'Bearer '+platform.identities['OWNER']['key']})
        assert response.status_code == 202,response.text
        job_id=response.json()['job_id']
    with platform.session() as db:
        job=db.get(TranscodeJob,uuid.UUID(job_id));assert job.configuration_snapshot['schema_version']==1
        assert job.configuration_snapshot['encoder']['threads']==1
        assert job.configuration_snapshot['enabled_qualities']==['360p','480p','720p']
    final,_=wait_ready(platform,video['video_id'])
    assert final['status']=='READY'


def test_reconciler_same_generation_and_real_maintenance_heartbeat(platform):
    from real_platform_support import docker, IMAGE
    name='abv-real-e2e-unified-reconciler'
    docker('run','-d','--name',name,*platform.options(),IMAGE,'python','-m','app.workers.reconciler','--interval','30')
    platform.created.append(name)
    with platform.client() as owner:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            response=owner.get('/api/v1/health/reconciler')
            if response.status_code==200:break
            time.sleep(.2)
        assert response.status_code==200,response.text
        assert response.json()['status']=='online'
    images={docker('inspect','--format','{{.Image}}',n) for n in [platform.api_name,*platform.workers,name]}
    assert len(images)==1
    log=docker('logs',name)
    assert 'Error in reconciliation loop' not in log,log


def test_observed_production_models_can_read_forward_schema(platform):
    from real_platform_support import docker, ROOT, IMAGE
    preserved=ROOT/'release/unified-reconciliation-20261016/preserved/aboodvideo_backend/app'
    assert preserved.is_dir()
    script="from sqlalchemy import create_engine,select,func;from sqlalchemy.orm import Session;from app.core.config import settings;from app.models.video import Video,TranscodeJob;from app.models.user import User;from app.models.audit import AuditLog;e=create_engine(settings.DATABASE_URL_SYNC);s=Session(e);assert not hasattr(Video,'is_test_content');assert s.scalar(select(func.count(User.id)))>=3;s.execute(select(Video).limit(5)).scalars().all();s.execute(select(TranscodeJob.configuration_snapshot).limit(5)).all();s.execute(select(AuditLog.request_id).limit(5)).all();print('ORIGINAL_PRODUCTION_MODELS_FORWARD_SCHEMA=PASS');s.close();e.dispose()"
    output=docker('run','--rm',*platform.options(),'--mount',f'type=bind,src={preserved},dst=/app/app,readonly',IMAGE,'python','-B','-c',script)
    assert 'ORIGINAL_PRODUCTION_MODELS_FORWARD_SCHEMA=PASS' in output


def test_queued_snapshot_does_not_follow_later_quality_setting(platform):
    from real_platform_support import docker
    from test_real_platform_integration import upload_source, wait_ready
    from app.models.video import VideoRendition
    state=upload_source(platform,platform.synthetic(seconds=3,width=1280,height=720))
    for worker in platform.workers:docker('stop',worker)
    try:
        with platform.client() as owner:
            assert owner.put('/api/v1/admin/quality-settings',json={'enabled_qualities':['360p','480p','720p']}).status_code==200
            job=owner.post('/api/v1/admin/videos/'+state['video_id']+'/transcode')
            assert job.status_code==202,job.text
            assert owner.put('/api/v1/admin/quality-settings',json={'enabled_qualities':['720p']}).status_code==200
    finally:
        for worker in platform.workers:docker('start',worker)
    try:
        wait_ready(platform,state['video_id'])
        with platform.session() as db:
            qualities=set(db.execute(select(VideoRendition.quality_label).where(VideoRendition.video_id==uuid.UUID(state['video_id']))).scalars())
            assert qualities=={'360p','480p','720p'},qualities
    finally:
        with platform.client() as owner:owner.put('/api/v1/admin/quality-settings',json={'enabled_qualities':['360p','480p','720p']})


def test_trash_all_buttons_include_every_real_server_page_and_keep_media(platform):
    from datetime import datetime, timezone
    from playwright.sync_api import sync_playwright
    from app.models.video import VideoStatus
    ids=[uuid.uuid4() for _ in range(105)]
    with platform.session() as db:
        db.add_all([Video(id=identity,user_id=platform.identities['OWNER']['user'],title='Paged trash '+str(index),source_filename='synthetic.mp4',status=VideoStatus.UPLOADED,trashed_at=datetime.now(timezone.utc)) for index,identity in enumerate(ids)])
        db.commit()
    with platform.client() as owner, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        context=browser.new_context()
        context.add_cookies([{'name':c.name,'value':c.value,'url':platform.base} for c in owner.cookies.jar])
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(platform.base+'/admin/videos?filter=trash',wait_until='networkidle')
        page.wait_for_function("()=>window.__abood_drive_state?.currentData?.total_items===105")
        assert page.locator('#driveNextPage').is_enabled()
        page.locator('#btnRestoreAllTrash').click()
        page.wait_for_function("()=>window.__abood_drive_state?.currentData?.total_items===0")
        with platform.session() as db:
            rows=db.execute(select(Video).where(Video.id.in_(ids))).scalars().all()
            assert len(rows)==105 and all(row.trashed_at is None for row in rows)
            for row in rows:row.trashed_at=datetime.now(timezone.utc)
            db.commit()
        page.reload(wait_until='networkidle')
        page.wait_for_function("()=>window.__abood_drive_state?.currentData?.total_items===105")
        page.locator('#driveNextPage').click()
        page.wait_for_function("()=>window.__abood_drive_state?.page===2 && window.__abood_drive_state?.visibleItems?.length===5")
        page.locator('#selectAllCheckbox').check()
        page.locator('#btnMultiRestore').click()
        page.wait_for_function("()=>window.__abood_drive_state?.page===1 && window.__abood_drive_state?.visibleItems?.length===100 && window.__abood_drive_state?.currentData?.total_items===100")
        with platform.session() as db:
            for row in db.execute(select(Video).where(Video.id.in_(ids))).scalars():row.trashed_at=datetime.now(timezone.utc)
            db.commit()
        page.reload(wait_until='networkidle')
        page.wait_for_function("()=>window.__abood_drive_state?.currentData?.total_items===105")
        page.locator('#btnEmptyTrash').click()
        page.wait_for_function("()=>window.__abood_drive_state?.pendingPermanentDeleteItems?.length===105")
        # Confirmation is prepared for all 105, but no permanent deletion is
        # triggered in this browser check; existing deletion-safety tests apply.
        assert not errors,errors
        browser.close()
