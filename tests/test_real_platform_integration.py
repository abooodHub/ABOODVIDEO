"""Real Linux RQ workers and FFmpeg over TCP HTTP; no core dependency mocks."""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
from sqlalchemy import select

from real_platform_support import RealPlatform,docker,assert_owned,IMAGE

pytestmark=pytest.mark.skipif(os.getenv("ABV_RUN_REAL_INTEGRATION")!="1",reason="Requires labelled disposable real integration stack")


@pytest.fixture(scope="module")
def platform():
    service=RealPlatform()
    try:yield service.start()
    finally:service.close()


def upload_source(platform,path,title="Synthetic episode",chunk_size=65536,interrupted=False,**metadata):
    base=platform.base+"/api/v1/studio/source-uploads"
    headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]}
    content=path.read_bytes()
    with httpx.Client(timeout=30,headers=headers) as c:
        response=c.post(base+"/initiate",json={"title":title,"filename":path.name,"file_size_bytes":len(content),"chunk_size_bytes":chunk_size,"expected_sha256":hashlib.sha256(content).hexdigest(),**metadata})
        assert response.status_code==201,response.text
        state=response.json();sid=state["upload_id"]
        for i in range(state["total_chunks"]):
            if interrupted and i==1:break
            data=content[i*chunk_size:(i+1)*chunk_size]
            response=c.post(f"{base}/{sid}/chunk",data={"chunk_index":i},files={"chunk_file":("chunk",data)},headers={"X-Chunk-SHA256":hashlib.sha256(data).hexdigest()})
            assert response.status_code==200,response.text
        if interrupted:return state
        response=c.post(f"{base}/{sid}/finalize");assert response.status_code==200,response.text
        return state


def wait_ready(platform,vid,timeout=120):
    with httpx.Client(timeout=10) as c:
        deadline=time.monotonic()+timeout;seen=[]
        while time.monotonic()<deadline:
            r=c.get(platform.base+f"/api/v1/studio/source-uploads/videos/{vid}/status",headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]})
            assert r.status_code==200,r.text
            state=r.json();seen.append(state["status"])
            if state["status"]=="READY":return state,seen
            assert state["status"] not in {"FAILED","CANCELLED"},state
            time.sleep(.15)
        raise AssertionError(f"Worker did not finish {vid}: {seen[-10:]}")


def test_f10_real_server_processing_upload_worker_ffmpeg_ready(platform):
    from studio.core.server_processing import process_on_server
    source=platform.synthetic();saved={};seen=[]
    vid=process_on_server(source,platform.base+"/api/v1",platform.identities["OWNER"]["key"],"127.0.0.1",{"title":"Real F10 fixture"},{},lambda state,pct:seen.append((state,pct)),threading.Event(),saved.update,poll_seconds=120)
    from app.models.video import Video,VideoRendition,TranscodeJob,TranscodeJobStatus
    with platform.session() as db:
        video=db.get(Video,uuid.UUID(vid));assert video.status.value=="READY" and video.active_generation_id
        renditions=db.execute(select(VideoRendition).where(VideoRendition.video_id==video.id)).scalars().all()
        assert {r.quality_label for r in renditions}=={"360p","480p","720p"}
        assert all(r.is_ready and r.size_bytes>0 for r in renditions)
        job=db.get(TranscodeJob,video.active_generation_id);assert job.status==TranscodeJobStatus.COMPLETED and job.worker_id and job.progress_percent==100
    assert saved["video_id"]==vid and any(x[0]=="UPLOADING" for x in seen)
    platform.ready_video=vid


def test_source_disconnect_resume_and_finalize_idempotency(platform):
    from studio.core.server_processing import process_on_server
    path=platform.synthetic(seconds=4);state=upload_source(platform,path,interrupted=True)
    stages=[]
    vid=process_on_server(path,platform.base,platform.identities["OWNER"]["key"],None,{"title":"Resume fixture"},{"upload_id":state["upload_id"],"video_id":state["video_id"]},lambda *a:stages.append(a),threading.Event(),lambda _:pytest.fail("resume must reuse existing session"),poll_seconds=120)
    assert vid==state["video_id"]
    with httpx.Client(headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]}) as c:
        response=c.post(platform.base+f"/api/v1/studio/source-uploads/{state['upload_id']}/finalize")
        assert response.status_code==200,response.text
        status=c.get(platform.base+f"/api/v1/studio/source-uploads/{state['upload_id']}/status").json()
        assert status["bytes_received"]==path.stat().st_size and status["status"]=="COMPLETED"


