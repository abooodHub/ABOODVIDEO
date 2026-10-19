"""Opt-in real Windows RC lifecycle against owned, loopback-only HTTPS staging."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
import uuid
from urllib.parse import urlparse

import httpx
import psutil
import pytest
from sqlalchemy import text

from real_platform_support import RealPlatform, ROOT, NETWORK, IMAGE, VOLUME, docker, assert_owned

pytestmark = pytest.mark.skipif(os.getenv('ABV_RUN_REAL_INTEGRATION') != '1' or not os.getenv('ABV_STUDIO_RC_ROOT'),
                               reason='Requires explicit packaged RC and owned disposable Docker staging')


def wait_for(condition, timeout=90):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        value = condition()
        if value:
            return value
        time.sleep(.3)
    raise AssertionError('Packaged release condition timed out')


def test_actual_packaged_studio_https_lifecycle_and_recovery():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from playwright.sync_api import sync_playwright

    release = Path(os.environ['ABV_STUDIO_RC_ROOT']).resolve()
    evidence = release / 'evidence'
    if evidence.exists():
        evidence = release / ('evidence-retry-' + uuid.uuid4().hex[:8])
    evidence.mkdir(exist_ok=False)
    (release/'ACTIVE_EVIDENCE_PATH.txt').write_text(str(evidence))
    bundle = (release / 'candidate/portable/ABOODVIDEO-Studio').resolve()
    exe = bundle / 'ABOODVIDEO Studio.exe'
    assert exe.is_file() and ROOT in release.parents
    assert docker('context', 'show') == 'desktop-linux'
    profile = evidence / 'profile'; data = profile / 'ABOODVIDEO Studio'; data.mkdir(parents=True)
    output = evidence / 'converted'; output.mkdir()
    fonts = evidence / 'fonts'; fonts.mkdir()
    api = 'https://localhost:28443'; studio = 'http://127.0.0.1:28610'
    assert urlparse(api).hostname == 'localhost' and urlparse(studio).hostname == '127.0.0.1'
    # Preseed before launch: the real production default is never contacted.
    config = {'server_url': api + '/api/v1', 'api_key': '', 'converted_output_folder': str(output),
              'font_library_folder': str(fonts), 'first_run_complete': True, 'include_360p': True,
              'processing_mode': 'local', 'default_encoder': 'gpu', 'default_preset': 'balanced'}
    (data / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    manifest = json.loads(Path(os.environ['ABV_STAGING_MEDIA_MANIFEST']).read_text())
    item = next(i for i in manifest['media'] if i['id'] == 'hevc-mp4')
    source = Path(item['path']); source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert source_hash == item['sha256']
    platform = RealPlatform(); proc = None; workers_stopped = False; result = {'steps': []}
    tls_api, nginx = 'abv-real-e2e-rc-tls-api', 'abv-real-e2e-rc-tls-nginx'
    try:
        platform.start()
        from app.models.video import Video, VideoStatus
        from app.models.package import PackageUploadSession
        from app.models.api_key import ApiKey
        from sqlalchemy import select
        sentinel_video=uuid.uuid4();sentinel_package=uuid.uuid4()
        with platform.session() as db:
            key=db.scalar(select(ApiKey.id).where(ApiKey.user_id==platform.identities['OTHER']['user']))
            db.add(Video(id=sentinel_video,user_id=platform.identities['OTHER']['user'],title='Preserved disposable history',source_filename='sentinel.mp4',status=VideoStatus.READY))
            db.flush()
            db.add(PackageUploadSession(id=sentinel_package,video_id=sentinel_video,created_by_key_id=key,status='READY',total_files=0,total_bytes=0,manifest_data={},staging_path='/storage/synthetic-preservation-sentinel',target_generation_id=uuid.uuid4(),expires_at=datetime.now(timezone.utc)+timedelta(days=1)))
            db.commit()
        folder = evidence / 'tls'; folder.mkdir()
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ABOODVIDEO disposable release staging')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
                .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
                .sign(private, hashes.SHA256()))
        ca = folder / 'certificate.pem'; ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (folder / 'key.pem').write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        spki = base64.b64encode(hashlib.sha256(private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode()
        verify = ssl.create_default_context(cafile=str(ca))
        network = json.loads(docker('network', 'inspect', NETWORK))[0]
        proxy = str(ipaddress.ip_network(network['IPAM']['Config'][0]['Subnet']).network_address + 100)
        conf = (ROOT/'nginx/conf.d/default.conf').read_text(encoding='utf-8').replace('listen 80;', 'listen 443 ssl;\n ssl_certificate /fixture-tls/certificate.pem;\n ssl_certificate_key /fixture-tls/key.pem;')
        (folder/'default.conf').write_text(conf, encoding='utf-8')
        docker('run', '-d', '--name', tls_api, *platform.options(), '--network-alias', 'backend',
               '-e', 'ENVIRONMENT=production', '-e', 'SECURITY_MODE=public', '-e', 'FORCE_HTTPS=true',
               '-e', 'SESSION_COOKIE_SECURE=true', '-e', 'PUBLIC_BASE_URL='+api, '-e', 'TRUSTED_PROXIES='+proxy,
               '-e', 'SERVE_MEDIA_VIA_NGINX=true', IMAGE, 'python', '-m', 'uvicorn', 'app.main:app',
               '--host', '0.0.0.0', '--port', '8000', '--no-proxy-headers', '--no-access-log')
        platform.created.append(tls_api)
        docker('run', '-d', '--name', nginx, '--network', NETWORK, '--ip', proxy,
               '--label', 'abv.purpose=real-integration', '--label', 'abv.run='+os.environ['ABV_INTEGRATION_RUN_ID'],
               '-p', '127.0.0.1:28443:443', '--mount', f'type=bind,src={ROOT/"nginx/nginx.conf"},dst=/etc/nginx/nginx.conf,readonly',
               '--mount', f'type=bind,src={folder/"default.conf"},dst=/etc/nginx/conf.d/default.conf,readonly',
               '--mount', f'type=bind,src={folder},dst=/fixture-tls,readonly',
               *([] if os.getenv("ABV_INTEGRATION_NGINX_IMAGE") else ["--mount", f'type=bind,src={ROOT/"backend/app/static"},dst=/var/www/static,readonly']),
               '--mount', f'type=volume,src={VOLUME},dst=/var/www/storage,readonly', os.getenv('ABV_INTEGRATION_NGINX_IMAGE', 'nginx:1.27.1-alpine3.20'))
        platform.created.append(nginx); docker('exec', nginx, 'nginx', '-t')
        with httpx.Client(base_url=api, verify=verify, timeout=30) as owner:
            def healthy():
                try:return owner.get('/readiness').status_code == 200
                except httpx.HTTPError:return False
            wait_for(healthy)
            login = owner.post('/api/v1/auth/login', json={'username_or_email':platform.identities['OWNER']['username'], 'password':'Fixture-only-password-2026!'})
            assert login.status_code == 200
            owner.headers['X-CSRF-Token'] = login.json()['csrf_token']
            assert any('secure' in v.lower() and 'httponly' in v.lower() for v in login.headers.get_list('set-cookie'))
            assert owner.post('/api/v1/drive/series', json={'title':'No CSRF'}, headers={'X-CSRF-Token':'invalid'}).status_code == 403
            result['steps'].append('verified TLS hostname/CA, HTTPS login, Secure/HttpOnly cookies and CSRF')
            env = dict(os.environ, LOCALAPPDATA=str(profile), APPDATA=str(profile/'roaming'), USERPROFILE=str(profile/'home'),
                       HOME=str(profile/'home'), SSL_CERT_FILE=str(ca), ENVIRONMENT='test',
                       PATH=os.path.join(os.environ['WINDIR'],'System32')+';'+os.environ['WINDIR'])
            for key in ['PYTHONPATH', 'PYTHONHOME', 'ABOODVIDEO_FFMPEG_BIN_DIR']:env.pop(key, None)
            for key in ['APPDATA','USERPROFILE']:Path(env[key]).mkdir(parents=True, exist_ok=True)
            def launch():
                return subprocess.Popen([str(exe), '--port', '28610'], env=env, cwd=evidence,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def terminate():
                if proc is None:return
                try:
                    children = psutil.Process(proc.pid).children(recursive=True)
                    for child in reversed(children):
                        try:child.terminate()
                        except psutil.Error:pass
                except psutil.Error:pass
                if proc.poll() is None:proc.terminate();proc.wait(30)
            proc = launch()
            with httpx.Client(base_url=studio, timeout=60, headers={'Origin':studio}, limits=httpx.Limits(keepalive_expiry=2)) as app:
                def running():
                    try:return app.get('/api/config').status_code == 200
                    except httpx.HTTPError:return False
                wait_for(running)
                cfg=app.get('/api/config').json();assert Path(cfg['appdata_root']).resolve() == data.resolve()
                assert cfg['server_url'] == api+'/api/v1' and not cfg['has_api_key']
                assert app.post('/api/config', json={'api_key':platform.identities['OWNER']['key']}).status_code == 200
                assert app.get('/api/server/status').json()['authenticated']
                assert app.post('/api/test-connection', json={'server_url':api+'/api/v1','api_key':'invalid-disposable-fixture-key'}).json()['ok'] is False
                assert app.post('/api/probe', json={'video_path':str(evidence/'missing.mp4')}).status_code >= 400
                mapped=[]
                for process in [psutil.Process(proc.pid),*psutil.Process(proc.pid).children(recursive=True)]:
                    try:mapped += [m.path for m in process.memory_maps() if m.path.lower().endswith('python312.dll')]
                    except psutil.Error:pass
                assert any(str(bundle).casefold() in p.casefold() for p in mapped)
                result['bundled_python_loaded'] = True
                result['steps'].append('clean frozen executable launch with host Python/FFmpeg paths removed; settings and negative error cases')
                (evidence/'native-ready.json').write_text(json.dumps({'pid':proc.pid,'exe':str(exe),'url':studio,'profile':str(profile)}))
                if os.getenv('ABV_NATIVE_UI_REVIEW') == '1':
                    wait_for(lambda:(evidence/'native-ui-complete.flag').exists(), timeout=300)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True, args=['--disable-features=AudioFocusEnforcement,HardwareMediaKeyHandling', '--ignore-certificate-errors-spki-list='+spki])
                    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    assert page.goto(studio,wait_until='networkidle').status == 200
                    page.locator('[data-go="import"]:visible').first.click()
                    page.locator('#source-path').fill(str(source));page.locator('#use-source-path').click()
                    page.wait_for_function('()=>!document.querySelector("#queue-selection").disabled',timeout=30000)
                    page.locator('#import-encoder').select_option('gpu');page.locator('#include-360p').check()
                    page.locator('#queue-selection').click()
                    wait_for(lambda:len(app.get('/api/batch/current').json().get('episodes',[]))==1)
                    page.locator('#page-queue [data-action="start-all"]').click()
                    def encoded_child():
                        try:return next((p for p in psutil.Process(proc.pid).children(recursive=True) if p.name().lower()=='ffmpeg.exe' and str(bundle).casefold() in p.exe().casefold()),None)
                        except psutil.Error:return None
                    encoder=wait_for(encoded_child);result['bundled_ffmpeg_executed']=True
                    page.locator('#page-queue [data-action="pause-all"]').click()
                    def paused():
                        d=app.get('/api/batch/current').json()
                        # A configured worker remains alive while paused. The
                        # invariant is no active encoder, not thread death.
                        return (d['runner']['pause_requested'] and d['runner']['active_encodes_count']==0
                                and d['episodes'][0].get('current_stage')=='PAUSED')
                    wait_for(paused,60)
                    page.locator('#page-queue [data-action="resume-all"]').click()
                    def ready_local():
                        d=app.get('/api/batch/current').json();ep=d['episodes'][0]
                        assert ep['state']!='FAILED',ep.get('error')
                        return ep if ep['state']=='READY_TO_UPLOAD' else None
                    ep=wait_for(ready_local,300)
                    metadata=json.loads((Path(ep['work_dir'])/'manifest.json').read_text(encoding='utf-8'))
                    assert '360p' in {r['quality_label'] for r in metadata['renditions']}
                    result['steps'].append('real HEVC import/scan -> bundled FFmpeg encode -> actual pause/resume -> packaged renditions READY_TO_UPLOAD')
                    for worker in platform.workers:assert_owned(worker);docker('stop',worker)
                    workers_stopped=True
                    page.locator('[data-go="upload"]:visible').first.click()
                    page.wait_for_function('()=>!document.querySelector("#upload-ready").disabled')
                    page.locator('#upload-ready').click()
                    wait_for(lambda:app.get('/api/batch/current').json()['episodes'][0]['state']=='SERVER_VERIFYING',120)
                    with platform.engine.connect() as db:
                        original=db.execute(text('select id,video_id,request_fingerprint from package_upload_sessions where created_by_key_id in (select id from api_keys where user_id=:owner)'), {'owner':platform.identities['OWNER']['user']}).one()
                    page.close();terminate();time.sleep(2)
                    proc=launch();wait_for(running)
                    restored=app.get('/api/batch/current').json()['episodes'];assert len(restored)==1 and restored[0]['state']=='READY_TO_UPLOAD'
                    assert Path(restored[0]['work_dir'])==Path(ep['work_dir'])
                    page=browser.new_page();assert page.goto(studio,wait_until='networkidle').status==200
                    page.locator('[data-go="upload"]:visible').first.click()
                    page.wait_for_function('()=>!document.querySelector("#upload-ready").disabled')
                    page.locator('#upload-ready').click()
                    for worker in platform.workers:docker('start',worker)
                    workers_stopped=False
                    def ready_server():
                        e=app.get('/api/batch/current').json()['episodes'][0];assert e['state']!='FAILED',e.get('error');return e if e['state']=='READY' else None
                    final=wait_for(ready_server,180)
                    with platform.engine.connect() as db:
                        recovered=db.execute(text('select id,video_id,request_fingerprint from package_upload_sessions where created_by_key_id in (select id from api_keys where user_id=:owner)'), {'owner':platform.identities['OWNER']['user']}).one()
                        assert db.execute(text('select count(*) from package_upload_sessions where id=:sentinel'), {'sentinel':sentinel_package}).scalar_one()==1
                        assert db.execute(text('select count(*) from videos where id=:sentinel'), {'sentinel':sentinel_video}).scalar_one()==1
                        assert original == recovered
                        assert db.execute(text('select count(*) from videos where user_id=:owner'), {'owner':platform.identities['OWNER']['user']}).scalar_one()==1
                    result['steps'].append('HTTPS upload -> queued verification -> real executable crash/relaunch -> explicit resume, same session/video/fingerprint -> READY')
                    viewer=browser.new_context()
                    viewer.add_cookies([{'name':c.name,'value':c.value,'url':api,'secure':True} for c in owner.cookies.jar])
                    tab=viewer.new_page();assert tab.goto(api+'/watch/'+str(recovered.video_id),wait_until='networkidle').status==200
                    tab.evaluate('document.querySelector("video").muted=true');tab.locator('#big-play-btn').click()
                    tab.wait_for_function('()=>document.querySelector("video").currentTime>1',timeout=60000)
                    duration=tab.evaluate('document.querySelector("video").duration')
                    tab.evaluate('t=>document.querySelector("video").currentTime=t',duration-4)
                    tab.wait_for_function('()=>document.querySelector("video").ended',timeout=30000)
                    assert not errors,errors
                    result.update(PASS=True,version=json.loads((bundle / 'RELEASE.json').read_text(encoding='utf-8'))['version'],exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                                  real_fixture_seconds=duration,source_sha256_unchanged=hashlib.sha256(source.read_bytes()).hexdigest()==source_hash,
                                  qualities=[r['quality_label'] for r in metadata['renditions']],same_session_video_fingerprint=True,
                                  javascript_errors=errors,https_browser_playback=True,external_staging='NOT_AVAILABLE',
                                  browser_tls='Dedicated test certificate SPKI pin; HTTP client verifies CA and hostname, no global trust-store changes')
                    browser.close()
            terminate()
    except BaseException as exc:
        result.update(PASS=False,error_type=type(exc).__name__,error=str(exc)[:1500]);raise
    finally:
        (evidence/'PACKAGED_WORKFLOW.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        if proc is not None:
            try:
                for child in reversed(psutil.Process(proc.pid).children(recursive=True)):
                    try:child.terminate()
                    except psutil.Error:pass
            except psutil.Error:pass
            if proc.poll() is None:proc.terminate();proc.wait(30)
        if workers_stopped:
            for worker in platform.workers:assert_owned(worker);docker('start',worker)
        platform.close()
