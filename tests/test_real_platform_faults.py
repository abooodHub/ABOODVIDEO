"""Physical ENOSPC and verified TLS against owned disposable Docker services."""
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import tarfile
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx
import pytest
from sqlalchemy import select

from real_platform_support import RealPlatform,ROOT,IMAGE,NETWORK,assert_owned,docker
from test_real_platform_integration import upload_source,wait_ready,make_real_package

pytestmark=pytest.mark.skipif(os.getenv("ABV_RUN_REAL_INTEGRATION")!="1",reason="Requires the disposable integration runner")


@pytest.fixture(scope="module")
def platform():
    service=RealPlatform()
    try:yield service.start()
    finally:service.close()


def wait_http(base,client=None):
    own=client is None
    client=client or httpx.Client(base_url=base,timeout=3)
    try:
        for _ in range(150):
            try:
                if client.get("/health").status_code==200:return
            except httpx.HTTPError:pass
            time.sleep(.15)
        raise AssertionError("Disposable HTTP service did not become healthy")
    finally:
        if own:client.close()


@contextmanager
def small_disk_api(platform):
    name="abv-real-e2e-enospc"
    args=platform.options()
    assert args[-2]=="--mount" and args[-1].endswith("dst=/storage")
    args=args[:-2]+["--mount","type=tmpfs,destination=/storage,tmpfs-size=67108864,tmpfs-mode=1777"]
    docker("run","-d","--name",name,*args,"-p","127.0.0.1:28081:8000",
           "-e","STORAGE_MIN_FREE_BYTES=0","-e","STORAGE_SAFETY_MARGIN_BYTES=0","-e","STORAGE_HARD_THRESHOLD_PERCENT=100",
           IMAGE,"python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--no-access-log")
    try:
        mounts=json.loads(docker("inspect","--format","{{json .Mounts}}",name))
        assert any(m["Destination"]=="/storage" and m["Type"]=="tmpfs" for m in mounts)
        wait_http("http://127.0.0.1:28081")
        docker("exec",name,"python","-c","from pathlib import Path;Path('/storage/untouched-fixture').write_bytes(b'keep this independent fixture')")
        yield name,"http://127.0.0.1:28081"
    finally:
        assert_owned(name)
        (platform.temp/(name+".log")).write_text(docker("logs",name),encoding="utf-8")
        docker("rm","-f",name)


def fill_tmpfs(name):
    assert_owned(name)
    script="import shutil;from pathlib import Path;p=Path('/storage/.fault-fill');remaining=shutil.disk_usage('/storage').free-8192;f=p.open('wb');block=b'x'*65536\nwhile remaining>0:\n n=min(remaining,len(block));f.write(block[:n]);remaining-=n\nf.close();print(shutil.disk_usage('/storage').free)"
    assert int(docker("exec",name,"python","-c",script))<=8192


def release_tmpfs(name):
    assert_owned(name)
    docker("exec",name,"python","-c","from pathlib import Path;Path('/storage/.fault-fill').unlink();assert Path('/storage/untouched-fixture').read_bytes()==b'keep this independent fixture'")