def test_concurrent_source_chunks_and_duplicate_submission_exact_bytes(platform):
    path=platform.synthetic(seconds=3);content=path.read_bytes();size=32768
    base=platform.base+"/api/v1/studio/source-uploads";headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]}
    state=httpx.post(base+"/initiate",headers=headers,json={"title":"Concurrent source","filename":path.name,"file_size_bytes":len(content),"chunk_size_bytes":size,"expected_sha256":hashlib.sha256(content).hexdigest()}).json()
    sid=state["upload_id"]
    def send(i):
        block=content[i*size:(i+1)*size]
        r=httpx.post(f"{base}/{sid}/chunk",headers={**headers,"X-Chunk-SHA256":hashlib.sha256(block).hexdigest()},data={"chunk_index":i},files={"chunk_file":("chunk",block)},timeout=30)
        assert r.status_code==200,r.text
    with ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(send,[*range(state["total_chunks"]),0,0]))
    result=httpx.get(f"{base}/{sid}/status",headers=headers).json()
    assert result["bytes_received"]==len(content),result
    assert result["missing_chunks"]==[],result
    r=httpx.post(f"{base}/{sid}/finalize",headers=headers,timeout=30);assert r.status_code==200,r.text


def test_library_lifecycle_series_season_episode_playback_trash_restore(platform):
    from app.models.video import Video,PlaybackSession
    with platform.client() as c:
        r=c.post("/api/v1/drive/series",json={"title":"Integration series"});assert r.status_code==201,r.text
        series=r.json()["series"]["id"]
        r=c.post("/api/v1/drive/seasons",json={"series_id":series,"season_number":1});assert r.status_code==201,r.text
        season=r.json()["season"]["id"]
        state=upload_source(platform,platform.synthetic(seconds=4),series_id=series,season_id=season,episode_number=1,visibility="PUBLIC")
        vid=state["video_id"]
        with platform.session() as db:
            v=db.get(Video,uuid.UUID(vid));assert str(v.series_id)==series and str(v.season_id)==season
        r=c.post(f"/api/v1/admin/videos/{vid}/transcode");assert r.status_code==202,r.text
        wait_ready(platform,vid)
        with platform.session() as db:share=db.get(Video,uuid.UUID(vid)).share_id
        pages=[]
        for _ in range(2):
            page=c.get(f"/watch/{share}");assert page.status_code==200
            match=re.search(r'/api/v1/playback/(s/[^/"\s]+)',page.text);assert match,page.text[:300]
            pages.append(match.group(1))
        assert pages[0]!=pages[1]
        for token in pages:
            r=c.get(f"/api/v1/playback/{token}/master.m3u8");assert r.status_code==200,r.text
            urls=[x for x in r.text.splitlines() if x and not x.startswith("#")]
            assert len(urls)==3
            playlist=c.get(urljoin(str(r.request.url),urls[0]));assert playlist.status_code==200,playlist.text
            segment=[x for x in playlist.text.splitlines() if x and not x.startswith("#")][0]
            r=c.get(urljoin(str(playlist.request.url),segment));assert r.status_code==200 and len(r.content)>188
        naked=httpx.get(platform.base+f"/api/v1/playback/{pages[0]}/master.m3u8");assert naked.status_code in {401,403}
        with platform.client("OTHER") as other:
            assert other.get(f"/api/v1/admin/videos/{vid}").status_code==404
            assert other.post(f"/api/v1/admin/videos/{vid}/transcode").status_code==404
        with platform.client("ADMIN") as admin:assert admin.get(f"/api/v1/admin/videos/{vid}").status_code==200
        r=c.post("/api/v1/drive/trash",json={"items":[{"type":"video","id":vid}]});assert r.status_code==200,r.text
        assert c.get(f"/api/v1/playback/{pages[0]}/master.m3u8").status_code in {401,403,404}
        r=c.post("/api/v1/drive/restore",json={"items":[{"type":"video","id":vid}]});assert r.status_code==200,r.text
        assert c.get(f"/api/v1/playback/{pages[0]}/master.m3u8").status_code==200
        assert c.get("/api/v1/drive/scope",params={"id":season,"type":"season"}).status_code==200
        # Real telemetry writes: two sessions, one IP identity, one view per session.
        for token in pages:
            r=c.post(f"/api/v1/playback/{token}/event",json={"event":"heartbeat","duration":4,"bytes_received":100,"quality":"360p"});assert r.status_code==200,r.text
        analytics=c.get(f"/api/v1/admin/analytics/video/{vid}",params={"days":1}).json()["video"]
        assert analytics["views"]==2 and analytics["unique_visitors"]==1 and analytics["bandwidth_bytes"]==200,analytics


def test_invalid_source_and_conflicting_chunk_do_not_damage_ready_media(platform):
    from app.models.video import Video
    path=platform.temp/"invalid.mp4";path.write_bytes(b"invalid synthetic content")
    state=upload_source(platform,path,interrupted=True)
    headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]};base=platform.base+"/api/v1/studio/source-uploads"
    r=httpx.post(f"{base}/{state['upload_id']}/finalize",headers=headers);assert r.status_code==422,r.text
    with platform.session() as db:assert db.get(Video,uuid.UUID(state["video_id"])).status.value=="FAILED"
    body=b"x"*path.stat().st_size
    r=httpx.post(f"{base}/{state['upload_id']}/chunk",headers={**headers,"X-Chunk-SHA256":hashlib.sha256(body).hexdigest()},data={"chunk_index":0},files={"chunk_file":("chunk",body)})
    assert r.status_code in {409,410},r.text
    state=upload_source(platform,platform.synthetic(seconds=1),interrupted=True)
    body=b"x"*65536
    r=httpx.post(f"{base}/{state['upload_id']}/chunk",headers={**headers,"X-Chunk-SHA256":hashlib.sha256(body).hexdigest()},data={"chunk_index":0},files={"chunk_file":("chunk",body)})
    assert r.status_code==409,r.text


