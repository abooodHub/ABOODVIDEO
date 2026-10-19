"""ABOODVIDEO Drive Phase 2 Regression Tests.

Validates deterministic media auto-detection, polymorphic sharing,
inherited download policy resolution, and move safety rules.
"""
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from app.services.media_detector import parse_filename_metadata
from app.models.folder import Folder
from app.models.series import Series, Season
from app.models.share import Share
from app.models.video import Video
from app.services.drive_service import DriveService


# =========================================================================
# 1. DETERMINISTIC MEDIA AUTO-DETECTION TESTS
# =========================================================================
def test_media_detector_standard_s01e02():
    parsed = parse_filename_metadata("[SILVER] Spirit_Cage_Incarnation_S01E02[4K].mkv")
    assert parsed["season_number"] == 1
    assert parsed["episode_number"] == 2
    assert parsed["quality"] == "2160p"
    assert parsed["suggested_title"] == "الحلقة 02"
    assert parsed["confidence"] == "HIGH"


def test_media_detector_01x02():
    parsed = parse_filename_metadata("Spirit.Cage.01x03.1080p.mkv")
    assert parsed["season_number"] == 1
    assert parsed["episode_number"] == 3
    assert parsed["quality"] == "1080p"
    assert parsed["suggested_title"] == "الحلقة 03"
    assert parsed["confidence"] == "HIGH"


def test_media_detector_season_words():
    parsed = parse_filename_metadata("Donghua Series Season 2 Episode 14 720p.mp4")
    assert parsed["season_number"] == 2
    assert parsed["episode_number"] == 14
    assert parsed["quality"] == "720p"
    assert parsed["suggested_title"] == "الحلقة 14"
    assert parsed["confidence"] == "HIGH"


def test_media_detector_dash_episode():
    parsed = parse_filename_metadata("Swallowed Star - 085 [1080p].mp4")
    assert parsed["episode_number"] == 85
    assert parsed["confidence"] in ("HIGH", "MEDIUM")
    assert parsed["suggested_title"] == "الحلقة 85"


def test_media_detector_folder_context():
    context = {"folder_type": "season", "season_number": 2, "series_name": "Spirit Cage"}
    parsed = parse_filename_metadata("Episode 05.mkv", folder_context=context)
    assert parsed["series_name"] == "Spirit Cage"
    assert parsed["season_number"] == 2
    assert parsed["episode_number"] == 5
    assert parsed["confidence"] == "HIGH"
    assert parsed["suggested_title"] == "الحلقة 05"


# =========================================================================
# 2. INHERITED DOWNLOAD POLICY LOGIC TESTS
# =========================================================================
@pytest.mark.asyncio
async def test_download_policy_explicit_video_override():
    """Video explicit DENY must override even if parent Season/Folder is ALLOW."""
    v = Video(
        id=uuid.uuid4(),
        title="Test Ep",
        source_filename="test.mp4",
        allow_download_policy="DENY",
    )
    # Mock db session
    class FakeDB:
        async def get(self, model, id):
            return None

    policy = await DriveService.get_effective_download_policy(FakeDB(), v)
    assert policy is False


@pytest.mark.asyncio
async def test_download_policy_season_inheritance():
    """Video with INHERIT must inherit Season's DENY policy."""
    season_id = uuid.uuid4()
    season = Season(
        id=season_id,
        series_id=uuid.uuid4(),
        season_number=1,
        display_name="الموسم 1",
        allow_download_policy="DENY",
    )
    video = Video(
        id=uuid.uuid4(),
        title="Ep 2",
        source_filename="ep2.mp4",
        season_id=season_id,
        allow_download_policy="INHERIT",
    )

    class FakeDB:
        async def get(self, model, ident):
            if model == Season and ident == season_id:
                return season
            return None

    policy = await DriveService.get_effective_download_policy(FakeDB(), video)
    assert policy is False


@pytest.mark.asyncio
async def test_download_policy_default_allow():
    """Default fallback without explicit DENY must be ALLOW (True)."""
    video = Video(
        id=uuid.uuid4(),
        title="Ep 1",
        source_filename="ep1.mp4",
        allow_download_policy="INHERIT",
    )

    class FakeDB:
        async def get(self, model, ident):
            return None

    policy = await DriveService.get_effective_download_policy(FakeDB(), video)
    assert policy is True


# =========================================================================
# 3. POLYMORPHIC SHARE MODEL TESTS
# =========================================================================
def test_share_is_active_properties():
    now = datetime.now(timezone.utc)
    # Active share
    s1 = Share(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        target_type="season",
        target_id=uuid.uuid4(),
        token="tok1",
        expires_at=now + timedelta(days=7),
        revoked_at=None,
    )
    assert s1.is_active is True

    # Revoked share
    s2 = Share(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        target_type="season",
        target_id=uuid.uuid4(),
        token="tok2",
        expires_at=None,
        revoked_at=now,
    )
    assert s2.is_active is False

    # Expired share
    s3 = Share(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        target_type="video",
        target_id=uuid.uuid4(),
        token="tok3",
        expires_at=now - timedelta(hours=1),
        revoked_at=None,
    )
    assert s3.is_active is False
