"""F01–F16: synthetic data, disposable PostgreSQL/Redis, no live services."""
import asyncio
import hashlib
import json
import threading
import time
import uuid
import io
import tarfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from redis.exceptions import ConnectionError as RedisConnectionError
from rq.exceptions import NoSuchJobError
from sqlalchemy import select, delete

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import get_session_client
from app.models.video import Video, VideoStatus, VideoVisibility, VideoRendition, PlaybackSession
from app.models.package import PackageUploadSession, PackageFile
from app.models.series import Series
from app.models.user import UserRole


def video(owner, **kw):
    return Video(id=uuid.uuid4(), user_id=owner, title="Synthetic episode", source_filename="fixture.mkv", **kw)


@pytest.mark.parametrize("rq_state", ["outage", "started", "missing", "finished"])
@pytest.mark.parametrize("package_state", ["VERIFYING", "PROMOTING"])
async def test_f01_reconciler_requires_positive_abandonment(monkeypatch, tmp_path, rq_state, package_state):
    from app.workers import reconciler as m
    record = SimpleNamespace(id=uuid.uuid4(), status=package_state, verification_job_id="fixture-job", updated_at=datetime.now(timezone.utc)-timedelta(minutes=10))
    db = AsyncMock()
    r1 = MagicMock(); r1.scalars.return_value.all.return_value = []
    r2 = MagicMock(); r2.scalars.return_value.all.return_value = [record]
    db.execute.side_effect = [r1, r2, r1]
    @asynccontextmanager
    async def context(): yield db
    monkeypatch.setattr(m, "get_db_context", context)
    monkeypatch.setattr(m.UploadService, "cleanup_expired_sessions", AsyncMock(return_value=0))
    clean = MagicMock(); monkeypatch.setattr(m.PackageStorageCleanup, "remove_terminal_staging", clean)
    def fetch(*a, **kw):
        if rq_state == "outage": raise RedisConnectionError("synthetic outage")
        if rq_state == "missing": raise NoSuchJobError("synthetic absence")
        return SimpleNamespace(get_status=lambda:rq_state)
    monkeypatch.setattr(m.Job, "fetch", fetch)
    await m.reconcile_stalled_package_jobs()
    if rq_state in {"outage", "started"}:
        assert record.status == package_state
        clean.assert_not_called()
    else:
        assert record.status == "FAILED"
        clean.assert_called_once_with(record)


async def test_f02_tabs_keep_identity_and_atomic_concurrency(db_session, test_user, monkeypatch):
    from app.api.watch_routes import _init_playback_session
    from app.security.playback_session import verify_playback_session
    monkeypatch.setattr(settings,"PLAYBACK_MAX_CONCURRENT_SESSIONS", 2)
    vids = [video(test_user.id,status=VideoStatus.READY,visibility=VideoVisibility.PUBLIC,active_generation_id=uuid.uuid4()) for _ in range(3)]
    db_session.add_all(vids); await db_session.commit()
    redis = get_session_client()
    request = SimpleNamespace(cookies={},headers={},client=SimpleNamespace(host="127.0.0.1"))
    token, cookie = await _init_playback_session(request,db_session,redis,vids[0],None)
    secret = cookie.split(";",1)[0].split("=",1)[1]
    request.cookies[settings.PLAYBACK_COOKIE_NAME] = secret
    token2, cookie2 = await _init_playback_session(request,db_session,redis,vids[1],None)
    assert cookie2.split(";",1)[0] == cookie.split(";",1)[0]
    ctx, reason = await verify_playback_session(db_session,redis,token[2:],secret)
    assert ctx and reason is None
    ctx2, reason = await verify_playback_session(db_session,redis,token2[2:],secret)
    assert ctx2 and reason is None
    token3, _ = await _init_playback_session(request,db_session,redis,vids[2],None)
    assert (await verify_playback_session(db_session,redis,token[2:],secret))[0] is None
    assert (await verify_playback_session(db_session,redis,token3[2:],secret))[0]
    for item in vids: await db_session.delete(item)
    await db_session.commit()