def test_real_ass_package_worker_http_hls_and_browser_rendering(platform,tmp_path):
    import shutil
    from studio.core.encoder import transcode_package_renditions
    from studio.core.fonts import analyze_ass_fonts,package_required_fonts
    from studio.core.packager import build_and_verify_package
    from studio.core.uploader import upload_and_verify_package
    from app.models.video import Video
    source=platform.synthetic(seconds=30,width=640,height=360);package=tmp_path/"ass-package"
    encoded=transcode_package_renditions(source,package,["360p"],encoder_choice="cpu",include_360p=True,hls_segment_duration=2)
    fixture=Path(__file__).parent/"fixtures/ass/eleven_fonts_arabic.ass"
    (package/"subtitles").mkdir();shutil.copy2(fixture,package/"subtitles/ar.ass")
    analysis=analyze_ass_fonts(fixture);assert analysis["missing_count"]==0 and analysis["total_fonts"]==11
    families=[f["name"] for f in analysis["fonts"]]
    fonts=package_required_fonts(families,package/"fonts");assert len(fonts)==11
    manifest=build_and_verify_package(package,"Real ASS fixture",encoded["duration_seconds"],encoded["renditions"],subtitles=[{"language_code":"ar","language_name":"العربية","label":"Arabic ASS","is_default":True,"path":"subtitles/ar.ass","format":"ass","fonts":families}],fonts=fonts)["manifest"]
    result=upload_and_verify_package(platform.base+"/api/v1",platform.identities["OWNER"]["key"],package,manifest,poll_timeout_seconds=120)
    assert result["success"],result
    vid=result["video_id"]
    with platform.client() as c:
        response=c.patch(f"/api/v1/admin/videos/{vid}",json={"visibility":"PUBLIC"});assert response.status_code==200,response.text
        with platform.session() as db:share=db.get(Video,uuid.UUID(vid)).share_id
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);context=browser.new_context()
        page=context.new_page();errors=[];fonts_loaded=[];messages=[]
        page.on("pageerror",lambda e:errors.append(str(e)))
        page.on("console",lambda m:messages.append(m.text))
        page.on("response",lambda r:fonts_loaded.append(r.url) if "/fonts/" in r.url and r.status==200 else None)
        response=page.goto(platform.base+"/watch/"+share,wait_until="networkidle")
        assert response.status==200,page.locator('body').inner_text()
        assert page.locator('#video-element').count()==1,page.locator('body').inner_text()
        page.locator("#big-play-btn").click()
        page.wait_for_function("() => document.querySelector('video').readyState >= 2",timeout=30000)
        page.wait_for_function("() => Boolean(window.jassubInstance)",timeout=30000)
        font_worker=next(worker for worker in page.workers if worker.url.endswith("jassub-worker.js"))
        page.evaluate("async () => await Promise.race([window.jassubInstance._loaded, new Promise((_, reject) => setTimeout(() => reject(new Error('ASS worker initialization timed out')), 15000))])")
        page.evaluate("document.querySelector('video').pause();document.querySelector('video').currentTime=22.5")
        page.wait_for_function("() => document.querySelector('video').currentTime > 22",timeout=15000)
        page.wait_for_function("() => document.querySelector('video').readyState >= 2",timeout=15000)
        page.wait_for_timeout(1200)
        assert font_worker.evaluate("() => !!fontMap_.arial"), "Attached Arial must be selected for Arabic fallback"
        styles=font_worker.evaluate("() => Array.from({length:jassubObj.getStyleCount()},(_,i)=>({name:jassubObj.getStyle(i).Name,font:jassubObj.getStyle(i).FontName,size:jassubObj.getStyle(i).FontSize}))")
        assert font_worker.evaluate("() => jassubObj.getEvent(0).Text").startswith(r"{\fnBahij Janna\bord2}")
        by_name={style["name"]:style for style in styles}
        for style,family in {"Azer":"29LT Azer Medium","Adobe":"Bahij Janna","Nassim":"Bahij Nassim-edit","TheSans":"Bahij TheSansArabic Plain"}.items():
            assert by_name[style]["font"]==family and by_name[style]["size"]==24,by_name[style]
        assert len(set(fonts_loaded))==11,fonts_loaded
        assert page.evaluate("document.querySelector('video').videoHeight == 360")
        assert page.locator("#ass-canvas").is_visible()
        page.wait_for_function("""() => {const s=document.querySelector('#ass-canvas');const c=document.createElement('canvas');c.width=s.width;c.height=s.height;const ctx=c.getContext('2d');ctx.drawImage(s,0,0);return ctx.getImageData(0,0,c.width,c.height).data.some((v,i)=> i%4===3 && v>0)}""",timeout=30000)
        page.locator("#player-container").screenshot(path=str(tmp_path/"real-ass-render.png"))
        assert not errors,errors
        browser.close()
    platform.ass_video=vid


