"""End-to-End Pipeline Integration Test for ABOODVIDEO Studio:
Inspect -> Multi-Rendition Transcode -> Assemble & SHA256 -> Upload -> Promote to READY.
"""
import hashlib
import json
import os
import subprocess
import uuid
import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import create_application
from app.core.config import settings
from app.core.database import get_db_context, SyncSessionLocal
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, VideoRendition
from app.models.package import PackageUploadSession, PackageSessionStatus
from app.workers.package_worker import verify_and_promote_package
from studio.core.encoder import transcode_package_renditions
from studio.core.packager import build_and_verify_package
from studio.core.probe import probe_video
from studio.core.subtitles import convert_to_webvtt


@pytest.fixture
def app():
    return create_application()


@pytest.fixture(scope="module")
def synthetic_video(tmp_path_factory) -> Path:
    """Creates a real 1-second 720p MP4 test video with AAC audio using FFmpeg."""
    tmp = tmp_path_factory.mktemp("e2e_video")
    vid = tmp / "clip_720p.mp4"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", "testsrc=size=1280x720:rate=25",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", "1.0",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        str(vid),
    ]
    subprocess.run(cmd, check=True)
    return vid


@pytest.mark.asyncio
async def test_studio_e2e_full_pipeline_to_ready(app, synthetic_video, tmp_path):
    """
    Complete End-to-End test of ABOODVIDEO Studio v0.1:
    1. Inspects video via ffprobe.
    2. Transcodes 360p & 720p renditions using Studio transcoding engine.
    3. Converts sample ASS subtitle to WebVTT.
    4. Assembles package, validates playlists, hashes all files with SHA-256, generates manifest.
    5. Inits session on ABOODVIDEO backend via Studio API.
    6. Streams all declared files (m3u8 playlists, .ts segments, subtitles, poster) to server.
    7. Finalizes session (receives HTTP 202).
    8. Executes package worker (verify_and_promote_package).
    9. Confirms video status is READY and all renditions are playable!
    """
    # 1. Probe
    probe = probe_video(synthetic_video)
    assert probe["width"] == 1280
    assert probe["height"] == 720
    assert probe["duration_seconds"] > 0

    # 2. Transcode
    work_dir = tmp_path / "studio_pkg_e2e"
    t_res = transcode_package_renditions(
        source_path=synthetic_video,
        output_dir=work_dir,
        selected_qualities=["360p", "720p"],
        encoder_choice="auto",
        hls_segment_duration=1,
        include_360p=True,
    )
    assert len(t_res["renditions"]) == 2

    # 3. Add Softsub
    sub_dir = work_dir / "subtitles"
    sub_dir.mkdir(parents=True, exist_ok=True)
    ass_file = tmp_path / "arabic.ass"
    ass_file.write_text("""[Script Info]
ScriptType: v4.00+
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.10,0:00:00.90,Default,,0,0,0,,تجربة استوديو عبود الكاملة
""", encoding="utf-8")

    vtt_file = sub_dir / "ar.vtt"
    convert_to_webvtt(ass_file, vtt_file)

    # 4. Assemble and Verify Package
    pkg_meta = build_and_verify_package(
        package_dir=work_dir,
        title="ABOODVIDEO Studio E2E Test",
        duration_seconds=probe["duration_seconds"],
        renditions=t_res["renditions"],
        subtitles=[{
            "language_code": "ar",
            "language_name": "العربية",
            "label": "العربية",
            "is_default": True,
            "path": "subtitles/ar.vtt",
        }],
    )
    manifest = pkg_meta["manifest"]
    assert len(manifest["files"]) >= 4  # 2 playlists, 2 segments, 1 vtt, 1 poster

    # 5. Authenticate and Upload to Backend
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Create Admin API key
        from app.security.api_key import generate_raw_api_key, hash_api_key
        from app.models.api_key import ApiKey
        raw_key = generate_raw_api_key()

        async with get_db_context() as db:
            user_res = await db.execute(select(User).where(User.role == UserRole.ADMIN))
            admin = user_res.scalars().first()
            if not admin:
                admin = User(
                    username="admin_e2e_studio",
                    email="admin_e2e_studio@aboodvideo.local",
                    role=UserRole.ADMIN,
                    is_active=True,
                )
                db.add(admin)
                await db.commit()
                await db.refresh(admin)

            api_key_obj = ApiKey(
                user_id=admin.id,
                name="Studio E2E Key",
                prefix=raw_key[:12],
                key_hash=hash_api_key(raw_key),
                scopes=["*"],
                is_active=True,
            )
            db.add(api_key_obj)
            await db.commit()

        # Step 5a: Init Package Session
        init_res = await client.post(
            "/api/v1/studio/packages/init",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"manifest": manifest},
        )
        assert init_res.status_code == 201
        session_info = init_res.json()
        session_id = session_info["session_id"]
        video_id = session_info["video_id"]
        server_files = session_info["files"]

        # Step 5b: Stream all files
        for s_file in server_files:
            rel = s_file["relative_path"]
            f_id = s_file["file_id"]
            abs_p = work_dir / rel
            assert abs_p.exists()

            with open(abs_p, "rb") as f_data:
                put_res = await client.put(
                    f"/api/v1/studio/packages/{session_id}/files/{f_id}",
                    headers={
                        "Authorization": f"Bearer {raw_key}",
                        "Content-Type": s_file["media_type"],
                    },
                    content=f_data.read(),
                )
                assert put_res.status_code == 200
                assert put_res.json()["status"] == "COMPLETED"

        # Step 5c: Finalize Session (202 Accepted)
        fin_res = await client.post(
            f"/api/v1/studio/packages/{session_id}/finalize",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert fin_res.status_code == 202
        assert fin_res.json()["status"] == "QUEUED_FOR_VERIFICATION"

    # 6. Execute Background RQ Worker Verification
    verify_and_promote_package(session_id)

    # 7. Assert Database State: Video is READY!
    with SyncSessionLocal() as sync_db:
        db_session = sync_db.execute(
            select(PackageUploadSession).where(PackageUploadSession.id == uuid.UUID(session_id))
        ).scalar_one()
        assert db_session.status == PackageSessionStatus.READY.value

        db_video = sync_db.execute(
            select(Video).where(Video.id == uuid.UUID(video_id))
        ).scalar_one()
        assert db_video.status == VideoStatus.READY.value
        assert db_video.active_generation_id is not None
        assert db_video.master_playlist_path is not None

        renditions = sync_db.execute(
            select(VideoRendition).where(VideoRendition.video_id == db_video.id)
        ).scalars().all()
        assert len(renditions) == 2
        labels = {r.quality_label for r in renditions}
        assert labels == {"360p", "720p"}
        assert all(r.is_ready for r in renditions)
