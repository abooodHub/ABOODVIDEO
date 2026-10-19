"""Embed endpoint rendering and scoped frame policy regressions."""
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.watch_routes import render_watch_template
from app.core.database import AsyncSessionLocal
from app.main import app
from app.models.video import Video, VideoStatus, VideoVisibility


def assert_embed_frame_policy(response):
    csp = response.headers["content-security-policy"]
    directive = next(part.strip() for part in csp.split(";") if part.strip().startswith("frame-ancestors "))
    allowed_ancestors = set(directive.split()[1:])
    assert "https://partner.example" in allowed_ancestors
    assert "https://preview.partner.test" in allowed_ancestors
    assert "x-frame-options" not in response.headers


def test_embed_template_contains_only_the_player_surface():
    html = render_watch_template(
        title="Embed fixture", share_id="embedfixture123456789", playback_token="test-token",
        series_name="Fixture series", episode_number=2, embed_mode=True,
    ).body.decode("utf-8")

    assert 'id="player-container"' in html
    assert 'id="video-element"' in html
    assert 'id="ass-canvas"' in html
    assert "viewer-header" not in html
    assert "viewer-footer" not in html
    assert "/admin" not in html
    assert "/browse" not in html
    assert '<div class="watch-details-container">' not in html
    assert "secret_key" not in html.lower()
    assert "api_key" not in html.lower()


@pytest.mark.asyncio
async def test_embed_valid_and_invalid_share_ids(test_user, monkeypatch):
    monkeypatch.setenv("ABOODVIDEO_EMBED_ORIGINS", "https://partner.example https://preview.partner.test")
    share_id = "embedfixture" + uuid.uuid4().hex[:12]
    video = Video(
        user_id=test_user.id,
        title="Embed acceptance fixture",
        source_filename="synthetic-fixture.mp4",
        status=VideoStatus.READY,
        visibility=VideoVisibility.PUBLIC,
        share_id=share_id,
        active_generation_id=uuid.uuid4(),
    )
    processing = Video(
        user_id=test_user.id,
        title="Unavailable embed fixture",
        source_filename="synthetic-processing-fixture.mp4",
        status=VideoStatus.PROCESSING,
        visibility=VideoVisibility.PUBLIC,
        share_id="embedprocessing" + uuid.uuid4().hex[:8],
    )
    trashed = Video(
        user_id=test_user.id,
        title="Trashed embed fixture",
        source_filename="synthetic-trashed-fixture.mp4",
        status=VideoStatus.READY,
        visibility=VideoVisibility.PUBLIC,
        share_id="embedtrashed" + uuid.uuid4().hex[:8],
        active_generation_id=uuid.uuid4(),
        trashed_at=datetime.now(timezone.utc),
    )
    async with AsyncSessionLocal() as db:
        db.add_all([video, processing, trashed])
        await db.commit()
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
                response = await client.get(f"/embed/{share_id}")
                assert response.status_code == 200
                assert 'id="video-element"' in response.text
                assert "viewer-header" not in response.text and "/admin" not in response.text
                assert_embed_frame_policy(response)

                normal = await client.get(f"/watch/{share_id}")
                assert normal.status_code == 200
                assert normal.headers["x-frame-options"] == "DENY"
                assert "frame-ancestors 'none'" in normal.headers["content-security-policy"]

                admin = await client.get("/admin")
                assert admin.headers["x-frame-options"] == "DENY"
                assert "frame-ancestors 'none'" in admin.headers["content-security-policy"]

                unavailable = await client.get(f"/embed/{processing.share_id}")
                assert unavailable.status_code == 404
                trashed_response = await client.get(f"/embed/{trashed.share_id}")
                assert trashed_response.status_code == 404
                for response in (unavailable, trashed_response):
                    assert_embed_frame_policy(response)

                missing = await client.get("/embed/does-not-exist-123456")
                assert missing.status_code == 404
                assert "video is unavailable" not in missing.text.lower()
                assert "/admin" not in missing.text and "/browse" not in missing.text
                assert_embed_frame_policy(missing)
        finally:
            await db.delete(video)
            await db.delete(processing)
            await db.delete(trashed)
            await db.commit()