@pytest.mark.parametrize("mode",["local","server","gpu"])
def test_studio_real_mode_selection_cpu_concurrency_and_completion(platform,tmp_path,mode):
    from studio.config import load_studio_config,save_studio_config
    from studio.core.batch_runner import BatchRunner
    from studio.core.batch_store import BatchStore
    previous=load_studio_config()
    (tmp_path/"output").mkdir()
    choice="gpu" if mode=="gpu" else "cpu"
    save_studio_config({"processing_mode":"local" if mode=="gpu" else mode,"cpu_concurrent_jobs":2,"gpu_concurrent_jobs":1,"default_encoder":choice,"include_360p":True,"converted_output_folder":str(tmp_path/"output"),"server_url":platform.base+"/api/v1","api_key":platform.identities["OWNER"]["key"]})
    store=BatchStore(tmp_path/"jobs.db");batch=store.create_batch(str(tmp_path/"series"))
    sources=[platform.synthetic(seconds=5,width=640,height=360) for _ in range(3)]
    store.add_episodes(batch["id"],[{"source_path":str(source),"filename":source.name,"order_index":i,"episode_number":i,"state":"PENDING","settings_json":{"encoder_choice":choice,"include_360p":True,"selected_qualities":["360p"],"default_preset":"economy"}} for i,source in enumerate(sources,1)])
    runner=BatchRunner(store)
    try:
        runner.start_batch(batch["id"]);runner._worker_thread.join(180)
        episodes=store.get_episodes(batch["id"])
        assert not runner.is_running,episodes
        assert all(ep["state"]==("READY" if mode=="server" else "READY_TO_UPLOAD") for ep in episodes),[(ep["state"],ep.get("error")) for ep in episodes]
        assert runner._max_observed_encodes==(1 if mode=="gpu" else 2) and runner._active_encodes_count==0
        if mode=="server":assert all(ep["server_video_id"] for ep in episodes)
        else:
            assert all((Path(ep["work_dir"])/"manifest.json").is_file() for ep in episodes)
            actual_encoders={json.loads((Path(ep["work_dir"])/"studio_package_identity.json").read_text(encoding="utf-8"))["encoder_used"] for ep in episodes}
            if mode=="gpu":assert actual_encoders and actual_encoders.issubset({"h264_nvenc","h264_qsv","h264_amf"}),actual_encoders
            else:assert actual_encoders=={"libx264"},actual_encoders
    finally:runner.shutdown();save_studio_config(previous)


def test_real_upload_pause_resume_cancel_and_terminal_file_safety(platform):
    from app.models.upload import UploadSession
    source=platform.synthetic(seconds=2);state=upload_source(platform,source,interrupted=True)
    sid=state["upload_id"];base="/api/v1/uploads/"+sid
    with platform.client() as c:
        r=c.post(base+"/pause");assert r.status_code==200 and r.json()["status"]=="PAUSED",r.text
        block=source.read_bytes()[65536:131072]
        r=c.post(base+"/chunk",data={"chunk_index":1},files={"chunk_file":("chunk",block)});assert r.status_code==409,r.text
        r=c.post(base+"/finalize");assert r.status_code==409,r.text
        r=c.post(base+"/resume");assert r.status_code==200 and r.json()["status"]=="UPLOADING",r.text
        r=c.post(base+"/chunk",data={"chunk_index":1},files={"chunk_file":("chunk",block)});assert r.status_code==200,r.text
        r=c.post(base+"/cancel");assert r.status_code==200,r.text
        with platform.session() as db:session=db.get(UploadSession,uuid.UUID(sid));path=session.temp_directory
        assert docker("exec",platform.api_name,"python","-c",f"from pathlib import Path;print(Path({path!r}).exists())")=="False"
        assert c.post(base+"/resume").status_code in {400,409,410}


def test_real_disk_admission_and_no_record_allocation(platform):
    from app.models.video import Video
    from sqlalchemy import func
    name="abv-real-e2e-lowdisk"
    with platform.session() as db:before=db.scalar(select(func.count(Video.id)))
    try:
        docker("run","-d","--name",name,*platform.options(),"-e","STORAGE_MIN_FREE_BYTES=1152921504606846976","-p","127.0.0.1:28081:8000",IMAGE,"python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--no-access-log")
        for _ in range(80):
            try:
                response=httpx.get("http://127.0.0.1:28081/health",timeout=1)
                if response.status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.15)
        response=httpx.post("http://127.0.0.1:28081/api/v1/studio/source-uploads/initiate",headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]},json={"title":"Disk rejected","filename":"fixture.mp4","file_size_bytes":100})
        assert response.status_code==507,response.text
        with platform.session() as db:assert db.scalar(select(func.count(Video.id)))==before
    finally:
        assert_owned(name);docker("rm","-f",name)