async def test_f03_detection_bulk_is_atomic_and_owner_scoped(db_session, test_user):
    from app.services.drive_service import DriveService
    from app.models.user import User
    other = User(id=uuid.uuid4(),email=f"f16-{uuid.uuid4()}@test.local",username=f"f16-{uuid.uuid4()}",hashed_password="synthetic",role=UserRole.OWNER)
    db_session.add(other); await db_session.flush()
    own, foreign = video(test_user.id), video(other.id)
    series = Series(id=uuid.uuid4(),owner_id=other.id,title="Other owner")
    db_session.add_all([own,foreign,series]); await db_session.commit()
    with pytest.raises(ValueError):
        await DriveService.apply_media_detection(db_session,test_user.id,[{"video_id":str(own.id),"suggested_title":"Changed"},{"video_id":str(foreign.id)}])
    assert own.title == "Synthetic episode"
    with pytest.raises(ValueError): await DriveService.create_season(db_session,series.id,1,owner_id=test_user.id)
    with pytest.raises(ValueError): await DriveService.detect_media(db_session,[foreign.id],owner_id=test_user.id)
    await DriveService.apply_media_detection(db_session,test_user.id,[{"video_id":str(foreign.id),"suggested_title":"Admin changed"}],is_admin=True)
    assert foreign.title == "Admin changed"
    await db_session.delete(own); await db_session.delete(other); await db_session.commit()


async def seed_package(db, owner, directory, contents):
    v = video(owner); db.add(v); await db.flush()
    sid = uuid.uuid4()
    directory = directory or Path(settings.STORAGE_PACKAGES_STAGING_PATH) / str(v.id) / str(sid)
    session = PackageUploadSession(id=sid,video_id=v.id,status="INITIALIZED",total_files=len(contents),total_bytes=sum(map(len,contents.values())),completed_bytes=0,manifest_data={},staging_path=str(directory),target_generation_id=uuid.uuid4(),expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    directory.mkdir(parents=True)
    db.add(session); await db.flush()
    files = []
    for path, data in contents.items():
        f = PackageFile(id=uuid.uuid4(),package_session_id=session.id,relative_path=path,expected_size=len(data),expected_sha256=hashlib.sha256(data).hexdigest(),media_type="application/octet-stream",status="PENDING")
        db.add(f); files.append(f)
    await db.commit()
    return v, session, files


@pytest.mark.parametrize("same_file",[True,False])
async def test_f04_f16_concurrent_files_exact_progress_and_completion(db_session,test_user,tmp_path,same_file):
    from app.services.package_service import PackageService
    contents = {"a.ts":b"a"*3000} if same_file else {"a.ts":b"a"*3000,"b.ts":b"b"*2000}
    v,s,files = await seed_package(db_session,test_user.id,tmp_path/"pkg",contents)
    async def send(f, delay):
        async def stream():
            data=contents[f.relative_path]; yield data[:100]
            await asyncio.sleep(delay); yield data[100:]
        async with AsyncSessionLocal() as db:
            return await PackageService.upload_file_chunk_stream(db,s.id,f.id,stream())
    await asyncio.gather(send(files[0],.05),send(files[0] if same_file else files[1],0))
    await db_session.refresh(s)
    assert s.completed_bytes == sum(map(len,contents.values()))
    assert s.status == "UPLOADED"
    for name,data in contents.items(): assert (tmp_path/"pkg"/name).read_bytes()==data
    assert not list((tmp_path/"pkg").glob("*.part"))
    await db_session.execute(delete(Video).where(Video.id == v.id)); await db_session.commit()


async def test_f05_blocking_io_keeps_event_loop_responsive():
    from app.utils.file_io import file_io
    task=asyncio.create_task(file_io(time.sleep,.15))
    pulses=0
    while not task.done():
        await asyncio.sleep(.01); pulses+=1
    await task
    assert pulses >= 5


async def test_f05_archive_finalize_does_not_block_health(db_session,test_user,tmp_path,monkeypatch):
    from app.services.package_service import PackageService
    from app.main import create_application
    root=tmp_path/"package";root.mkdir();manifest=synthetic_package(root)
    contents={x["path"]:(root/x["path"]).read_bytes() for x in manifest["files"]}
    v,s,_=await seed_package(db_session,test_user.id,None,contents)
    s.manifest_data=manifest;await db_session.commit()
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode="w:") as tar:
        for name,content in contents.items():
            info=tarfile.TarInfo(name);info.size=len(content);tar.addfile(info,io.BytesIO(content))
    data=data.getvalue();key=SimpleNamespace(id=None)
    await PackageService.archive_init(db_session,s.id,key,len(data),hashlib.sha256(data).hexdigest())
    await PackageService.append_package_archive(db_session,s.id,key,0,data)
    original=tarfile.open
    import threading
    entered=threading.Event();release=threading.Event()
    def slow(*a,**kw):
        entered.set()
        assert release.wait(10), 'Archive I/O blocked the event loop'
        return original(*a,**kw)
    monkeypatch.setattr(tarfile,"open",slow)
    task=asyncio.create_task(PackageService.finalize_package_archive(db_session,s.id,key))
    try:
        assert await asyncio.to_thread(entered.wait,10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_application()),base_url="http://test") as client:
            assert (await asyncio.wait_for(client.get("/health"),5)).status_code==200
        # Health must complete while the actual archive operation is held.
        # This proves concurrency without an unstable 300ms scheduler budget.
        assert not task.done() and not release.is_set()
    finally:
        release.set()
        result=await task  # Always drain DB work before fixture teardown.
    assert result["status"]=="UPLOADED"
    await db_session.execute(delete(Video).where(Video.id==v.id));await db_session.commit()