@pytest.mark.parametrize("phase",["source_chunk","source_finalize","package_file","archive_chunk","archive_finalize"])
def test_real_enospc_is_retryable_and_preserves_acknowledged_data(platform,tmp_path,phase):
    from app.models.upload import UploadSession
    from app.models.package import PackageUploadSession,PackageFile
    source=platform.synthetic(seconds=2,width=640,height=360)
    headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]}
    with small_disk_api(platform) as (name,base),httpx.Client(base_url=base,headers=headers,timeout=30) as c:
        if phase.startswith("source"):
            body=source.read_bytes();chunk_size=(len(body)+1)//2
            state=upload_source(SimpleNamespace(base=base,identities=platform.identities),source,chunk_size=chunk_size,interrupted=True)
            sid=state["upload_id"];path="/api/v1/studio/source-uploads/"+sid
            block=body[chunk_size:]
            if phase=="source_finalize":
                r=c.post(path+"/chunk",data={"chunk_index":1},files={"chunk_file":("chunk",block)});assert r.status_code==200
            with platform.session() as db:
                session=db.get(UploadSession,uuid.UUID(sid));old_bytes=session.bytes_received;temp_dir=session.temp_directory
            fill_tmpfs(name)
            if phase=="source_chunk":r=c.post(path+"/chunk",data={"chunk_index":1},files={"chunk_file":("chunk",block)})
            else:r=c.post(path+"/finalize")
            assert r.status_code==507,r.text
            with platform.session() as db:
                session=db.get(UploadSession,uuid.UUID(sid));assert session.bytes_received==old_bytes and session.status.value=="UPLOADING"
            leftovers=docker("exec",name,"python","-c",f"from pathlib import Path;p=Path({temp_dir!r});assert (p/'chunk_0.part').is_file();print(len(list(p.glob('.tmp*')))+len(list(p.parent.glob('*_assembled.tmp'))))")
            assert leftovers=="0"
            release_tmpfs(name)
            if phase=="source_chunk":
                r=c.post(path+"/chunk",data={"chunk_index":1},files={"chunk_file":("chunk",block)});assert r.status_code==200,r.text
            r=c.post(path+"/finalize");assert r.status_code==200 and r.json()["status"]=="UPLOADED",r.text
        else:
            package,manifest=make_real_package(platform,tmp_path,"Physical ENOSPC fixture")
            r=c.post("/api/v1/studio/packages/init",json={"manifest":manifest,"idempotency_key":hashlib.sha256(uuid.uuid4().bytes).hexdigest()});assert r.status_code==201,r.text
            state=r.json();sid=state["session_id"];path="/api/v1/studio/packages/"+sid
            with platform.session() as db:staging=db.get(PackageUploadSession,uuid.UUID(sid)).staging_path
            if phase=="package_file":
                item=max(state["files"],key=lambda item:item["expected_size"]);body=(package/item["relative_path"]).read_bytes()
                completed=min((entry for entry in state["files"] if entry["file_id"]!=item["file_id"]),key=lambda entry:entry["expected_size"])
                r=c.put(path+"/files/"+completed["file_id"],content=(package/completed["relative_path"]).read_bytes());assert r.status_code==200
                fill_tmpfs(name)
                r=c.put(path+"/files/"+item["file_id"],content=body);assert r.status_code==507,r.text
                with platform.session() as db:
                    assert db.get(PackageFile,uuid.UUID(item["file_id"])).received_bytes==0
                    assert db.get(PackageUploadSession,uuid.UUID(sid)).completed_bytes==completed["expected_size"]
                    assert db.get(PackageFile,uuid.UUID(completed["file_id"])).status=="COMPLETED"
                assert docker("exec",name,"python","-c",f"from pathlib import Path;print(len(list(Path({staging!r}).glob('*.part'))))")=="0"
                release_tmpfs(name)
                r=c.put(path+"/files/"+item["file_id"],content=body);assert r.status_code==200,r.text
            else:
                archive=io.BytesIO()
                with tarfile.open(fileobj=archive,mode="w") as tar:
                    for item in manifest["files"]:tar.add(package/item["path"],arcname=item["path"],recursive=False)
                body=archive.getvalue();r=c.post(path+"/archive/init",json={"size":len(body),"sha256":hashlib.sha256(body).hexdigest()});assert r.status_code==200
                r=c.put(path+"/archive/chunk?offset=0",content=body[:16384]);assert r.status_code==200
                if phase=="archive_chunk":
                    fill_tmpfs(name)
                    r=c.put(path+"/archive/chunk?offset=16384",content=body[16384:]);assert r.status_code==507,r.text
                    r=c.get(path+"/archive/status");assert r.status_code==200 and r.json()["offset"]==16384,r.text
                    release_tmpfs(name)
                    r=c.put(path+"/archive/chunk?offset=16384",content=body[16384:]);assert r.status_code==200,r.text
                else:
                    r=c.put(path+"/archive/chunk?offset=16384",content=body[16384:]);assert r.status_code==200
                    fill_tmpfs(name)
                    r=c.post(path+"/archive/finalize");assert r.status_code==507,r.text
                    r=c.get(path+"/archive/status");assert r.status_code==200 and r.json()["offset"]==len(body)
                    assert docker("exec",name,"python","-c",f"from pathlib import Path;print(len(list(Path({staging!r}).glob('.extract-*'))))")=="0"
                    release_tmpfs(name)
                r=c.post(path+"/archive/finalize");assert r.status_code==200,r.text
                with platform.session() as db:
                    session=db.get(PackageUploadSession,uuid.UUID(sid));assert session.completed_bytes==session.total_bytes