def test_redis_disconnect_preserves_package_staging_and_database_state(platform):
    from app.models.package import PackageUploadSession
    from app.models.video import Video,VideoStatus
    from datetime import datetime,timedelta,timezone
    vid,sid=uuid.uuid4(),uuid.uuid4();path=f"/storage/staging/packages/{vid}/{sid}"
    docker("exec",platform.api_name,"python","-c",f"from pathlib import Path;p=Path({path!r});p.mkdir(parents=True);(p/'sentinel.ts').write_bytes(b'preserve this fixture')")
    with platform.session() as db:
        db.add(Video(id=vid,user_id=platform.identities["OWNER"]["user"],title="Redis outage fixture",source_filename="fixture.m3u8",status=VideoStatus.VERIFYING));db.flush()
        record=PackageUploadSession(id=sid,video_id=vid,status="VERIFYING",total_files=1,total_bytes=21,completed_bytes=21,manifest_data={},staging_path=path,target_generation_id=uuid.uuid4(),verification_job_id="outage-fixture",expires_at=datetime.now(timezone.utc)+timedelta(hours=1),updated_at=datetime.now(timezone.utc)-timedelta(minutes=10))
        db.add(record);db.commit()
    assert_owned("abv-f16-redis")
    try:
        docker("network","disconnect","abv-real-e2e-net","abv-f16-redis")
        docker("exec",platform.api_name,"python","-c","import asyncio;from app.workers.reconciler import reconcile_stalled_package_jobs;print(asyncio.run(reconcile_stalled_package_jobs()))",timeout=25)
        with platform.session() as db:assert db.get(PackageUploadSession,sid).status=="VERIFYING"
        assert docker("exec",platform.api_name,"python","-c",f"from pathlib import Path;print((Path({path!r})/'sentinel.ts').read_bytes().decode())")=="preserve this fixture"
    finally:
        docker("network","connect","--alias","redis","abv-real-e2e-net","abv-f16-redis")
        for worker in platform.workers:platform.restart_worker(worker)
        # End this synthetic failed verification explicitly so later reconciler
        # tests cannot classify it as an unrelated stalled package.
        with platform.session() as db:db.get(PackageUploadSession,sid).status="CANCELLED";db.commit()


def test_worker_crash_is_recovered_with_another_worker_still_alive(platform):
    from app.models.video import Video,TranscodeJob
    state=upload_source(platform,platform.synthetic(seconds=45,width=1920,height=1080),chunk_size=5*1024*1024)
    vid=state["video_id"]
    with platform.client() as c:
        r=c.post(f"/api/v1/admin/videos/{vid}/transcode");assert r.status_code==202,r.text
        jid=uuid.UUID(r.json()["job_id"]);deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            with platform.session() as db:job=db.get(TranscodeJob,jid);status=job.status.value;owner=job.worker_id;quality=job.current_rendition
            if status=="PROCESSING" and quality=="480p":break
            assert status not in {"FAILED","COMPLETED"},(status,quality)
            time.sleep(.05)
        else:pytest.fail("Worker did not reach a recoverable second rendition")
        name=next(name for name in platform.workers if docker("inspect","--format","{{.Config.Hostname}}",name) in owner)
        assert_owned(name);docker("kill","--signal","KILL",name)
        # Let the real 15-second heartbeat and 60-second stale interval expire.
        time.sleep(65)
        output=docker("exec",platform.api_name,"python","-c","import asyncio;from app.workers.reconciler import reconcile_stalled_transcode_jobs;print(asyncio.run(reconcile_stalled_transcode_jobs(threshold_minutes=0)))")
        with platform.session() as db:
            job=db.get(TranscodeJob,jid)
            assert job.status.value=="FAILED" and job.error_code=="TRANSCODE_ABANDONED",(job.status.value,output[-400:])
        platform.restart_worker(name)
        r=c.post(f"/api/v1/admin/transcodes/{jid}/retry");assert r.status_code==200,r.text
        wait_ready(platform,vid,180)
        logs="\n".join(docker("logs",worker) for worker in platform.workers)
        assert "Reusing verified 360p rendition" in logs


def test_concurrent_playback_telemetry_counts_once_and_preserves_bytes(platform):
    from app.models.video import Video,PlaybackSession
    vid=getattr(platform,"ready_video",None)
    if not vid:
        state=upload_source(platform,platform.synthetic());vid=state["video_id"]
        with platform.client() as c:assert c.post(f"/api/v1/admin/videos/{vid}/transcode").status_code==202
        wait_ready(platform,vid)
    with platform.client() as c:
        page=c.get("/watch/"+vid);assert page.status_code==200
        token=re.search(r'/api/v1/playback/(s/[^/"\s]+)',page.text).group(1)
        url=platform.base+f"/api/v1/playback/{token}/event"
        cookie=c.build_request("POST",url).headers["Cookie"]
        def send(_):
            response=httpx.post(url,headers={"Cookie":cookie},json={"event":"heartbeat","duration":1,"bytes_received":100,"quality":"360p"},timeout=20)
            assert response.status_code==200,response.text
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(send,range(8)))
        with platform.session() as db:
            session=db.scalar(select(PlaybackSession).where(PlaybackSession.video_id==uuid.UUID(vid)))
            assert session.duration_seconds==8 and session.bytes_served==800,(session.duration_seconds,session.bytes_served)
            assert db.get(Video,uuid.UUID(vid)).view_count==1