def test_f06_authorization_and_csp_use_same_domains():
    from app.services.embed_settings import parse_domains_string,build_frame_ancestors,is_embed_host_allowed
    domains=parse_domains_string("https://partner.example")
    assert domains == ["partner.example"]
    assert is_embed_host_allowed("sub.partner.example",domains)
    assert not is_embed_host_allowed("untrusted.example",domains)
    policy=build_frame_ancestors(domains)
    assert "https://*.partner.example" in policy and "untrusted.example" not in policy
    assert parse_domains_string("")==[]


@pytest.mark.parametrize("action",["pause","stop","cancel"])
def test_f07_manual_upload_honors_control_after_current(tmp_path,monkeypatch,action):
    from studio.core.batch_runner import BatchRunner
    from studio.core.batch_store import BatchStore
    monkeypatch.setattr(BatchRunner,"_report_ready_local_episodes",lambda self:None)
    store=BatchStore(tmp_path/"jobs.db"); b=store.create_batch(str(tmp_path/"series"))
    store.add_episodes(b["id"],[dict(source_path=str(tmp_path/f"{i}.mkv"),filename=f"{i}.mkv",order_index=i,state="READY_TO_UPLOAD") for i in (1,2)])
    runner=BatchRunner(store); calls=[]
    def upload(id,**kw):
        calls.append(id)
        if action=="pause": runner._pause_requested=True
        elif action=="stop": runner._stop_after_current_requested=True
        else: runner._current_upload_cancel_event.set()
    runner.upload_episode=upload
    runner._run_batch_upload([b["id"]])
    assert len(calls)==1
    runner.shutdown()


def test_f08_verification_preserves_transfer_bytes(tmp_path,monkeypatch):
    from studio.core import batch_runner as m
    from studio.core.batch_store import BatchStore
    monkeypatch.setattr(m.BatchRunner,"_report_ready_local_episodes",lambda self:None)
    monkeypatch.setattr(m,"load_studio_config",lambda:{"api_key":"fixture-only","server_url":"http://test/api/v1"})
    store=BatchStore(tmp_path/"jobs.db"); b=store.create_batch(str(tmp_path/"series"))
    work=tmp_path/"output";work.mkdir();(work/"manifest.json").write_text("{}")
    store.add_episodes(b["id"],[dict(source_path=str(tmp_path/"source.mkv"),filename="source.mkv",order_index=1,state="READY_TO_UPLOAD",work_dir=str(work),package_bytes=123)])
    runner=m.BatchRunner(store); runner._report_local_processing=lambda *a:None
    runner.inspect_local_package=lambda *a,**kw:{"compatible":True}
    def upload(**kw):
        cb=kw["progress_callback"]
        cb(dict(stage="uploading_files",uploaded_bytes=123,total_bytes=123,upload_progress_pct=100))
        cb(dict(stage="server_verifying",upload_progress_pct=100))
        assert runner._telemetry[store.get_episodes(b["id"])[0]["id"]]["uploaded_bytes"]==123
        return dict(success=True,video_id="video",session_id="session",status="READY",server_url="http://test")
    monkeypatch.setattr(m,"upload_and_verify_package",upload)
    runner.upload_episode(store.get_episodes(b["id"])[0]["id"])
    assert store.get_episodes(b["id"])[0]["uploaded_bytes"]==123
    runner.shutdown()


