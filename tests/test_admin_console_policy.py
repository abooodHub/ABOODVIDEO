"""Focused regression checks for runtime admin quality and transfer policies."""
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

from app.api.v1.admin_quality_settings import (
    PlaybackQualityPayload,
    read_playback_quality,
    update_playback_quality,
)
from app.api.v1.admin_transfer_settings import (
    TransferPolicyPayload,
    get_transfer_policy,
    read_transfer_policy,
    update_transfer_policy,
)
from app.api.v1.playback import _enforce_quality, _filter_master_variants, _media_file_response
from app.core.config import settings
from app.models.setting import SystemSetting


class FakeSession:
    def __init__(self):
        self.rows = {}
        self.committed = False

    async def get(self, _model, key):
        return self.rows.get(key)

    def add(self, row):
        self.rows[row.key] = row

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_playback_quality_policy_persists_separately_and_hides_master_variants(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", True)
    db = FakeSession()
    initial = await read_playback_quality(Response(), db)
    assert "2160p" in initial["enabled_qualities"]
    await update_playback_quality(PlaybackQualityPayload(enabled_qualities=["360p", "720p"]), Response(), db)
    assert db.committed
    assert await read_playback_quality(Response(), db) == {
        "enabled_qualities": ["360p", "720p"],
        "available_qualities": ["360p", "480p", "720p", "1080p", "1440p", "2160p"],
    }
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720\n720p/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2,RESOLUTION=1920x1080\n1080p/index.m3u8\n"
    filtered = _filter_master_variants(master, None, {1080})
    assert "720p/index.m3u8" in filtered
    assert "1080p/index.m3u8" not in filtered


@pytest.mark.asyncio
async def test_direct_hls_quality_request_is_denied_by_runtime_policy(monkeypatch):
    from app.api.v1 import playback

    monkeypatch.setattr(playback, "session_allows_quality", lambda *_: True)
    db = FakeSession()
    db.rows["playback_quality_policy"] = SystemSetting(key="playback_quality_policy", value='["720p"]')
    context = {"session": {"generation_id": str(uuid.uuid4())}, "video": type("Video", (), {"id": uuid.uuid4()})()}
    with pytest.raises(HTTPException) as error:
        await _enforce_quality(context, "1080p/segment0001.ts", "session-id", db)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_transfer_settings_apply_nginx_rate_header_and_persist():
    db = FakeSession()
    await update_transfer_policy(TransferPolicyPayload(playback_kbps=2048, upload_kbps=1024, concurrent_uploads=3), Response(), db)
    assert await get_transfer_policy(db) == {"playback_kbps": 2048, "upload_kbps": 1024, "concurrent_uploads": 3}
    result = await read_transfer_policy(Response(), db)
    assert result["playback_kbps"] == 2048
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "SERVE_MEDIA_VIA_NGINX", True)
    try:
        response = _media_file_response(type("Video", (), {"id": uuid.uuid4()})(), Path("segment.ts"), "/internal_media/x/segment.ts", playback_kbps=2048)
        assert response.headers["X-Accel-Limit-Rate"] == str(2048 * 125)
        assert response.headers["X-Accel-Redirect"] == "/internal_media/x/segment.ts"
    finally:
        monkeypatch.undo()