def make_real_package(platform, tmp_path, title):
    from studio.core.encoder import transcode_package_renditions
    from studio.core.packager import build_and_verify_package
    source=platform.synthetic(seconds=5,width=640,height=360)
    package=tmp_path / uuid.uuid4().hex
    encoded=transcode_package_renditions(source,package,["360p"],encoder_choice="cpu",include_360p=True,hls_segment_duration=2)
    manifest=build_and_verify_package(package,title,encoded["duration_seconds"],encoded["renditions"])["manifest"]
    return package,manifest


def test_real_studio_archive_cancel_resume_idempotency_and_fingerprint(platform,tmp_path,monkeypatch):
    from studio.core.uploader import upload_and_verify_package
    from app.models.video import Video
    from app.models.package import PackageUploadSession
    package,manifest=make_real_package(platform,tmp_path,"Archive resume fixture")
    cancelled=threading.Event();state={}
    monkeypatch.setenv("ABOODVIDEO_UPLOAD_CHUNK_BYTES","16384")
    def stop_after_confirmed_chunk(progress):
        if progress["stage"]=="uploading_files":
            state.update(progress);cancelled.set()
    with pytest.raises(RuntimeError):
        upload_and_verify_package(platform.base+"/api/v1",platform.identities["OWNER"]["key"],package,manifest,progress_callback=stop_after_confirmed_chunk,cancel_event=cancelled)
    assert state["session_id"] and 0<state["upload_progress_pct"]<100,state
    headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]}
    base=platform.base+"/api/v1/studio/packages"
    with httpx.Client(headers=headers,timeout=30) as c:
        r=c.get(f"{base}/{state['session_id']}/archive/status");assert r.status_code==200 and r.json()["offset"]==16384,r.text
        with platform.session() as db:record=db.get(PackageUploadSession,uuid.UUID(state["session_id"]));fingerprint=record.request_fingerprint;idem=record.idempotency_key
        changed=json.loads(json.dumps(manifest));changed["video"]["title"]="Different request"
        r=c.post(base+"/init",json={"manifest":changed,"idempotency_key":idem});assert r.status_code==409,r.text
    cancelled.clear()
    result=upload_and_verify_package(platform.base+"/api/v1",platform.identities["OWNER"]["key"],package,manifest,video_id=state["video_id"],session_id=state["session_id"],poll_timeout_seconds=120)
    assert result["success"] and result["video_id"]==state["video_id"],result
    second=upload_and_verify_package(platform.base+"/api/v1",platform.identities["OWNER"]["key"],package,manifest,video_id=state["video_id"],session_id=state["session_id"],poll_timeout_seconds=120)
    assert second["success"] and second["video_id"]==result["video_id"]
    with platform.session() as db:
        assert db.get(PackageUploadSession,uuid.UUID(state["session_id"])).request_fingerprint==fingerprint
        video=db.get(Video,uuid.UUID(result["video_id"]));assert video.status.value=="READY"


@pytest.mark.parametrize("repeat",range(3))
def test_real_per_file_package_upload_worker_and_parallel_idempotent_init(platform,tmp_path,repeat):
    package,manifest=make_real_package(platform,tmp_path,"Per file fixture")
    base=platform.base+"/api/v1/studio/packages";headers={"Authorization":"Bearer "+platform.identities["OWNER"]["key"]}
    payload={"manifest":manifest,"idempotency_key":hashlib.sha256(uuid.uuid4().bytes).hexdigest()}
    barrier=threading.Barrier(8)
    def initiate(_):
        barrier.wait(timeout=10)
        r=httpx.post(base+"/init",headers=headers,json=payload,timeout=30);assert r.status_code==201,r.text;return r.json()
    with ThreadPoolExecutor(max_workers=8) as pool:states=list(pool.map(initiate,range(8)))
    assert len({s["session_id"] for s in states})==1,states
    state=states[0]
    with httpx.Client(headers=headers,timeout=30) as c:
        for declared in state["files"]:
            content=(package/declared["relative_path"]).read_bytes()
            r=c.put(f"{base}/{state['session_id']}/files/{declared['file_id']}",content=content,headers={"Content-Type":declared.get("media_type") or "application/octet-stream"});assert r.status_code==200,r.text
        r=c.post(f"{base}/{state['session_id']}/finalize");assert r.status_code==202,r.text
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            r=c.get(f"{base}/{state['session_id']}");assert r.status_code==200,r.text
            status=r.json()["status"]
            if status=="READY":break
            assert status not in {"FAILED","CANCELLED"},r.text
            time.sleep(.15)
        assert status=="READY"
        r=c.post(f"{base}/{state['session_id']}/finalize");assert r.status_code in {200,202},r.text


