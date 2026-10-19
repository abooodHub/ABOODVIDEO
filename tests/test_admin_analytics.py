"""Unit & Integration Tests for Admin Analytics & Telemetry API."""
import os
import sys
import uuid
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-analytics-testing-32-chars")
os.environ.setdefault("IP_PRIVACY_SALT", "test-salt-for-analytics-testing-32-chars")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,test,web")

from app.models.video import Video, VideoStatus, PlaybackSession
from app.api.v1.admin_analytics import _format_bytes, _format_duration, get_analytics_overview, get_video_analytics
from app.api.v1.playback import PlaybackEventRequest, _track_event


class TestAdminAnalytics(unittest.IsolatedAsyncioTestCase):
    """Isolated tests for Admin Analytics, Telemetry, and Embed Metrics."""

    def test_format_bytes(self):
        """Verify byte formatting handles B, KB, MB, GB correctly."""
        self.assertEqual(_format_bytes(0), "0 B")
        self.assertEqual(_format_bytes(500), "500.0 B")
        self.assertEqual(_format_bytes(1024), "1.0 KB")
        self.assertEqual(_format_bytes(1048576), "1.0 MB")
        self.assertEqual(_format_bytes(1073741824), "1.0 GB")

    def test_format_duration(self):
        """Verify duration formatting into MM:SS and HH:MM:SS."""
        self.assertEqual(_format_duration(0), "00:00")
        self.assertEqual(_format_duration(65), "01:05")
        self.assertEqual(_format_duration(3665), "1:01:05")

    def test_playback_event_request_schema(self):
        """Verify PlaybackEventRequest parses quality, device_type, and referrer."""
        req = PlaybackEventRequest(
            event="heartbeat",
            position=45.0,
            duration=15.0,
            bytes_received=500000,
            quality="1080p",
            device_type="mobile",
            referrer="https://partner.example/episode/4"
        )
        self.assertEqual(req.quality, "1080p")
        self.assertEqual(req.device_type, "mobile")
        self.assertEqual(req.referrer, "https://partner.example/episode/4")
        self.assertEqual(req.bytes_received, 500000)

    async def test_track_event_records_telemetry(self):
        """Verify _track_event updates session with quality, device, referrer, and video bandwidth."""
        mock_db = AsyncMock()
        mock_video = MagicMock()
        mock_video.id = uuid.uuid4()
        mock_video.source_duration_seconds = 1200.0
        mock_video.view_count = 5
        mock_video.bandwidth_bytes = 1000000

        mock_request = MagicMock()
        mock_request.client.host = "127.0.0.1"
        mock_request.headers = {"User-Agent": "Mozilla/5.0 Test Mobile"}

        # Simulate existing session
        mock_session = PlaybackSession(
            id=uuid.uuid4(),
            video_id=mock_video.id,
            session_id="test_viewer_key_abc",
            ip_hash="fake_ip_hash",
            user_agent_short="Test UA",
            started_at=datetime.now(timezone.utc),
            last_heartbeat_at=datetime.now(timezone.utc),
            duration_seconds=5.0,
            bytes_served=250000,
            is_completed=False,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_session
        mock_db.execute.return_value = mock_result

        event_body = PlaybackEventRequest(
            event="heartbeat",
            duration=10.0,
            bytes_received=300000,
            quality="720p",
            device_type="mobile",
            referrer="https://partner.example/episode/1"
        )

        res = await _track_event(
            db=mock_db,
            request=mock_request,
            video=mock_video,
            body=event_body,
            viewer_key="test_viewer_key_abc"
        )

        self.assertEqual(res["status"], "ok")
        # Session duration should have advanced from 5.0 + 10.0 = 15.0
        self.assertEqual(mock_session.duration_seconds, 15.0)
        # Session should be marked completed (>= 10.0s threshold)
        self.assertTrue(mock_session.is_completed)
        self.assertEqual(mock_session.quality, "720p")
        self.assertEqual(mock_session.device_type, "mobile")
        self.assertEqual(mock_session.referrer_domain, "partner.example")
        # DB commit should be called
        mock_db.commit.assert_awaited_once()

    async def test_get_analytics_overview(self):
        """Verify get_analytics_overview aggregates KPIs, quality distribution, and referrers."""
        mock_db = AsyncMock()

        # Mock results for the queries executed in overview
        # 1. visitors, 2. views, 3. lifetime views, 4. bw, 5. lifetime bw, 6. duration
        call_returns = [
            MagicMock(scalar=MagicMock(return_value=42)),       # unique_visitors
            MagicMock(scalar=MagicMock(return_value=85)),       # engaged_views
            MagicMock(scalar=MagicMock(return_value=120)),      # lifetime_views
            MagicMock(scalar=MagicMock(return_value=50000000)), # bandwidth_bytes
            MagicMock(scalar=MagicMock(return_value=80000000)), # lifetime_bw
            MagicMock(scalar=MagicMock(return_value=720.5)),    # avg_duration
            MagicMock(all=MagicMock(return_value=[("1080p", 60), ("720p", 30), ("4K", 10)])), # qualities
            MagicMock(all=MagicMock(return_value=[("partner.example", 90), ("partner-site.com", 10)])), # referrers
            MagicMock(all=MagicMock(return_value=[("mobile", 65), ("desktop", 35)])), # devices
            MagicMock(all=MagicMock(return_value=[])), # top_videos
            MagicMock(scalar_one_or_none=MagicMock(return_value="1080p")), # highest ready rendition
        ]
        mock_db.execute.side_effect = call_returns

        mock_user = MagicMock()
        mock_user.role = "OWNER"

        overview = await get_analytics_overview(days=30, db=mock_db, current_user=mock_user)

        self.assertIn("kpis", overview)
        self.assertEqual(overview["kpis"]["views"], 85)
        self.assertEqual(overview["kpis"]["lifetime_views"], 120)
        self.assertEqual(overview["kpis"]["unique_visitors"], 42)
        self.assertEqual(overview["kpis"]["top_quality"], "1080p")
        self.assertEqual(len(overview["quality_distribution"]), 3)
        self.assertEqual(overview["quality_distribution"][0]["quality"], "1080p")
        self.assertEqual(overview["quality_distribution"][0]["percentage"], 60.0)
        self.assertEqual(len(overview["referrer_domains"]), 2)
        self.assertEqual(overview["referrer_domains"][0]["domain"], "partner.example")


if __name__ == "__main__":
    unittest.main()