def synthetic_package(root):
    payload=b"synthetic segment"*100
    playlist=b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"
    files={"360p/index.m3u8":playlist,"360p/segment.ts":payload}
    for path,data in files.items():
        target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    manifest={"video":{"title":"Synthetic","duration_seconds":1},"renditions":[{"quality_label":"360p","width":640,"height":360,"video_bitrate_bps":700000,"audio_bitrate_bps":96000,"segment_count":1,"playlist_path":"360p/index.m3u8"}],"files":[{"path":p,"size_bytes":len(d),"sha256":hashlib.sha256(d).hexdigest()} for p,d in files.items()],"fonts":[],"subtitles":[]}
    (root/"manifest.json").write_text(json.dumps(manifest));return manifest


def test_f09_hashing_is_observable_and_cancellable(tmp_path):
    from studio.core.package_cleaner import build_final_package_inventory
    synthetic_package(tmp_path);event=threading.Event();seen=[]
    def progress(data):seen.append(data);event.set()
    with pytest.raises(RuntimeError,match="إلغاء"):
        build_final_package_inventory(tmp_path,verify_hashes=True,cancel_event=event,progress_callback=progress)
    assert seen[0]["stage"]=="hashing_package"


@pytest.mark.parametrize("condition",["disk","cancel"])
def test_f09_archive_checks_temp_space_and_cancels_preparation(tmp_path,monkeypatch,condition):
    from studio.core import uploader as m
    manifest=synthetic_package(tmp_path);event=threading.Event()
    if condition=="disk":monkeypatch.setattr(m.shutil,"disk_usage",lambda p:SimpleNamespace(free=0))
    def handle(request):
        if request.url.path.endswith("/init"):
            return httpx.Response(201,json={"session_id":"s","video_id":"v","total_files":2,"total_bytes":sum(x["size_bytes"] for x in manifest["files"]),"files":[{"file_id":str(i),"relative_path":x["path"],"expected_size":x["size_bytes"]} for i,x in enumerate(manifest["files"])]})
        raise AssertionError("preparation must finish before any archive upload")
    def progress(data):
        if condition=="cancel" and data["stage"]=="creating_archive":event.set()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises((RuntimeError,OSError)):
            m.upload_and_verify_package("http://test/api/v1","fixture",tmp_path,manifest,progress_callback=progress,cancel_event=event,http_client=client)


def test_f10_scheduler_bounds_both_resources_and_runs_parallel():
    from studio.core.scheduler import run_resource_scheduler
    episodes=[dict(id=str(i),kind="cpu" if i%2 else "gpu") for i in range(8)]
    counts={"cpu":0,"gpu":0};peaks=dict(counts);lock=threading.Lock()
    def work(ep):
        kind=ep["kind"]
        with lock:counts[kind]+=1;peaks[kind]=max(peaks[kind],counts[kind])
        time.sleep(.04)
        with lock:counts[kind]-=1
        return True
    results=run_resource_scheduler(lambda:episodes,work,lambda ep:ep["kind"],{"cpu":2,"gpu":1},lambda:False)
    assert len(results)==8 and peaks=={"cpu":2,"gpu":1}


def test_f10_server_processing_uses_resumable_source_api_without_ffmpeg(tmp_path):
    from studio.core.server_processing import process_on_server
    source=tmp_path/"fixture.mkv";source.write_bytes(b"synthetic only")
    saved={};requests=[];queued=False
    def handle(req):
        nonlocal queued
        requests.append(req.url.path); assert req.headers["host"]=="video.example"
        if req.url.path.endswith("/initiate"):return httpx.Response(201,json=dict(upload_id="u",video_id="v",chunk_size_bytes=100,total_chunks=1,status="INITIATED"))
        if req.url.path.endswith("/chunk"):return httpx.Response(200,json=dict(progress_percent=100))
        if req.url.path.endswith("/finalize"):return httpx.Response(200,json={})
        if req.url.path.endswith("/transcode"):queued=True;return httpx.Response(202,json={})
        if req.url.path.endswith("/status"):return httpx.Response(200,json=dict(status="READY" if queued else "UPLOADED",progress=100))
        raise AssertionError(req.url.path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        vid=process_on_server(source,"http://loopback/api/v1","fixture","video.example",{"title":"Synthetic"},{},lambda *a:None,threading.Event(),saved.update,client)
    assert vid=="v" and saved["upload_id"]=="u" and any(x.endswith("/transcode") for x in requests)


async def test_f11_probe_forwards_host_header(monkeypatch):
    from studio.web.routes import api as m
    seen=[]
    monkeypatch.setattr(m,"load_studio_config",lambda:{"server_url":"http://127.0.0.1/api/v1","server_host_header":"video.example","api_key":"fixture"})
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def get(self,url,headers):seen.append(headers);return SimpleNamespace(status_code=200)
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw:Client())
    def connection(*a,**kw):assert kw["host_header"]=="video.example";return {"ok":True}
    monkeypatch.setattr(m,"test_connection",connection)
    assert (await m.get_server_status())["connected"]
    assert seen==[{"Host":"video.example"},{"Host":"video.example"}]