def test_real_studio_cpu_pause_resume_and_shutdown_cancel(platform,tmp_path):
    from studio.config import load_studio_config,save_studio_config
    from studio.core.batch_runner import BatchRunner
    from studio.core.batch_store import BatchStore
    previous=load_studio_config();(tmp_path/"output").mkdir()
    save_studio_config({"processing_mode":"local","cpu_concurrent_jobs":1,"default_encoder":"cpu","include_360p":True,"converted_output_folder":str(tmp_path/"output")})
    store=BatchStore(tmp_path/"controls.db");batch=store.create_batch(str(tmp_path/"series"))
    sources=[platform.synthetic(seconds=30,width=1280,height=720) for _ in range(2)]
    store.add_episodes(batch["id"],[{"source_path":str(source),"filename":source.name,"order_index":i,"episode_number":i,"state":"PENDING","settings_json":{"encoder_choice":"cpu","include_360p":True,"selected_qualities":["360p"],"default_preset":"economy"}} for i,source in enumerate(sources,1)])
    runner=BatchRunner(store)
    try:
        runner.start_batch(batch["id"])
        deadline=time.monotonic()+30
        while not any(ep["state"]=="ENCODING" for ep in store.get_episodes(runner._active_batch_id)) and time.monotonic()<deadline:time.sleep(.01)
        assert runner._active_encodes_count==1
        runner.pause_batch();runner._worker_thread.join(120)
        episodes=store.get_episodes(batch["id"])
        assert episodes[0]["state"] in {"READY_TO_UPLOAD","READY_TO_ENCODE"} and episodes[1]["state"]=="PENDING",[(ep["state"],ep.get("error")) for ep in episodes]
        runner.resume_batch();runner._worker_thread.join(120)
        assert all(ep["state"]=="READY_TO_UPLOAD" for ep in store.get_episodes(batch["id"]))
        before=[hashlib.sha256((Path(ep["work_dir"])/"manifest.json").read_bytes()).hexdigest() for ep in store.get_episodes(batch["id"])]
        next_batch=store.create_batch(str(tmp_path/"cancel-series"));source=platform.synthetic(seconds=45,width=1920,height=1080)
        store.add_episodes(next_batch["id"],[{"source_path":str(source),"filename":source.name,"order_index":1,"episode_number":1,"state":"PENDING","settings_json":{"encoder_choice":"cpu","selected_qualities":["720p"],"default_preset":"economy"}}])
        runner.start_batch(next_batch["id"])
        deadline=time.monotonic()+30
        while not any(ep["state"]=="ENCODING" for ep in store.get_episodes(runner._active_batch_id)) and time.monotonic()<deadline:time.sleep(.01)
        assert runner._active_encodes_count==1
        runner.shutdown(timeout_seconds=30)
        assert not runner.is_running and runner._active_encodes_count==0
        assert source.is_file()
        after=[hashlib.sha256((Path(ep["work_dir"])/"manifest.json").read_bytes()).hexdigest() for ep in store.get_episodes(batch["id"])]
        assert before==after
    finally:runner.shutdown();save_studio_config(previous)


