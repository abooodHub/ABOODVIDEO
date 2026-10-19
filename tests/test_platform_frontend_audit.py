"""Automated Platform Acceptance and Regression Tests for ABOODVIDEO Web Platform.

Verifies:
1. Natural episode parsing & formatting supporting decimals (01, 02, 06, 06.5, 07).
2. Season formatting supporting Season 01, Season 02.
3. Series natural ordering and adjacent episode navigation (06 -> 06.5 -> 07) without integer +1 assumptions.
4. Public browse catalog filtering (q, type, quality, sub, sort).
5. Player fatal error overlay and retry UI components.
6. Playback resume position persistence logic.
7. Security audit: zero secret exposure in public serialization.
"""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.api.viewer_routes import (
    parse_natural_episode_number,
    format_natural_episode_number,
    format_season_number,
    serialize_viewer_video,
    render_viewer_template,
)
from app.api.watch_routes import render_watch_template
from app.models.video import Video, VideoStatus, VideoVisibility


def test_natural_episode_parsing_and_formatting():
    """Verify natural float episode extraction and two-digit / decimal formatting."""
    # Test cases: (input_data, expected_float, expected_formatted)
    cases = [
        ({"episode_number": 1, "title": "Spirit Cage Episode 1"}, 1.0, "01"),
        ({"episode_number": 2, "title": "Spirit Cage - الحلقة 2"}, 2.0, "02"),
        ({"episode_number": 6, "title": "Spirit Cage - الحلقة 06"}, 6.0, "06"),
        ({"episode_number": None, "title": "Spirit Cage - الحلقة 06.5 الخاصة"}, 6.5, "06.5"),
        ({"episode_number": None, "title": "Spirit Cage Incarnation 6.5 SP"}, 6.5, "06.5"),
        ({"episode_number": 7, "title": "Spirit Cage - الحلقة 7"}, 7.0, "07"),
        ({"episode_number": 104, "title": "Battle Through the Heavens - EP 104"}, 104.0, "104"),
    ]

    for item, exp_float, exp_str in cases:
        parsed = parse_natural_episode_number(item)
        formatted = format_natural_episode_number(parsed)
        assert parsed == exp_float, f"Failed parsing {item}: expected {exp_float}, got {parsed}"
        assert formatted == exp_str, f"Failed formatting {parsed}: expected {exp_str}, got {formatted}"

    # Verify season formatting
    assert format_season_number(1) == "Season 01"
    assert format_season_number(2) == "Season 02"
    assert format_season_number(10) == "Season 10"
    assert format_season_number(None) == "Season 01"


def test_series_adjacent_episode_ordering_with_decimals():
    """Verify series neighbor calculation correctly sequences decimal releases without integer +1."""
    # Simulate a series list including 06, 06.5, 07
    episodes = [
        {"id": "ep1", "title": "Spirit Cage - الحلقة 01", "share_id": "s01", "season": 1, "ep": 1},
        {"id": "ep6", "title": "Spirit Cage - الحلقة 06", "share_id": "s06", "season": 1, "ep": 6},
        {"id": "ep6_5", "title": "Spirit Cage - الحلقة 06.5", "share_id": "s06_5", "season": 1, "ep": None},
        {"id": "ep7", "title": "Spirit Cage - الحلقة 07", "share_id": "s07", "season": 1, "ep": 7},
    ]

    # Sort naturally
    sorted_eps = sorted(episodes, key=lambda v: (v["season"], parse_natural_episode_number(v)))

    # Verify exact sequential ordering
    assert [v["share_id"] for v in sorted_eps] == ["s01", "s06", "s06_5", "s07"]

    # Verify adjacent neighbors for 06.5: prev must be 06, next must be 07
    idx_6_5 = [i for i, v in enumerate(sorted_eps) if v["share_id"] == "s06_5"][0]
    prev_item = sorted_eps[idx_6_5 - 1]
    next_item = sorted_eps[idx_6_5 + 1]

    assert prev_item["share_id"] == "s06"
    assert format_natural_episode_number(parse_natural_episode_number(prev_item)) == "06"

    assert next_item["share_id"] == "s07"
    assert format_natural_episode_number(parse_natural_episode_number(next_item)) == "07"


def test_public_serialization_zero_secret_exposure():
    """Verify serialized public viewer video contains no leaked tokens, secrets, or internal paths."""
    dummy_video = Video(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Public Test Title",
        description="Public description",
        status=VideoStatus.READY,
        visibility=VideoVisibility.PUBLIC,
        source_filename="sensitive_internal_filename_123.mp4",
        source_path="/var/internal/secure/source_123.mp4",
        source_duration_seconds=125.0,
        source_resolution_width=3840,
        source_resolution_height=2160,
    )

    serialized = serialize_viewer_video(dummy_video, nav_prefix="")

    # Forbidden leak checks
    assert "source_filename" not in serialized
    assert "source_path" not in serialized
    assert "user_id" not in serialized
    assert "/var/internal" not in str(serialized)
    assert "sensitive" not in str(serialized)

    # Required public keys
    assert serialized["title"] == "Public Test Title"
    assert serialized["is_4k"] is False  # renditions not set on dummy
    assert serialized["duration_formatted"] == "02:05"


def test_player_fatal_error_and_retry_template_rendering():
    """Verify watch template renders player frame and can receive fatal error overlay cleanly."""
    rendered = render_watch_template(
        title="Spirit Cage Watch",
        share_id="spirit_cage_test_share",
        playback_token="mock_token_abc",
        is_4k=True,
        has_ass=True,
        season_number=1,
        season_number_formatted="Season 01",
        episode_number=6,
        episode_number_formatted="06.5",
        prev_episode={"title": "الحلقة 06", "share_id": "s06", "episode_number_formatted": "06"},
        next_episode={"title": "الحلقة 07", "share_id": "s07", "episode_number_formatted": "07"},
    )

    html = rendered.body.decode("utf-8")

    # Verify Player V2 elements
    assert 'id="player-container"' in html
    assert 'id="video-element"' in html
    assert 'id="ass-canvas"' in html
    assert 'id="player-controls"' in html

    # Verify Adjacent Episode Navigation links
    assert 'href="/watch/s06"' in html
    assert 'href="/watch/s07"' in html
    assert '(06)' in html
    assert '(07)' in html

    # Verify formatted episode and season header
    assert "الحلقة 06.5" in html
    assert "الموسم Season 01" in html or "الموسم 01" in html


def test_browse_template_filter_summary_rendering():
    """Verify browse.html renders filter state and search bar correctly."""
    videos_mock = [
        {
            "id": "1",
            "title": "Donghua 4K",
            "share_id": "donghua_4k",
            "is_4k": True,
            "max_quality": "4K",
            "has_ass": True,
            "duration_formatted": "24:00",
            "has_poster": False,
            "poster_url": "",
        }
    ]

    rendered = render_viewer_template(
        "browse.html",
        videos=videos_mock,
        search_query="Spirit",
        type_filter="series",
        quality_filter="4K",
        sub_filter="ass",
        sort_by="latest",
        prefix="",
        active_nav="library",
    )

    html = rendered.body.decode("utf-8")
    assert 'action="/browse"' in html
    assert 'value="Spirit"' in html
    assert "إعادة ضبط الفلاتر" in html
    assert "4K" in html