def test_real_public_https_secure_cookies_csrf_playback_and_proxy_boundary(platform):
    from datetime import datetime,timedelta,timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    api,nginx="abv-real-e2e-tls-api","abv-real-e2e-tls-nginx"
    created=[];folder=platform.temp/"tls";folder.mkdir()
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"ABOODVIDEO disposable localhost")])
    now=datetime.now(timezone.utc)
    certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
                 .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
                 .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),critical=False).sign(key,hashes.SHA256()))
    cert=folder/"certificate.pem";cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (folder/"key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    context=ssl.create_default_context(cafile=str(cert));assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    network=json.loads(docker("network","inspect",NETWORK))[0]
    subnet=ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"]);proxy=str(subnet.network_address+100)
    assert proxy not in {value["IPv4Address"].split("/")[0] for value in network["Containers"].values()}
    config=(ROOT/"nginx/conf.d/default.conf").read_text(encoding="utf-8").replace("listen 80;","listen 443 ssl;\n    ssl_certificate /fixture-tls/certificate.pem;\n    ssl_certificate_key /fixture-tls/key.pem;")
    (folder/"default.conf").write_text(config,encoding="utf-8")
    source=platform.synthetic(seconds=3,width=640,height=360);state=upload_source(platform,source)
    with platform.client() as owner:assert owner.post(f"/api/v1/admin/videos/{state['video_id']}/transcode").status_code==202
    wait_ready(platform,state["video_id"])
    try:
        docker("run","-d","--name",api,*platform.options(),"--network-alias","backend","-p","127.0.0.1:28083:8000",
               "-e","ENVIRONMENT=production","-e","SECURITY_MODE=public","-e","FORCE_HTTPS=true","-e","SESSION_COOKIE_SECURE=true",
               "-e","PUBLIC_BASE_URL=https://localhost:28443","-e","TRUSTED_PROXIES="+proxy,"-e","SERVE_MEDIA_VIA_NGINX=true",
               "-e","PLAYBACK_SESSION_TTL_SECONDS=4","-e","PLAYBACK_SLIDING_REFRESH_SECONDS=1",
               IMAGE,"python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--no-proxy-headers","--no-access-log")
        created.append(api)
        docker("run","-d","--name",nginx,"--network",NETWORK,"--ip",proxy,"--label","abv.purpose=real-integration","--label","abv.run="+os.getenv("ABV_INTEGRATION_RUN_ID","external-fixture"),"-p","127.0.0.1:28443:443",
               "--mount",f"type=bind,src={ROOT/'nginx/nginx.conf'},dst=/etc/nginx/nginx.conf,readonly",
               "--mount",f"type=bind,src={folder/'default.conf'},dst=/etc/nginx/conf.d/default.conf,readonly",
               "--mount",f"type=bind,src={folder},dst=/fixture-tls,readonly",
               *([] if os.getenv("ABV_INTEGRATION_NGINX_IMAGE") else ["--mount", f"type=bind,src={ROOT/'backend/app/static'},dst=/var/www/static,readonly"]),
               "--mount","type=volume,src=abv-real-e2e-storage,dst=/var/www/storage,readonly",os.getenv("ABV_INTEGRATION_NGINX_IMAGE", "nginx:1.27.1-alpine3.20"))
        created.append(nginx);docker("exec",nginx,"nginx","-t")
        with httpx.Client(base_url="https://localhost:28443",verify=context,timeout=30,follow_redirects=False) as c:
            wait_http("https://localhost:28443",c)
            r=c.get("/api/v1/auth/me",headers={"X-User-ID":str(platform.identities["OWNER"]["user"])});assert r.status_code==401
            r=c.post("/api/v1/auth/login",json={"username_or_email":platform.identities["OWNER"]["username"],"password":"Fixture-only-password-2026!"});assert r.status_code==200
            attributes={value.split("=",1)[0].lower():{part.strip().lower() for part in value.split(";")[1:]} for value in r.headers.get_list("set-cookie")}
            assert all("secure" in value and "samesite=strict" in value for value in attributes.values())
            assert any("httponly" in value for value in attributes.values())
            assert r.headers.get("strict-transport-security","").startswith("max-age=")
            csrf=r.json()["csrf_token"]
            for _ in range(3):
                assert c.get("/api/v1/auth/me").status_code==200
                assert c.get("/admin/videos").status_code==200
            # A forged forwarding header is overwritten by the known proxy.
            assert c.get("/api/v1/auth/me",headers={"X-Forwarded-Proto":"http"}).status_code==200
            payload={"title":"TLS CSRF fixture"}
            assert c.post("/api/v1/drive/series",json=payload).status_code==403
            assert c.post("/api/v1/drive/series",json=payload,headers={"X-CSRF-Token":"invalid"}).status_code==403
            assert c.post("/api/v1/drive/series",json=payload,headers={"X-CSRF-Token":csrf}).status_code==201
            page=c.get("/watch/"+state["video_id"]);assert page.status_code==200
            playback_attributes=[{part.strip().lower() for part in value.split(";")[1:]} for value in page.headers.get_list("set-cookie")]
            assert playback_attributes and all("secure" in value and "httponly" in value and "samesite=lax" in value for value in playback_attributes)
            token=re.search(r'/api/v1/playback/(s/[^/"\s]+)',page.text).group(1)
            master=c.get(f"/api/v1/playback/{token}/master.m3u8");assert master.status_code==200
            variant=next(line for line in master.text.splitlines() if line and not line.startswith("#"))
            playlist=c.get(urljoin(str(master.request.url),variant));assert playlist.status_code==200
            segment=next(line for line in playlist.text.splitlines() if line and not line.startswith("#"))
            assert c.get(urljoin(str(playlist.request.url),segment)).status_code==200
            # Real CookieJar expiry and real Redis TTL: active HTTPS playback
            # must survive the initial cookie lifetime without weakening attrs.
            for _ in range(6):
                time.sleep(1.05)
                refreshed=c.get(str(master.request.url))
                assert refreshed.status_code==200
                renewal=refreshed.headers.get_list("set-cookie")
                assert any("max-age=4" in value.lower() and "httponly" in value.lower()
                           and "secure" in value.lower() and "samesite=lax" in value.lower()
                           and "path=/" in value.lower() for value in renewal)
            with httpx.Client(verify=context) as anonymous:assert anonymous.get(str(master.request.url)).status_code in {401,403}
            from app.models.user import User
            from app.models.video import Video
            owner_id=platform.identities["OWNER"]["user"]
            with platform.session() as db:user=db.get(User,owner_id);user.is_active=False;db.commit()
            try:assert c.get(str(master.request.url)).status_code in {401,403}
            finally:
                with platform.session() as db:user=db.get(User,owner_id);user.is_active=True;db.commit()
            with platform.session() as db:video=db.get(Video,uuid.UUID(state["video_id"]));video.user_id=platform.identities["OTHER"]["user"];db.commit()
            try:assert c.get(str(master.request.url)).status_code==403
            finally:
                with platform.session() as db:video=db.get(Video,uuid.UUID(state["video_id"]));video.user_id=owner_id;db.commit()
            replay_headers={"Cookie":c.build_request("GET",str(master.request.url)).headers["Cookie"]}
            assert c.post("/api/v1/auth/logout",headers={"X-CSRF-Token":csrf}).status_code==200
            assert c.get("/api/v1/auth/me").status_code==401
            assert c.get(str(master.request.url)).status_code in {401,403}
            with httpx.Client(verify=context) as replay:
                replayed=replay.get(str(master.request.url),headers=replay_headers)
                assert replayed.status_code in {401,403}
        direct=httpx.get("http://127.0.0.1:28083/api/v1/auth/me",headers={"X-Forwarded-Proto":"https","X-User-ID":str(platform.identities["OWNER"]["user"])},follow_redirects=False)
        assert direct.status_code==307
    finally:
        for name in reversed(created):
            assert_owned(name);(platform.temp/(name+".log")).write_text(docker("logs",name),encoding="utf-8");docker("rm","-f",name)