def test_real_nginx_x_accel_playback_and_internal_asset_isolation(platform):
    from real_platform_support import ROOT,VOLUME,IMAGE
    from app.models.video import Video
    api,nginx="abv-real-e2e-accel-api","abv-real-e2e-nginx"
    created=[]
    try:
        docker("run","-d","--name",api,*platform.options(),"--network-alias","backend","-e","SERVE_MEDIA_VIA_NGINX=true",IMAGE,"python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--no-access-log")
        created.append(api)
        docker("run","-d","--name",nginx,"--network","abv-real-e2e-net","--label","abv.purpose=real-integration","-p","127.0.0.1:28082:80",
               "--mount",f"type=bind,src={ROOT/'nginx/nginx.conf'},dst=/etc/nginx/nginx.conf,readonly",
               "--mount",f"type=bind,src={ROOT/'nginx/conf.d/default.conf'},dst=/etc/nginx/conf.d/default.conf,readonly",
               *([] if os.getenv("ABV_INTEGRATION_NGINX_IMAGE") else ["--mount", f"type=bind,src={ROOT/'backend/app/static'},dst=/var/www/static,readonly"]),
               "--mount",f"type=volume,src={VOLUME},dst=/var/www/storage,readonly",os.getenv("ABV_INTEGRATION_NGINX_IMAGE", "nginx:1.27.1-alpine3.20"))
        created.append(nginx);docker("exec",nginx,"nginx","-t")
        with httpx.Client(base_url="http://127.0.0.1:28082",timeout=30) as c:
            for _ in range(100):
                try:
                    if c.get("/health").status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            for route in ["/health","/readiness"]:
                r=c.get(route);assert r.status_code==200,r.text
            r=c.post("/api/v1/auth/login",json={"username_or_email":platform.identities["OWNER"]["username"],"password":"Fixture-only-password-2026!"});assert r.status_code==200,r.text
            c.headers["X-CSRF-Token"]=r.json()["csrf_token"]
            vid=getattr(platform,"ass_video",None) or platform.ready_video
            page=c.get("/watch/"+vid);assert page.status_code==200
            token=re.search(r'/api/v1/playback/(s/[^/"\s]+)',page.text).group(1)
            r=c.get(f"/api/v1/playback/{token}/master.m3u8");assert r.status_code==200 and "#EXTM3U" in r.text
            variant=next(line for line in r.text.splitlines() if line and not line.startswith("#"))
            r=c.get(urljoin(str(r.request.url),variant));assert r.status_code==200 and "#EXT-X-ENDLIST" in r.text
            segment=next(line for line in r.text.splitlines() if line and not line.startswith("#"))
            r=c.get(urljoin(str(r.request.url),segment),headers={"Range":"bytes=0-187"});assert r.status_code==206 and len(r.content)==188,r.text[:100]
            assert r.headers["content-type"]=="video/mp2t"
            assert "x-accel-redirect" not in r.headers
            with platform.session() as db:generation=str(db.get(Video,uuid.UUID(vid)).active_generation_id)
            r=c.get(f"/internal_media/{vid}/generations/{generation}/360p/index.m3u8");assert r.status_code==404
            assert c.get("/static/vendor/jassub/jassub-worker.wasm").headers["content-type"]=="application/wasm"
    finally:
        for name in reversed(created):
            assert_owned(name);(platform.temp/(name+".log")).write_text(docker("logs",name),encoding="utf-8");docker("rm","-f",name)


def test_real_browser_playback_resume_survives_reload(platform):
    from app.models.video import Video
    from playwright.sync_api import sync_playwright
    vid=platform.ass_video
    with platform.session() as db:share=db.get(Video,uuid.UUID(vid)).share_id
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(platform.base+"/watch/"+share,wait_until="networkidle");page.locator("#big-play-btn").click()
        page.wait_for_function("() => document.querySelector('video').readyState >= 2")
        page.evaluate("document.querySelector('video').currentTime=12")
        page.wait_for_function("() => document.querySelector('video').currentTime >= 12 && document.querySelector('video').readyState >= 2")
        page.evaluate("document.querySelector('video').pause()")
        page.wait_for_function("key => JSON.parse(localStorage.getItem(key) || '{}').time >= 12",arg="aboodvideo_resume_"+share)
        page.reload(wait_until="networkidle")
        page.locator(".toast-action-btn").wait_for(state="visible",timeout=15000)
        page.locator(".toast-action-btn").click()
        page.wait_for_function("() => document.querySelector('video').currentTime >= 12")
        assert not errors,errors
        browser.close()


@pytest.mark.parametrize("action",["pause","resume","cancel"])
def test_real_source_controls_cannot_overwrite_concurrent_finalization(platform,action):
    from sqlalchemy import text
    from app.models.upload import UploadSession
    from app.models.video import Video
    source=platform.synthetic(seconds=1,width=640,height=360)
    state=upload_source(platform,source,chunk_size=5*1024*1024,interrupted=True)
    sid=state["upload_id"];base=platform.base+"/api/v1/uploads/"+sid
    with platform.client() as c:
        headers=dict(c.build_request("POST",base+"/finalize").headers)
        headers.pop("content-length",None)
        gate=platform.engine.connect();transaction=gate.begin()
        gate.execute(select(UploadSession).where(UploadSession.id==uuid.UUID(sid)).with_for_update())
        def waiting_queries():
            with platform.engine.connect() as conn:
                return list(conn.execute(text("SELECT query FROM pg_stat_activity WHERE datname='aboodvideo_e2e_test' AND wait_event_type='Lock' AND query ILIKE '%upload_sessions%' ")).scalars())
        def wait_count(count):
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if len(waiting_queries())>=count:return
                time.sleep(.02)
            pytest.fail("Concurrent requests did not reach the real PostgreSQL row lock")
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                final=pool.submit(httpx.post,base+"/finalize",headers=headers,timeout=40)
                wait_count(1)
                control=pool.submit(httpx.post,base+"/"+action,headers=headers,timeout=40)
                wait_count(2)
            finally:transaction.commit();gate.close()
            finalized=final.result();controlled=control.result()
        assert finalized.status_code==200,finalized.text
        assert controlled.status_code in {400,409,410},controlled.text
        with platform.session() as db:
            assert db.get(UploadSession,uuid.UUID(sid)).status.value=="COMPLETED"
            video=db.get(Video,uuid.UUID(state["video_id"]));assert video.status.value=="UPLOADED"
            path=video.source_path
        assert docker("exec",platform.api_name,"python","-c",f"from pathlib import Path;print(Path({path!r}).is_file())")=="True"
