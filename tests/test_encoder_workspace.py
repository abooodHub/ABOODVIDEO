"""Encoder workspace policies, authorization, immutable plans, and real media."""
import json
import subprocess
from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.main import create_application
from app.models.setting import SystemSetting
from app.services.encoder_settings import ENCODER_SETTING_KEY, EncoderPolicy
from app.services.quality_settings import SETTING_KEY
from app.services.transcode_plan import build_transcode_configuration_snapshot, validate_transcode_configuration_snapshot, build_legacy_transcode_configuration_snapshot
from app.services.transcoder import select_quality_ladder, execute_transcode_rendition, probe_source
from test_admin_auth_and_management import admin_fixture

ENDPOINT = '/api/v1/admin/quality-settings/encoder'


@pytest.fixture
async def clean_encoder_policy():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SystemSetting).where(SystemSetting.key.in_([SETTING_KEY, ENCODER_SETTING_KEY])))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SystemSetting).where(SystemSetting.key.in_([SETTING_KEY, ENCODER_SETTING_KEY])))
        await db.commit()


async def login(client, fixture, role):
    response = await client.post('/api/v1/auth/login', json={'username_or_email': fixture[role].username, 'password': fixture[role + '_pass']})
    assert response.status_code == 200, response.text
    return {'X-CSRF-Token': response.json()['csrf_token']}


@pytest.mark.asyncio
async def test_encoder_auth_csrf_save_and_immutable_snapshots(admin_fixture, clean_encoder_policy):
    app = create_application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost') as client:
        assert (await client.get(ENDPOINT)).status_code == 401
        await login(client, admin_fixture, 'user')
        assert (await client.get(ENDPOINT)).status_code == 403
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost') as client:
        headers = await login(client, admin_fixture, 'owner')
        initial = await client.get(ENDPOINT)
        assert initial.status_code == 200 and initial.headers['cache-control'] == 'no-store'
        assert initial.json()['preset'] == settings.FFMPEG_PRESET
        payload = {'preset': 'medium', 'video_bitrate_percent': 75, 'enabled_qualities': ['720p']}
        assert (await client.put(ENDPOINT, json=payload)).status_code == 403
        saved = await client.put(ENDPOINT, json=payload, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()['quality_presets']['720p']['video_bitrate_kbps'] == 1875
        assert (await client.put(ENDPOINT, json={**payload, 'enabled_qualities': ['720p', '720p']}, headers=headers)).status_code == 422
        async with AsyncSessionLocal() as db:
            snapshot = await build_transcode_configuration_snapshot(db)
        original = deepcopy(snapshot)
        assert snapshot['encoder']['preset'] == 'medium'
        assert snapshot['quality_presets']['720p']['video_bitrate_kbps'] == 1875
        assert validate_transcode_configuration_snapshot(snapshot) == snapshot
        changed = await client.put(ENDPOINT, json={**payload, 'preset': 'veryfast', 'video_bitrate_percent': 100, 'enabled_qualities': ['1080p']}, headers=headers)
        assert changed.status_code == 200
        async with AsyncSessionLocal() as db:
            second = await build_transcode_configuration_snapshot(db)
        assert snapshot == original
        assert second['encoder']['preset'] == 'veryfast'
        native = select_quality_ladder(1280, 720, 3_000_000, second['enabled_qualities'], second['quality_presets'], True, second['source_fallback_presets'])
        assert native[0]['quality_label'] == 'source'
        assert native[0]['height'] == 720 and native[0]['video_bitrate_kbps'] == 2500
        page = await client.get('/admin/settings')
        assert page.status_code == 200 and 'id="tab-encoder"' in page.text
        upload = await client.get('/admin/upload')
        assert 'upload-quality-options' not in upload.text and 'enable-season-upload' not in upload.text
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost') as client:
        await login(client, admin_fixture, 'admin')
        assert (await client.get(ENDPOINT)).status_code == 200


@pytest.mark.parametrize('changes', [{'preset':'h264_nvenc'}, {'preset':'medium;shutdown'}, {'video_bitrate_percent':49}, {'video_bitrate_percent':151}, {'video_bitrate_percent':True}, {'video_bitrate_percent':'75'}, {'video_bitrate_percent':75.5}, {'crf':23}])
def test_encoder_policy_rejects_unsupported_values(changes):
    with pytest.raises(ValidationError):
        EncoderPolicy.model_validate({'preset':'veryfast', 'video_bitrate_percent':100, **changes})


def test_native_fallback_uses_source_appropriate_bitrate_and_legacy_snapshots_still_validate():
    assert select_quality_ladder(1280,720,3_000_000,['1080p'])[0]['video_bitrate_kbps'] == 2500
    assert select_quality_ladder(426,240,3_000_000,['1080p'])[0]['video_bitrate_kbps'] == 700
    assert validate_transcode_configuration_snapshot(build_legacy_transcode_configuration_snapshot())


@pytest.mark.asyncio
async def test_encoder_policy_snapshot_drives_real_ffmpeg(tmp_path, clean_encoder_policy):
    from app.api.v1.admin_quality_settings import EncoderSettingsPayload, update_encoder_settings
    from fastapi import Response
    async with AsyncSessionLocal() as db:
        await update_encoder_settings(EncoderSettingsPayload(preset='medium',video_bitrate_percent=75,enabled_qualities=['1080p']),Response(),db)
        snapshot = await build_transcode_configuration_snapshot(db)
    source = tmp_path/'encoder-source.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=1280x720:rate=12:duration=1','-c:v','libx264','-preset','ultrafast',str(source)],check=True,capture_output=True,timeout=45)
    info = probe_source(source)
    ladder = select_quality_ladder(1280,720,3_000_000,snapshot['enabled_qualities'],snapshot['quality_presets'],True,snapshot['source_fallback_presets'])
    rendition = ladder[0]
    assert rendition['video_bitrate_kbps'] == 1875
    output = tmp_path/'encoded'
    execute_transcode_rendition(source,rendition,output,info['fps'],False,info['duration'],0,1,{'current':5.0},encoding_config={**snapshot['encoder'],'segment_duration_seconds':2})
    actual = probe_source(next(output.glob('*.ts')))
    assert (actual['width'],actual['height'],actual['video_codec']) == (1280,720,'h264')
    assert '#EXT-X-ENDLIST' in (output/'index.m3u8').read_text()
