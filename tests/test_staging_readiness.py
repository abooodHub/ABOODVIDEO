"""Opt-in real-owner-media excerpts and measured staging load; never production.

ABV_STAGING_MEDIA_MANIFEST points at a temporary, explicitly prepared manifest.
Original media is stat-checked only; all processing uses disposable excerpt copies.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urljoin

import httpx
import pytest
from sqlalchemy import select, text

from real_platform_support import RealPlatform, ROOT, IMAGE, NETWORK, VOLUME, docker, assert_owned
from test_real_platform_integration import upload_source, wait_ready

pytestmark = pytest.mark.skipif(
    os.getenv("ABV_RUN_REAL_INTEGRATION") != "1" or not os.getenv("ABV_STAGING_MEDIA_MANIFEST"),
    reason="Requires disposable stack and an explicit real-media excerpt manifest",
)
SOURCE_FIXTURE_IDS=["hevc-mp4","hevc-multiaudio"]
if os.getenv("ABV_STAGING_MEDIA_MANIFEST"):
    available=json.loads(Path(os.environ["ABV_STAGING_MEDIA_MANIFEST"]).read_text(encoding="utf-8"))
    if any(item["id"]=="hevc10-mkv" for item in available["media"]):
        SOURCE_FIXTURE_IDS.append("hevc10-mkv")


def record(platform, category, value):
    platform.measurements.setdefault(category, []).append(value)
    platform.evidence.write_text(json.dumps(platform.measurements, indent=2), encoding="utf-8")


@pytest.fixture(scope="module")
def staging():
    platform = RealPlatform()
    manifest_path = Path(os.environ["ABV_STAGING_MEDIA_MANIFEST"])
    platform.media = json.loads(manifest_path.read_text(encoding="utf-8"))
    platform.measurements = {}
    platform.evidence = manifest_path.parent / ("measurements-" + os.environ["ABV_INTEGRATION_RUN_ID"] + ".json")
    stop = threading.Event()
    samples = []

    def sample():
        import psutil
        from studio.core.hardware import get_gpu_telemetry
        while not stop.is_set():
            try:
                names = docker("ps","--filter","label=abv.run="+os.environ["ABV_INTEGRATION_RUN_ID"],"--format","{{.Names}}").splitlines()
                rows = docker("stats", "--no-stream", "--format", "{{json .}}", *names)
                native = []
                for process in psutil.Process().children(recursive=True):
                    try:
                        if process.name().lower() in {"ffmpeg.exe","ffmpeg"}:
                            native.append({"pid":process.pid,"rss_bytes":process.memory_info().rss,"threads":process.num_threads()})
                    except psutil.Error: pass
                samples.append({"at": time.time(), "containers": [json.loads(r) for r in rows.splitlines()],
                                "native_ffmpeg":native,"host_shared_gpu":get_gpu_telemetry()})
            except RuntimeError:
                pass
            stop.wait(3)

    try:
        platform.start()
        # Actual staging encoding profile rather than the ultrafast test profile.
        platform.env_file.write_text(platform.env_file.read_text().replace("FFMPEG_PRESET=ultrafast", "FFMPEG_PRESET=medium"))
        for worker in platform.workers:
            platform.restart_worker(worker)
        for name, cpus, memory in [(platform.api_name,"1","1024m"),("abv-f16-pg","1","1024m"),
                                    ("abv-f16-redis","0.5","512m"),*[(w,"8","3072m") for w in platform.workers]]:
            assert_owned(name)
            docker("update","--cpus",cpus,"--memory",memory,"--memory-swap",memory,name)
        identities={name:docker("inspect","--format","{{.Image}}",name) for name in [platform.api_name,*platform.workers]}
        assert len(set(identities.values())) == 1
        if os.getenv("ABV_INTEGRATION_IMAGE"):
            for relative in ["app/services/transcoder.py","app/static/js/player.js"]:
                actual=docker("exec",platform.api_name,"python","-c",
                    f"from pathlib import Path;import hashlib;print(hashlib.sha256(Path({('/app/'+relative)!r}).read_bytes()).hexdigest())")
                assert actual == hashlib.sha256((ROOT/"backend"/relative).read_bytes()).hexdigest()
        record(platform,"runtime_images",{"containers":identities,"baked_candidate_source":bool(os.getenv("ABV_INTEGRATION_IMAGE"))})
        record(platform,"stack_dependencies",{
            "postgres":docker("exec","abv-f16-pg","postgres","--version"),
            "redis":docker("exec","abv-f16-redis","redis-server","--version"),
            "images":{name:docker("inspect","--format","{{.Image}}",name) for name in ["abv-f16-pg","abv-f16-redis"]}})
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        yield platform
    finally:
        stop.set()
        if "thread" in locals():
            thread.join(15)
        record(platform, "resources", samples)
        for item in platform.media["media"]:
            original = Path(item["original"])
            assert original.stat().st_size == item["original_size"]
            assert original.stat().st_mtime_ns == item["original_mtime_ns"]
        assert hashlib.sha256(Path(platform.media["srt_original"]).read_bytes()).hexdigest() == platform.media["srt_sha256"]
        platform.close()


@pytest.mark.parametrize("fixture_id", SOURCE_FIXTURE_IDS)
def test_real_media_studio_source_upload_worker_renditions(staging, fixture_id, proxy):
    from studio.core.server_processing import process_on_server
    from app.models.video import Video, VideoRendition
    item = next(i for i in staging.media["media"] if i["id"] == fixture_id)
    source = Path(item["path"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == item["sha256"]
    started = time.monotonic()
    stages = []
    vid = process_on_server(source, staging.base, staging.identities["OWNER"]["key"], None,
                            {"title": "Real excerpt " + fixture_id}, {},
                            lambda state, pct: stages.append(state), threading.Event(), lambda _: None,
                            poll_seconds=300)
    with staging.session() as db:
        video = db.get(Video, uuid.UUID(vid))
        renditions = db.execute(select(VideoRendition).where(VideoRendition.video_id == video.id)).scalars().all()
        assert video.status.value == "READY"
        assert {r.quality_label for r in renditions} == {"360p", "480p", "720p"}
        assert all(r.is_ready and r.size_bytes > 0 for r in renditions)
        generation=str(video.active_generation_id)
    segment=docker("exec",staging.api_name,"python","-c",
        f"from pathlib import Path;print(next((Path('/storage/videos')/{vid!r}/'generations'/{generation!r}).rglob('*.ts')))")
    output_probe=json.loads(docker("exec",staging.api_name,"ffprobe","-v","error","-show_entries",
        "stream=codec_type,codec_name,channels,profile,pix_fmt","-of","json",segment))
    audio=[s for s in output_probe["streams"] if s["codec_type"]=="audio"]
    assert len(audio)==1 and audio[0]["codec_name"]=="aac" and audio[0]["channels"]==2
    processing_seconds=time.monotonic()-started
    from playwright.sync_api import sync_playwright
    with login(staging,proxy) as c,sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);context=browser.new_context()
        context.add_cookies([{"name":cookie.name,"value":cookie.value,"url":proxy} for cookie in c.cookies.jar])
        page=context.new_page();errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        assert page.goto(proxy+"/watch/"+vid,wait_until="networkidle").status==200
        if fixture_id=="hevc10-mkv":
            video_stream=next(s for s in output_probe["streams"] if s["codec_type"]=="video")
            assert video_stream["profile"]=="High 10" and video_stream["pix_fmt"]=="yuv420p10le"
            assert page.evaluate("() => window.hls.levels.length === 3 && window.hls.levels.every(l => l.videoCodec.startsWith('avc1.6e'))")
        page.locator("#big-play-btn").click()
        page.wait_for_function("() => document.querySelector('video').currentTime > .2",timeout=45000)
        page.locator('#quality-menu .menu-item:not(#qual-opt-auto)').evaluate_all("nodes => nodes.find(n=>n.textContent.includes('720')).click()")
        page.wait_for_function("() => document.querySelector('video').videoHeight === 720",timeout=45000)
        page.evaluate("() => {document.querySelector('video').currentTime=8;}")
        page.wait_for_function("() => document.querySelector('video').currentTime >= 8 && document.querySelector('video').readyState >= 2",timeout=30000)
        assert not errors,errors
        browser.close()
    record(staging, "server_processing", {"fixture": fixture_id, "seconds": round(processing_seconds, 3),
                                           "bytes": source.stat().st_size, "video_id": vid,
                                           "source_duration_seconds":float(item["probe"]["format"]["duration"]),
                                           "input_audio_tracks":sum(s["codec_type"]=="audio" for s in item["probe"]["streams"]),
                                           "output_audio_tracks":len(audio),"output_audio_codec":"aac",
                                           "same_video_chromium_playback_and_seek":True,
                                           "stages": sorted(set(stages)), "preset": "medium"})


def test_real_upload_tcp_interruption_duplicate_resume_and_redis_restart(staging, request):
    import socket
    import redis
    from studio.core.server_processing import process_on_server
    from app.models.setting import SystemSetting
    from app.services.quality_settings import SETTING_KEY
    with staging.session() as db:
        setting = db.get(SystemSetting,SETTING_KEY); previous_quality = setting.value
        setting.value = "360p"; db.commit()
    def restore_quality():
        with staging.session() as db:
            db.get(SystemSetting,SETTING_KEY).value = previous_quality; db.commit()
    request.addfinalizer(restore_quality)
    item = next(i for i in staging.media["media"] if i["id"] == "hevc-mp4")
    source = Path(item["path"])
    state = upload_source(staging, source, "Real interrupted source", chunk_size=1024*1024, interrupted=True)
    base = staging.base+"/api/v1/studio/source-uploads/"+state["upload_id"]
    headers = {"Authorization":"Bearer "+staging.identities["OWNER"]["key"]}
    # Break a real TCP multipart request halfway through the next chunk.
    boundary = "abv-staging-interruption"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chunk_index\"\r\n\r\n1\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chunk_file\"; filename=\"chunk\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + b"partial-transfer"*100
    raw = (f"POST /api/v1/studio/source-uploads/{state['upload_id']}/chunk HTTP/1.1\r\nHost: 127.0.0.1\r\n"
           f"Authorization: {headers['Authorization']}\r\nContent-Type: multipart/form-data; boundary={boundary}\r\n"
           "Content-Length: 1049000\r\nConnection: close\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1",28080)) as connection:
        connection.sendall(raw+body)
    time.sleep(.3)
    result = httpx.get(base+"/status",headers=headers).json()
    assert result["received_chunks"] == [0] and result["bytes_received"] == 1024*1024, result
    block = source.read_bytes()[:1024*1024]
    duplicate = httpx.post(base+"/chunk", headers={**headers,"X-Chunk-SHA256":hashlib.sha256(block).hexdigest()},
        data={"chunk_index":0},files={"chunk_file":("chunk",block)})
    assert duplicate.status_code == 200
    vid = process_on_server(source,staging.base,staging.identities["OWNER"]["key"],None,{"title":"Resume actual TCP cut"},
        {"upload_id":state["upload_id"],"video_id":state["video_id"]},lambda *_:None,threading.Event(),lambda _:None,poll_seconds=300)
    assert vid == state["video_id"]
    # Two independent real uploads over concurrent HTTP connections.
    with ThreadPoolExecutor(max_workers=2) as pool:
        uploads = list(pool.map(lambda n:upload_source(staging,source,"Real concurrent "+str(n),chunk_size=1024*1024),range(2)))
    assert len({row["video_id"] for row in uploads}) == 2
    try:
        for worker in staging.workers:
            assert_owned(worker); docker("stop", "-t", "15", worker)
        with staging.client() as c:
            for row in uploads:
                response = c.post("/api/v1/admin/videos/"+row["video_id"]+"/transcode")
                assert response.status_code == 202,response.text
        queue = redis.Redis.from_url("redis://127.0.0.1:26379/5")
        queued_before = queue.llen("rq:queue:transcode")
        assert queued_before >= 2
        assert_owned("abv-f16-redis")
        docker("exec","abv-f16-redis","redis-cli","SAVE")
        docker("restart","abv-f16-redis")
        for _ in range(50):
            try:
                if queue.ping(): break
            except redis.RedisError: pass
            time.sleep(.1)
        assert queue.llen("rq:queue:transcode") == queued_before
    finally:
        for worker in staging.workers:
            staging.restart_worker(worker)
            docker("update","--cpus","8","--memory","3072m","--memory-swap","3072m",worker)
    for row in uploads: wait_ready(staging,row["video_id"],timeout=300)
    record(staging,"recovery",{"real_tcp_cut":True,"duplicate_chunk_idempotent":True,"resumed_same_video":True,
        "concurrent_uploads":2,"persisted_queued_jobs":queued_before,"redis_restart":"explicit RDB snapshot then restart",
        "both_jobs_ready":True,"recovery_quality_profile":["360p"]})


def test_real_studio_ass_srt_4k_package_worker(staging, tmp_path):
    import shutil
    from studio.config import load_studio_config, save_studio_config
    from studio.core.batch_runner import BatchRunner
    from studio.core.batch_store import BatchStore
    from studio.core.subtitles import process_softsub_file
    from studio.core.packager import build_and_verify_package
    from studio.core.uploader import upload_and_verify_package
    from studio.core.episode_scanner import scan_season_folder
    item = next(i for i in staging.media["media"] if i["id"] == "h264-ass")
    previous = load_studio_config()
    output = tmp_path / "output"; output.mkdir()
    save_studio_config({"processing_mode": "local", "cpu_concurrent_jobs": 1, "default_encoder": "cpu",
                        "include_360p": True, "converted_output_folder": str(output)})
    store = BatchStore(tmp_path / "studio.db")
    season = tmp_path / "series"; season.mkdir()
    shutil.copy2(item["path"], season / "Episode 01.mkv")
    episodes = scan_season_folder(season)
    assert len(episodes) == 1 and episodes[0]["subtitle_source"] == "embedded", episodes
    assert episodes[0]["state"] != "NEEDS_REVIEW", episodes[0].get("missing_fonts")
    batch = store.create_batch(str(season))
    episodes[0]["settings_json"] = {
            "encoder_choice": "cpu", "include_360p": True, "selected_qualities": ["360p", "720p", "2160p"],
            "default_preset": "balanced"}
    store.add_episodes(batch["id"], episodes)
    runner = BatchRunner(store); started = time.monotonic()
    try:
        runner.start_batch(batch["id"]); runner._worker_thread.join(360)
        assert not runner.is_running
        episode = store.get_episodes(batch["id"])[0]
        assert episode["state"] == "READY_TO_UPLOAD", (episode["state"], episode.get("error"))
        package = Path(episode["work_dir"])
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        assert any(s["format"] == "ass" for s in manifest["subtitles"])
        # Add a real downloaded SRT, retaining its original timing and text.
        sub = process_softsub_file(Path(staging.media["srt"]), package / "subtitles", "en")
        subtitles = manifest["subtitles"] + [{"language_code": "en", "language_name": "English", "label": "Real SRT",
            "is_default": False, "path": sub["primary_rel_path"], "format": sub["format"],
            "auxiliary_files": sub.get("auxiliary_files", [])}]
        manifest = build_and_verify_package(package, "Real 4K ASS and SRT excerpt", manifest["video"]["duration_seconds"],
            manifest["renditions"], subtitles=subtitles, fonts=manifest.get("fonts", []))["manifest"]
        local_seconds = time.monotonic() - started
        result = upload_and_verify_package(staging.base + "/api/v1", staging.identities["OWNER"]["key"],
                                           package, manifest, poll_timeout_seconds=120)
        assert result["success"], result
        staging.real_video = result["video_id"]
        record(staging, "studio_package", {"local_seconds": round(local_seconds, 3), "total_seconds": round(time.monotonic()-started, 3),
            "bytes": sum(p.stat().st_size for p in package.rglob("*") if p.is_file()),
            "qualities": [r["quality_label"] for r in manifest["renditions"]], "subtitles": [s["format"] for s in subtitles],
            "fonts": len(manifest.get("fonts", [])), "video_id": staging.real_video})
    finally:
        runner.shutdown(); save_studio_config(previous)


def test_real_hevc_4k_studio_native_gpu(staging, tmp_path, proxy):
    from studio.core.encoder import transcode_package_renditions
    from studio.core.packager import build_and_verify_package
    from studio.core.uploader import upload_and_verify_package
    item = next(i for i in staging.media["media"] if i["id"] == "hevc-mp4")
    package = tmp_path / "gpu"
    started = time.monotonic()
    result = transcode_package_renditions(Path(item["path"]),package,["360p","720p","2160p"],
        encoder_choice="gpu",include_360p=True,hls_segment_duration=2,preset="balanced")
    assert result["encoder"] in {"h264_nvenc","h264_qsv","h264_amf"}, result["encoder"]
    manifest = build_and_verify_package(package,"Real HEVC 4K GPU excerpt",result["duration_seconds"],result["renditions"])["manifest"]
    encoded = time.monotonic()-started
    uploaded = upload_and_verify_package(staging.base+"/api/v1",staging.identities["OWNER"]["key"],package,manifest,poll_timeout_seconds=120)
    assert uploaded["success"],uploaded
    from playwright.sync_api import sync_playwright
    with login(staging,proxy) as c, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);context=browser.new_context()
        context.add_cookies([{"name":cookie.name,"value":cookie.value,"url":proxy} for cookie in c.cookies.jar])
        page=context.new_page();errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        assert page.goto(proxy+"/watch/"+uploaded["video_id"],wait_until="networkidle").status==200
        page.locator("#big-play-btn").click()
        page.wait_for_function("() => document.querySelector('video').currentTime > .2",timeout=45000)
        page.locator('#quality-menu .menu-item:not(#qual-opt-auto)').evaluate_all("nodes => nodes.find(n=>n.textContent.includes('2160')).click()")
        page.wait_for_function("() => document.querySelector('video').videoHeight === 2160",timeout=45000)
        page.evaluate("() => {const v=document.querySelector('video');v.currentTime=12;}")
        page.wait_for_function("() => document.querySelector('video').currentTime >=12 && document.querySelector('video').readyState >= 2",timeout=30000)
        page.locator("#player-container").screenshot(path=str(staging.evidence.parent/"chromium-real-gpu-4k.png"))
        assert not errors,errors
        browser.close()
    record(staging,"gpu",{"encoder":result["encoder"],"encoding_seconds":round(encoded,3),
        "total_seconds":round(time.monotonic()-started,3),"qualities":[r["quality_label"] for r in result["renditions"]],
        "input":"real MP4 HEVC 3840x2160 excerpt","chromium_4k_playback_and_seek":True})


@pytest.fixture(scope="module")
def proxy(staging):
    api, nginx = "abv-staging-proxy-api", "abv-staging-nginx"
    created = []
    try:
        docker("run", "-d", "--name", api, *staging.options(), "--network-alias", "backend", "-e", "SERVE_MEDIA_VIA_NGINX=true",
               "--cpus","1","--memory","1024m",
               IMAGE, "python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log")
        created.append(api)
        configuration_mounts=[] if os.getenv("ABV_STAGING_NGINX_IMAGE") else [
            "--mount", f"type=bind,src={ROOT/'nginx/nginx.conf'},dst=/etc/nginx/nginx.conf,readonly",
            "--mount", f"type=bind,src={ROOT/'nginx/conf.d/default.conf'},dst=/etc/nginx/conf.d/default.conf,readonly"]
        docker("run", "-d", "--name", nginx, "--network", NETWORK, "--label", "abv.purpose=real-integration",
            "--label", "abv.run="+os.environ["ABV_INTEGRATION_RUN_ID"], "-p", "127.0.0.1:28082:80",
            "--cpus","0.5","--memory","256m",
            *configuration_mounts,
            "--mount", f"type=bind,src={ROOT/'backend/app/static'},dst=/var/www/static,readonly",
            "--mount", f"type=volume,src={VOLUME},dst=/var/www/storage,readonly", os.getenv("ABV_STAGING_NGINX_IMAGE","nginx:1.27.1-alpine3.20"))
        created.append(nginx)
        docker("exec", nginx, "nginx", "-t")
        base = "http://127.0.0.1:28082"
        for _ in range(120):
            try:
                response=httpx.get(base+"/health")
                if response.status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.2)
        else: raise AssertionError("Staging Nginx not healthy")
        record(staging,"nginx_headers",{key:len(response.headers.get_list(key)) for key in
            ["x-request-id","x-content-type-options","x-frame-options","referrer-policy"]})
        yield base
    finally:
        for name in reversed(created):
            assert_owned(name)
            (staging.temp/(name+".log")).write_text(docker("logs", name), encoding="utf-8")
            docker("rm", "-f", name)


@contextmanager
def login(staging, base):
    c = httpx.Client(base_url=base, timeout=30)
    r = c.post("/api/v1/auth/login", json={"username_or_email": staging.identities["OWNER"]["username"], "password": "Fixture-only-password-2026!"})
    assert r.status_code == 200
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    try:
        yield c
    finally:
        c.close()


def test_chromium_real_4k_seeking_quality_ass_srt(staging, proxy):
    from playwright.sync_api import sync_playwright
    assert hasattr(staging, "real_video"), "Real Studio package prerequisite failed"
    with login(staging, proxy) as c, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([{"name": cookie.name, "value": cookie.value, "url": proxy} for cookie in c.cookies.jar])
        page = context.new_page(); errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        started = time.monotonic()
        response = page.goto(proxy+"/watch/"+staging.real_video, wait_until="networkidle")
        assert response.status == 200
        page.locator("#big-play-btn").click()
        page.wait_for_function("() => document.querySelector('video').currentTime > 0.2", timeout=45000)
        startup = time.monotonic()-started
        for height in [360, 720, 2160]:
            page.locator('#quality-menu .menu-item:not(#qual-opt-auto)').evaluate_all("(nodes, height) => nodes.find(n => n.textContent.includes(String(height))).click()", height)
            try:
                page.wait_for_function("height => document.querySelector('video').videoHeight === height", arg=height, timeout=45000)
            except Exception:
                record(staging,"quality_failure",page.evaluate("() => {const v=document.querySelector('video'),h=window.hls;return {time:v.currentTime,duration:v.duration,height:v.videoHeight,paused:v.paused,ended:v.ended,error:v.error?.code,ready:v.readyState,currentLevel:h?.currentLevel,nextLevel:h?.nextLevel,loadLevel:h?.loadLevel,state:h?.streamController?.state,levels:h?.levels.map(l=>({height:l.height,bitrate:l.bitrate})),buffer:Array.from({length:v.buffered.length},(_,i)=>[v.buffered.start(i),v.buffered.end(i)])}}"))
                raise
            record(staging,"browser_progress",{"quality_height":height})
        page.evaluate("() => {const v=document.querySelector('video');v.currentTime=15;}")
        page.wait_for_function("() => document.querySelector('video').currentTime >= 15 && document.querySelector('video').readyState >= 2", timeout=30000)
        page.locator("#sub-opt-1").evaluate("node => node.click()")
        try:
            page.wait_for_function("() => Array.from(document.querySelector('video').textTracks).some(t => t.mode === 'showing' && t.cues && t.cues.length > 0)", timeout=30000)
        except Exception:
            record(staging,"subtitle_failure",page.evaluate("() => ({tracks:Array.from(document.querySelector('video').textTracks).map(t=>({mode:t.mode,label:t.label,cues:t.cues?.length})),elements:Array.from(document.querySelectorAll('video track')).map(t=>({readyState:t.readyState,file:new URL(t.src).pathname.split('/').pop()})),selected:document.querySelector('#subtitle-summary')?.textContent,errors:document.querySelector('video').error?.code})"))
            page.screenshot(path=str(staging.evidence.parent/"subtitle-failure.png"))
            raise
        page.locator("#sub-opt-0").evaluate("node => node.click()")
        page.wait_for_function("() => Boolean(window.jassubInstance)", timeout=30000)
        page.evaluate("async () => await window.jassubInstance._loaded")
        assert page.locator("#ass-canvas").is_visible()
        page.evaluate("() => {const v=document.querySelector('video');v.pause();v.currentTime=8;}")
        page.wait_for_function("() => document.querySelector('video').currentTime >= 8 && document.querySelector('video').readyState >= 2")
        # Real credit cues are in the upper half at t=8; the lower opaque mask
        # must not be mistaken for rendered subtitle text.
        page.wait_for_function("""() => {const s=document.querySelector('#ass-canvas');if(!s.width||!s.height)return false;
            const c=document.createElement('canvas');c.width=s.width;c.height=Math.floor(s.height/2);
            const ctx=c.getContext('2d');ctx.drawImage(s,0,0);return ctx.getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3&&v>0)}""",timeout=30000)
        page.locator("#player-container").screenshot(path=str(staging.evidence.parent/"chromium-real-ass-4k.png"))
        page.locator("#sub-opt-off").evaluate("node => node.click()")
        # A quick user selection must cancel in-flight ASS fonts/subtitle fetches.
        page.evaluate("() => {document.querySelector('#sub-opt-0').click();document.querySelector('#sub-opt-1').click();}")
        page.wait_for_timeout(1500)
        assert page.evaluate("() => window.jassubInstance === null"), "Stale ASS activation overrode the newer SRT selection"
        assert page.evaluate("() => Array.from(document.querySelector('video').textTracks).some(t=>t.mode==='showing')")
        page.locator("#sub-opt-off").evaluate("node => node.click()")
        assert not errors, errors
        record(staging, "browser", {"startup_including_navigation_seconds": round(startup,3), "qualities": [360,720,2160], "seek_seconds":15,
                                     "srt_cues_loaded": True, "ass_pixels_rendered":True, "engine":browser.version})
        browser.close()


def test_nginx_measured_viewer_load_expiration_and_persistence(staging, proxy):
    import redis
    assert hasattr(staging, "real_video")
    assert all(count==1 for count in staging.measurements["nginx_headers"][-1].values())
    with login(staging, proxy) as c:
        page = c.get("/watch/"+staging.real_video)
        token = re.search(r'/api/v1/playback/(s/[^/"\s]+)', page.text).group(1)
        master = c.get(f"/api/v1/playback/{token}/master.m3u8"); assert master.status_code == 200
        variants = [line for line in master.text.splitlines() if line and not line.startswith("#")]
        playlist = c.get(urljoin(str(master.request.url),variants[0])); assert playlist.status_code == 200
        segments = [urljoin(str(playlist.request.url),line) for line in playlist.text.splitlines() if line and not line.startswith("#")]
        warm_memory = None
        for viewers, requests, interval, profile in [(1,12,.25,"baseline"),(8,12,.25,"moderate"),
                                                     (24,30,2,"steady"),(24,12,.25,"burst")]:
            # Independent playback principals, one authorized owner account and NAT IP.
            clients = []
            urls = []
            for _ in range(viewers):
                client = httpx.Client(base_url=proxy,cookies=c.cookies,timeout=15)
                for cookie in list(client.cookies.jar):
                    if cookie.name == "abood_playback":
                        client.cookies.jar.clear(cookie.domain,cookie.path,cookie.name)
                response = client.get("/watch/"+staging.real_video)
                assert response.status_code == 200
                session = re.search(r'/api/v1/playback/(s/[^/"\s]+)',response.text).group(1)
                urls.append([url.replace(token,session) for url in segments])
                clients.append(client)
            time.sleep(2)
            before = json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", "abv-staging-proxy-api"))
            log_before=docker("logs","abv-staging-nginx")
            limited_before=log_before.count("limiting requests,")+log_before.count("limiting connections")
            def viewer(index):
                values = []
                try:
                    for request in range(requests):
                        start = time.monotonic()
                        response = clients[index].get(urls[index][request % len(urls[index])])
                        values.append((time.monotonic()-start, response.status_code, len(response.content)))
                        time.sleep(interval)
                finally:
                    clients[index].close()
                return values
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=viewers) as pool:
                results = [r for group in pool.map(viewer,range(viewers)) for r in group]
            elapsed = time.monotonic()-start
            log_after=docker("logs","abv-staging-nginx")
            throttled=log_after.count("limiting requests,")+log_after.count("limiting connections")-limited_before
            after = json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", "abv-staging-proxy-api"))
            def mib(value):
                size, unit = re.match(r"([\d.]+)(\w+)",value.split(" / ")[0]).groups()
                return float(size)*{"B":1/1048576,"KiB":1/1024,"MiB":1,"GiB":1024}[unit]
            if warm_memory is None: warm_memory = mib(after["MemUsage"])
            latencies = sorted(r[0]*1000 for r in results)
            success_latencies = sorted(r[0]*1000 for r in results if r[1]==200)
            counts = {str(code):sum(r[1]==code for r in results) for code in {r[1] for r in results}}
            record(staging, "load", {"viewers": viewers, "profile":profile, "interval_seconds":interval,"requests": len(results), "seconds": round(elapsed,3),
                "rps":round(len(results)/elapsed,2), "p50_ms": round(latencies[int(len(results)*.5)],2),
                "p95_ms":round(latencies[min(len(results)-1,int(len(results)*.95))],2), "statuses":counts,
                "nginx_recorded_throttle_rejections":throttled,
                "success_p95_ms":round(success_latencies[min(len(success_latencies)-1,int(len(success_latencies)*.95))],2) if success_latencies else None,
                "delivered_bytes":sum(r[2] for r in results if r[1]==200), "before":before,"after":after,
                "topology":"one NAT IP, independent playback sessions, actual Nginx production limits"})
            assert all(r[1] in {200, 429, 503} for r in results), counts
            assert any(r[1]==200 for r in results), counts
            assert throttled >= int(counts.get("503",0)), "A 503 without Nginx limiter evidence is a failure"
            if profile != "burst":
                assert all(r[1]==200 for r in results), counts
            time.sleep(3)
        time.sleep(10)
        idle = json.loads(docker("stats","--no-stream","--format","{{json .}}","abv-staging-proxy-api"))
        growth = mib(idle["MemUsage"])-warm_memory
        oom = docker("exec","abv-staging-proxy-api","cat","/sys/fs/cgroup/memory.events")
        record(staging,"memory_stability",{"warm_to_idle_growth_mib":round(growth,3),"idle":idle,"cgroup_memory_events":oom,
            "scope":"short staging load only; not a long-duration leak or soak proof"})
        assert growth < 128
        assert re.search(r"^oom_kill 0$",oom,re.M)
        # Actual Redis expiry, not a fake authorization result.
        queue = redis.Redis.from_url("redis://127.0.0.1:26379/6")
        assert queue.expire("pb:sess:"+token.split("/")[1],1)
        time.sleep(1.2)
        assert c.get(str(master.request.url)).status_code in {401,403}
        assert c.get("/watch/"+staging.real_video).status_code == 200
        # Recreate the owned backend container while retaining DB and media volume.
        assert_owned(staging.api_name)
        docker("rm", "-f", staging.api_name)
        docker("run","-d","--name",staging.api_name,*staging.options(),"--cpus","1","--memory","1024m",
               "-p","127.0.0.1:28080:8000",IMAGE,"python","-m","uvicorn","app.main:app","--host","0.0.0.0",
               "--port","8000","--workers","2","--no-access-log")
        for _ in range(100):
            try:
                if httpx.get(staging.base+"/readiness").status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.2)
        else: raise AssertionError("API readiness did not recover")
        with staging.session() as db:
            revision = db.execute(text("select version_num from alembic_version")).scalar_one()
        rebuilt_page = c.get(staging.base+"/watch/"+staging.real_video)
        assert rebuilt_page.status_code == 200
        rebuilt_token = re.search(r'/api/v1/playback/(s/[^/"\s]+)',rebuilt_page.text).group(1)
        rebuilt_master = c.get(staging.base+f"/api/v1/playback/{rebuilt_token}/master.m3u8")
        assert rebuilt_master.status_code == 200 and "#EXTM3U" in rebuilt_master.text
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import update
        from app.models.session import UserSession
        with staging.session() as db:
            db.execute(update(UserSession).where(UserSession.user_id==staging.identities["OWNER"]["user"])
                       .values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
            db.commit()
        assert c.get(staging.base+"/api/v1/auth/me").status_code == 401
        assert c.get(str(rebuilt_master.request.url)).status_code in {401,403}
        record(staging, "recovery", {"api_container_recreated_ready":True,"persisted_media_readable":True,
            "auth_expiration_denied_playback":True,"schema_revision":revision})
