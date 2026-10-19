"""Single End-to-End Test for ABOODVIDEO Studio 0.3.0 and Live Platform."""
import os
import sys
import json
import time
import shutil
import subprocess
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, r"C:\aboodvideo")

from studio.config import load_studio_config, save_studio_config, get_converted_output_root
from studio.core.probe import probe_video
from studio.core.job_runner import StudioJob
from studio.core.process import run_headless
import requests

def get_remote_share_id(video_id: str) -> str:
    cmd = [
        "ssh", "-o", "BatchMode=yes", "abood@127.0.0.1",
        f"docker exec aboodvideo_postgres psql -U aboodvideo_app -d aboodvideo_clean -t -A -c \"SELECT share_id FROM videos WHERE id = '{video_id}';\""
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()

def run_e2e_test():
    print("=" * 70)
    print("ABOODVIDEO Studio 0.3.0 — Single End-to-End Acceptance Test")
    print("=" * 70)

    cfg = load_studio_config()
    server_url = cfg.get("server_url", "https://platform.example/api/v1")
    api_key = cfg.get("api_key", "")
    assert bool(api_key), "API key not configured in DPAPI store!"

    # 1. Prepare disposable test video fixture
    test_fixture_dir = Path(r"C:\aboodvideo\test_e2e_fixture")
    test_fixture_dir.mkdir(parents=True, exist_ok=True)
    test_video_path = test_fixture_dir / "test_e2e_sample.mkv"
    
    print("\n[Step 1] Creating disposable synthetic 1080p test video with FFmpeg...")
    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\tools\bin\ffmpeg.exe"
    gen_cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", "4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(test_video_path)
    ]
    res = subprocess.run(gen_cmd, check=True)
    assert test_video_path.exists() and test_video_path.stat().st_size > 5000
    print(f"   -> Created: {test_video_path.name} ({test_video_path.stat().st_size} bytes)")

    # 2. Inspect metadata (Probe)
    print("\n[Step 2] Probing video metadata...")
    probe = probe_video(test_video_path)
    print(f"   -> Resolution: {probe['width']}x{probe['height']}")
    print(f"   -> Duration: {probe['duration_seconds']:.2f}s | FPS: {probe['fps']} | Codec: {probe['video_codec']}")
    assert probe["width"] == 1920 and probe["height"] == 1080
    assert probe["duration_seconds"] > 3.0

    # 3. Select converted output folder
    print("\n[Step 3] Configuring converted output destination...")
    output_root = Path(r"C:\aboodvideo\converted_output")
    output_root.mkdir(parents=True, exist_ok=True)
    save_studio_config({
        "converted_output_folder": str(output_root),
        "output_organization": "automatic"
    })
    confirmed_root = get_converted_output_root(require_configured=True)
    print(f"   -> Target Output Root: {confirmed_root}")

    # 4. Transcode with NVIDIA NVENC & Package
    print("\n[Step 4] Transcoding with NVIDIA NVENC & Packaging Renditions (1080p, 720p, 480p)...")
    job = StudioJob(
        video_path=str(test_video_path),
        title="E2E Acceptance Sample Video",
        selected_qualities=["1080p", "720p", "480p"],
        encoder_choice="h264_nvenc",
        subtitle_mode="none",
        server_url=server_url,
        api_key=api_key,
        auto_upload=True
    )

    events = []
    def on_progress(state):
        events.append(dict(state))
        status = state.get("status")
        pct = state.get("progress_pct", 0)
        desc = state.get("stage_description", "")
        fps = state.get("fps", 0)
        print(f"\r   [{status.upper()}] {pct:.1f}% - {desc} (FPS: {fps})", end="", flush=True)

    job.subscribe(on_progress)
    print("   Starting unified pipeline execution...")
    job._run()  # Run synchronously for test verification
    print("\n   Pipeline finished.")

    final_state = job.state
    assert final_state["status"] in ("completed", "complete"), f"Job did not complete: {final_state.get('error')}"
    print(f"   -> Job status: {final_state['status']}")

    # 5. Verify local output package
    print("\n[Step 5] Verifying local output package...")
    result_data = final_state.get("result", {})
    server_video_id = result_data.get("video_id")
    session_id = result_data.get("session_id")
    
    # Find package directory under output_root
    package_dirs = [p for p in output_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    assert len(package_dirs) > 0, f"No package found in {output_root}"
    package_dir = package_dirs[0]
    print(f"   -> Package directory: {package_dir}")
    manifest_file = package_dir / "manifest.json"
    assert manifest_file.exists(), "manifest.json missing in package!"
    poster_file = package_dir / "poster.webp"
    assert poster_file.exists(), "poster.webp missing in package!"
    
    for q in ["1080p", "720p", "480p"]:
        q_dir = package_dir / q
        assert q_dir.exists(), f"Rendition directory {q} missing!"
        assert (q_dir / "index.m3u8").exists(), f"Playlist index.m3u8 for {q} missing!"
        segments = list(q_dir.glob("*.ts"))
        assert len(segments) > 0, f"Segments for {q} missing!"
        print(f"     * {q}: index.m3u8 + {len(segments)} segments verified.")

    # 6. Verify Server Registration & Playback Readiness
    print("\n[Step 6] Verifying Server Registration & Playback Readiness...")
    print(f"   -> Session ID: {session_id}")
    print(f"   -> Server Video ID: {server_video_id}")
    assert session_id, "Session ID missing in upload result!"
    assert server_video_id, "Server Video ID missing in upload result!"

    headers = {"Authorization": f"Bearer {api_key}"}
    
    # 6a. Verify Studio Package Session via API Key
    pkg_status_url = f"{server_url}/studio/packages/{session_id}"
    pkg_resp = requests.get(pkg_status_url, headers=headers, timeout=10)
    print(f"   -> GET /api/v1/studio/packages/{session_id}: HTTP {pkg_resp.status_code}")
    assert pkg_resp.status_code == 200, f"Failed to get package session: {pkg_resp.text}"
    pkg_data = pkg_resp.json()
    print(f"   -> Package Session Status: {pkg_data.get('status')}")
    assert pkg_data.get("status") == "READY", f"Expected READY, got {pkg_data.get('status')}"

    # 6b. Verify Watch Page via share_id
    share_id = get_remote_share_id(server_video_id)
    print(f"   -> Video Share ID: {share_id}")
    assert share_id, "Share ID not found for video!"
    
    base_host = server_url.replace("/api/v1", "")
    watch_url = f"{base_host}/watch/{share_id}"
    watch_resp = requests.get(watch_url, timeout=10)
    print(f"   -> GET /watch/{share_id}: HTTP {watch_resp.status_code}")
    assert watch_resp.status_code == 200, f"Watch page failed: {watch_resp.status_code}"
    assert "E2E Acceptance Sample Video" in watch_resp.text
    print("   -> Watch page rendered successfully with 200 OK and video title present!")
    
    print("\n" + "=" * 70)
    print("END-TO-END ACCEPTANCE TEST COMPLETED WITH 100% SUCCESS!")
    print("=" * 70)
    
    # Cleanup test fixture
    shutil.rmtree(test_fixture_dir, ignore_errors=True)

if __name__ == "__main__":
    run_e2e_test()