async def test_f12_f13_period_visitors_and_ready_quality(db_session,test_user):
    from app.api.v1.admin_analytics import get_analytics_overview,get_video_analytics
    v=video(test_user.id,status=VideoStatus.READY,view_count=100,bandwidth_bytes=9999)
    db_session.add(v);await db_session.flush()
    now=datetime.now(timezone.utc)
    db_session.add_all([PlaybackSession(id=uuid.uuid4(),video_id=v.id,session_id=uuid.uuid4().hex,ip_hash="same-person",started_at=now-timedelta(days=days),last_heartbeat_at=now,duration_seconds=20,bytes_served=100,is_completed=True,quality="360p") for days in (0,0,10)])
    db_session.add(VideoRendition(id=uuid.uuid4(),video_id=v.id,quality_label="480p",resolution_width=854,resolution_height=480,video_bitrate=1000000,audio_bitrate=96000,video_codec="h264",audio_codec="aac",playlist_path="480p/index.m3u8",is_ready=True))
    await db_session.commit()
    overview=await get_analytics_overview(1,db_session,test_user)
    assert overview["kpis"]["views"]==2 and overview["kpis"]["unique_visitors"]==1
    assert overview["kpis"]["lifetime_views"]==100 and overview["kpis"]["bandwidth_bytes"]==200
    item=next(x for x in overview["top_videos"] if x["id"]==str(v.id))
    assert item["views"]==2 and item["visitors"]==1 and item["top_quality"]=="480p"
    details=await get_video_analytics(str(v.id),1,db_session,test_user)
    assert details["video"]["views"]==2 and details["video"]["unique_visitors"]==1
    await db_session.delete(v);await db_session.commit()


async def test_f15_legitimate_titles_visible_test_flag_hidden(db_session,test_user):
    from app.main import create_application
    from app.security.auth import require_owner_or_admin
    app=create_application();app.dependency_overrides[require_owner_or_admin]=lambda:test_user
    vids=[video(test_user.id,is_test_content=False) for _ in range(3)]
    for v,title in zip(vids,["The Expanse","Temple","Experiment"]):v.title=title
    marked=video(test_user.id,is_test_content=True);db_session.add_all([*vids,marked]);await db_session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.get("/api/v1/admin/videos",params={"include_test":"false"})
    assert response.status_code==200,response.text
    ids={x["id"] for x in response.json()["items"]}
    assert all(str(v.id) in ids for v in vids) and str(marked.id) not in ids
    for v in [*vids,marked]:await db_session.delete(v)
    await db_session.commit()


def test_f14_analytics_renders_injected_data_as_text():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    script=Path("backend/app/static/js/admin_analytics.js").read_text(encoding="utf-8")
    attack='<img src=x onerror="window.__injected=true"><b>payload</b>'
    payload={"kpis":{},"quality_distribution":[{"quality":attack,"percentage":100,"count":1}],"referrer_domains":[{"domain":attack,"views":1,"percentage":100}],"top_videos":[{"title":attack,"series_name":attack,"views":1,"visitors":1,"top_quality":attack,"bandwidth_formatted":"1 B","embed_url":'https://example.test/\" data-injected=\"yes'}]}
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        def route(r):
            if r.request.url.endswith("/analytics.js"):r.fulfill(content_type="text/javascript",body=script)
            elif "/api/" in r.request.url:r.fulfill(content_type="application/json",body=json.dumps(payload if "/overview" in r.request.url else {}))
            else:r.fulfill(content_type="text/html",body='<div id="kpi-views"></div><div id="kpi-visitors"></div><div id="kpi-bandwidth"></div><div id="kpi-top-quality"></div><div id="quality-bars-container"></div><div id="referrers-container"></div><table><tbody id="analytics-videos-tbody"></tbody></table><script src="/analytics.js"></script>')
        page.route("**/*",route);page.goto("http://isolated.test/")
        page.wait_for_selector("#analytics-videos-tbody tr")
        assert attack in page.locator("#analytics-videos-tbody").inner_text()
        assert page.locator("#analytics-videos-tbody img, [data-injected]").count()==0
        assert page.evaluate("window.__injected") is None
        browser.close()
