"""Unit & Integration Tests for Playback & Quality Inspector Service."""
import os
import sys
import uuid
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Ensure required environment variables for testing
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-inspector-testing-32-chars")
os.environ.setdefault("IP_PRIVACY_SALT", "test-salt-for-inspector-testing-32-chars")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,test,web")

from app.models.video import Video, VideoStatus, VideoVisibility, VideoRendition, VideoSubtitle
from app.services.playback_inspector import PlaybackInspectorService, playback_inspector


class TestPlaybackInspector(unittest.IsolatedAsyncioTestCase):
    """Isolated unit tests for PlaybackInspectorService using mocks and disposable temp fixtures."""

    async def asyncSetUp(self):
        self.inspector = PlaybackInspectorService()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_path = Path(self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_inspector_not_found(self):
        """Inspection of non-existent video returns structured FAIL with Arabic error message."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        result = await self.inspector.inspect(mock_db, "non_existent_share_id", probe_network=False)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("غير موجود", result["error"])
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["summary"]["score_pct"], 0)

    async def test_inspector_ready_video_full_pipeline(self):
        """Inspection of READY video with valid HLS, multi-qualities, and subtitles passes all checks."""
        video_id = uuid.uuid4()
        gen_id = uuid.uuid4()
        share_id = "test_share_ready_123"

        # Setup mock video
        video = Video(
            id=video_id,
            user_id=uuid.uuid4(),
            title="حلقة دونغهوا تجريبية",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PUBLIC,
            share_id=share_id,
            share_revoked=False,
            active_generation_id=gen_id,
            source_filename="episode_01.mp4",
            source_size_bytes=1024 * 1024 * 50,
            source_duration_seconds=1200.5,
            source_resolution_width=1920,
            source_resolution_height=1080,
            source_codec="h264",
            trashed_at=None,
        )

        # Setup temporary HLS generation files
        gen_dir = self.storage_path / str(video_id) / "generations" / str(gen_id)
        gen_dir.mkdir(parents=True, exist_ok=True)

        master_content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
            "1080p/index.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"\n'
            "720p/index.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=852x480,CODECS="avc1.4d401f,mp4a.40.2"\n'
            "480p/index.m3u8\n"
        )
        (gen_dir / "master.m3u8").write_text(master_content, encoding="utf-8")

        for q in ["1080p", "720p", "480p"]:
            q_dir = gen_dir / q
            q_dir.mkdir(parents=True, exist_ok=True)
            (q_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg_000.ts\n", encoding="utf-8")
            # Create valid TS segment starting with sync byte 0x47
            ts_data = b"\x47" + b"\x00" * 20000
            (q_dir / "seg_000.ts").write_bytes(ts_data)

        # Create subtitle file
        sub_file = self.storage_path / "subs.ass"
        sub_file.write_text("[Script Info]\nTitle: Arabic Sub\n", encoding="utf-8")

        sub = VideoSubtitle(
            id=uuid.uuid4(),
            video_id=video_id,
            language_code="ara",
            language_name="العربية",
            label="ترجمة عربية احترافية",
            format="ass",
            playback_renderer="jassub",
            is_default=True,
            is_playable=True,
            source_path=str(sub_file),
        )

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[sub])))),
        ]

        with patch("app.core.config.settings.STORAGE_VIDEOS_PATH", str(self.storage_path)):
            result = await self.inspector.inspect(mock_db, share_id, probe_network=False)

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["video"]["title"], "حلقة دونغهوا تجريبية")
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertGreaterEqual(result["summary"]["score_pct"], 90)

        # HLS renditions check
        self.assertTrue(result["hls"]["master_valid"])
        self.assertEqual(len(result["hls"]["renditions"]), 3)
        self.assertEqual(result["hls"]["renditions"][0]["quality"], "1080p")
        self.assertEqual(result["hls"]["renditions"][1]["quality"], "720p")
        self.assertEqual(result["hls"]["renditions"][2]["quality"], "480p")

        # Subtitle check
        self.assertEqual(len(result["subtitles"]), 1)
        self.assertTrue(result["subtitles"][0]["exists_on_disk"])

    async def test_inspector_unavailable_or_processing_video(self):
        """Inspection of PROCESSING video marks status as WARN and reports progress."""
        video = Video(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            title="فيديو قيد التحويل",
            status=VideoStatus.PROCESSING,
            visibility=VideoVisibility.PUBLIC,
            share_id="processing_123",
            share_revoked=False,
            active_generation_id=None,
            source_filename="input.mp4",
            source_size_bytes=1024,
            transcode_progress_percent=45.5,
            trashed_at=None,
        )

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]

        result = await self.inspector.inspect(mock_db, "processing_123", probe_network=False)
        self.assertIn(result["status"], ("WARN", "FAIL"))
        check_proc = next(c for c in result["checks"] if c["id"] == "video_status")
        self.assertEqual(check_proc["status"], "WARN")
        self.assertIn("45.5%", check_proc["detail"])

    async def test_inspector_trashed_video(self):
        """Inspection of trashed video accurately reports trash_status as WARN."""
        video = Video(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            title="فيديو في سلة المهملات",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PUBLIC,
            share_id="trashed_123",
            share_revoked=False,
            active_generation_id=uuid.uuid4(),
            source_filename="trashed.mp4",
            source_size_bytes=1024,
            trashed_at=datetime.now(timezone.utc),
        )

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]

        result = await self.inspector.inspect(mock_db, "trashed_123", probe_network=False)
        check_trash = next(c for c in result["checks"] if c["id"] == "trash_status")
        self.assertEqual(check_trash["status"], "WARN")
        self.assertTrue(result["video"]["is_trashed"])

    async def test_inspector_hls_failure_missing_master(self):
        """Missing master.m3u8 on disk causes hls_master_playlist check to FAIL."""
        video_id = uuid.uuid4()
        gen_id = uuid.uuid4()
        video = Video(
            id=video_id,
            user_id=uuid.uuid4(),
            title="فيديو مع master مفقود",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PUBLIC,
            share_id="missing_master_123",
            share_revoked=False,
            active_generation_id=gen_id,
            source_filename="input.mp4",
            source_size_bytes=1024,
            trashed_at=None,
        )

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]

        with patch("app.core.config.settings.STORAGE_VIDEOS_PATH", str(self.storage_path)):
            result = await self.inspector.inspect(mock_db, "missing_master_123", probe_network=False)

        check_hls = next(c for c in result["checks"] if c["id"] == "hls_master_playlist")
        self.assertEqual(check_hls["status"], "FAIL")
        self.assertFalse(result["hls"]["master_valid"])

    async def test_inspector_corrupt_hls_segment(self):
        """HLS segment lacking MPEG-TS sync byte 0x47 causes hls_segment_integrity check to FAIL."""
        video_id = uuid.uuid4()
        gen_id = uuid.uuid4()
        video = Video(
            id=video_id,
            user_id=uuid.uuid4(),
            title="فيديو مع مقطع تالف",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PUBLIC,
            share_id="corrupt_seg_123",
            share_revoked=False,
            active_generation_id=gen_id,
            source_filename="input.mp4",
            source_size_bytes=1024,
            trashed_at=None,
        )

        gen_dir = self.storage_path / str(video_id) / "generations" / str(gen_id)
        gen_dir.mkdir(parents=True, exist_ok=True)
        (gen_dir / "master.m3u8").write_text(
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n720p/index.m3u8\n',
            encoding="utf-8"
        )
        q_dir = gen_dir / "720p"
        q_dir.mkdir(parents=True, exist_ok=True)
        (q_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg_000.ts\n", encoding="utf-8")
        # Corrupt segment without 0x47
        (q_dir / "seg_000.ts").write_bytes(b"\x00\x01\x02\x03" * 500)

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]

        with patch("app.core.config.settings.STORAGE_VIDEOS_PATH", str(self.storage_path)):
            result = await self.inspector.inspect(mock_db, "corrupt_seg_123", probe_network=False)

        check_seg = next(c for c in result["checks"] if c["id"] == "hls_segment_integrity")
        self.assertEqual(check_seg["status"], "FAIL")

    async def test_inspector_missing_subtitle_file(self):
        """Subtitle entry whose file is missing on disk sets subtitles_availability check to WARN."""
        video_id = uuid.uuid4()
        video = Video(
            id=video_id,
            user_id=uuid.uuid4(),
            title="فيديو مع ترجمة مفقودة",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PUBLIC,
            share_id="missing_sub_123",
            share_revoked=False,
            active_generation_id=uuid.uuid4(),
            source_filename="input.mp4",
            source_size_bytes=1024,
            trashed_at=None,
        )

        sub = VideoSubtitle(
            id=uuid.uuid4(),
            video_id=video_id,
            language_code="ara",
            language_name="العربية",
            label="ترجمة مفقودة",
            format="ass",
            playback_renderer="jassub",
            is_default=True,
            is_playable=True,
            source_path="/non/existent/path/sub.ass",
        )

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[sub])))),
        ]

        with patch("app.core.config.settings.STORAGE_VIDEOS_PATH", str(self.storage_path)):
            result = await self.inspector.inspect(mock_db, "missing_sub_123", probe_network=False)

        check_sub = next(c for c in result["checks"] if c["id"] == "subtitles_availability")
        self.assertEqual(check_sub["status"], "WARN")
        self.assertFalse(result["subtitles"][0]["exists_on_disk"])

    def test_inspector_secret_masking(self):
        """Sensitive query params like tokens, keys, or secrets are sanitized from input reflections."""
        dirty_input = "share_123?token=SUPER_SECRET_TOKEN_XYZ&cookie=SECRET_SESSION"
        cleaned = PlaybackInspectorService._sanitize_string(dirty_input)
        self.assertNotIn("SUPER_SECRET_TOKEN_XYZ", cleaned)
        self.assertNotIn("SECRET_SESSION", cleaned)
        self.assertIn("[MASKED]", cleaned)

    async def test_unauthenticated_inspector_api_is_denied(self):
        """Unauthenticated requests to the inspector API endpoint are strictly rejected."""
        from httpx import ASGITransport, AsyncClient
        from app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            res = await client.post(
                "/api/v1/admin/inspector/check",
                json={"identifier": "test12345", "probe_network": False},
            )
            self.assertIn(res.status_code, (401, 403))

    async def test_unauthenticated_inspector_web_page_redirects_to_login(self):
        """Unauthenticated access to /admin/inspector web interface redirects to /admin/login."""
        from httpx import ASGITransport, AsyncClient
        from app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            res = await client.get("/admin/inspector", follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            self.assertEqual(res.headers.get("location"), "/admin/login")

    def test_embed_surface_isolation_and_protection(self):
        """Verify embed template contains exclusively the player surface and zero admin chrome."""
        from app.api.watch_routes import render_watch_template

        html = render_watch_template(
            title="Embed fixture",
            share_id="embedfixture123456789",
            playback_token="test-token",
            series_name="Fixture series",
            episode_number=2,
            embed_mode=True,
        ).body.decode("utf-8")

        self.assertIn('id="player-container"', html)
        self.assertIn('id="video-element"', html)
        self.assertIn('id="ass-canvas"', html)
        self.assertNotIn("viewer-header", html)
        self.assertNotIn("viewer-footer", html)
        self.assertNotIn("/admin", html)
        self.assertNotIn("/browse", html)
        self.assertNotIn('<div class="watch-details-container">', html)
        self.assertNotIn("secret_key", html.lower())
        self.assertNotIn("api_key", html.lower())

    async def test_inspector_network_probe_timeout(self):
        """When HTTP network probe times out, the inspector safely handles the exception and records WARN."""
        import httpx
        video_id = uuid.uuid4()
        video = Video(
            id=video_id,
            user_id=uuid.uuid4(),
            title="فيديو اختبار المهلة",
            status=VideoStatus.READY,
            visibility=VideoVisibility.PUBLIC,
            share_id="timeout_share_123",
            share_revoked=False,
            active_generation_id=uuid.uuid4(),
            source_filename="video.mp4",
            source_size_bytes=1000,
            trashed_at=None,
        )
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            MagicMock(scalar_one_or_none=MagicMock(return_value=video)),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]

        with patch.object(PlaybackInspectorService, "_probe_url", side_effect=TimeoutError("Connection timed out after 4.0s")):
            result = await self.inspector.inspect(mock_db, "timeout_share_123", probe_network=True)

        check_sec = next(c for c in result["checks"] if c["id"] == "embed_security_headers")
        self.assertEqual(check_sec["status"], "WARN")
        self.assertIn("timed out", check_sec["detail"])

    async def test_inspector_csrf_protection_enforcement(self):
        """When session cookie is present without matching CSRF token, check_csrf_protection raises 403."""
        from fastapi import HTTPException
        from app.security.csrf import check_csrf_protection
        from app.core.config import settings

        # 1. Request with session cookie but missing CSRF header
        req_missing = MagicMock()
        req_missing.method = "POST"
        req_missing.url.path = "/api/v1/admin/inspector/check"
        req_missing.cookies = {settings.SESSION_COOKIE_NAME: "dummy-session", settings.CSRF_COOKIE_NAME: "csrf-token-123"}
        req_missing.headers = {}

        with self.assertRaises(HTTPException) as ctx:
            await check_csrf_protection(req_missing)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("CSRF", ctx.exception.detail)

        # 2. Request with session cookie and mismatched CSRF header
        req_mismatch = MagicMock()
        req_mismatch.method = "POST"
        req_mismatch.url.path = "/api/v1/admin/inspector/check"
        req_mismatch.cookies = {settings.SESSION_COOKIE_NAME: "dummy-session", settings.CSRF_COOKIE_NAME: "csrf-token-123"}
        req_mismatch.headers = {"x-csrf-token": "wrong-token"}

        with self.assertRaises(HTTPException) as ctx2:
            await check_csrf_protection(req_mismatch)
        self.assertEqual(ctx2.exception.status_code, 403)
        self.assertIn("mismatch", ctx2.exception.detail)


if __name__ == "__main__":
    unittest.main()

