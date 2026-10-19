"""Per-upload encoder choices must not mutate saved defaults."""
import hashlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from pydantic import ValidationError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.main import create_application
from app.models.setting import SystemSetting
from app.models.video import TranscodeJob, Video, VideoStatus
from app.services.encoder_settings import EncoderSelection, ENCODER_SETTING_KEY
from app.services.quality_settings import SETTING_KEY
from test_encoder_workspace import admin_fixture, clean_encoder_policy, login
from test_upload_resumable import valid_sample_mp4


async def upload(client, headers, content):
    response = await client.post('/api/v1/uploads/initiate', json={'title':'Choice upload','filename':'choice.mp4','file_size_bytes':len(content)}, headers=headers)
    assert response.status_code == 201, response.text
    data = response.json()
    response = await client.post('/api/v1/uploads/'+data['upload_id']+'/chunk',data={'chunk_index':0},files={'chunk_file':('part',content,'application/octet-stream')},headers={**headers,'X-Chunk-SHA256':hashlib.sha256(content).hexdigest()})
    assert response.status_code == 200,response.text
    response = await client.post('/api/v1/uploads/'+data['upload_id']+'/finalize',headers=headers)
    assert response.status_code == 200,response.text
    return uuid.UUID(data['video_id'])


@pytest.mark.asyncio
async def test_finalize_then_conversion_captures_encoder_choice_without_saving_global(admin_fixture,clean_encoder_policy,valid_sample_mp4):
    app=create_application()
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://localhost') as client:
        headers=await login(client,admin_fixture,'owner')
        video_id=await upload(client,headers,valid_sample_mp4.read_bytes())
        async with AsyncSessionLocal() as db:
            video=await db.get(Video,video_id)
            assert video.status == VideoStatus.UPLOADED
            assert (await db.execute(select(func.count()).select_from(TranscodeJob).where(TranscodeJob.video_id==video_id))).scalar_one()==0
        path=f'/api/v1/admin/videos/{video_id}/transcode'
        selection={'preset':'slow','video_bitrate_percent':75,'enabled_qualities':['720p','360p']}
        assert (await client.post(path,json=selection)).status_code==403
        assert (await client.post(path,json={**selection,'enabled_qualities':['720p','720p']},headers=headers)).status_code==422
        response=await client.post(path,json=selection,headers=headers)
        assert response.status_code==202,response.text
        job_id=uuid.UUID(response.json()['job_id'])
        async with AsyncSessionLocal() as db:
            job=await db.get(TranscodeJob,job_id)
            assert job.configuration_snapshot['encoder']['preset']=='slow'
            assert job.configuration_snapshot['encoder']['video_bitrate_percent']==75
            assert job.configuration_snapshot['enabled_qualities']==['360p','720p']
            assert job.configuration_snapshot['quality_presets']['720p']['video_bitrate_kbps']==1875
            assert await db.get(SystemSetting,ENCODER_SETTING_KEY) is None
            assert await db.get(SystemSetting,SETTING_KEY) is None
        assert (await client.post(path,json=selection,headers=headers)).status_code==409
        second_id=await upload(client,headers,valid_sample_mp4.read_bytes())
        response=await client.post(f'/api/v1/admin/videos/{second_id}/transcode',headers=headers)
        assert response.status_code==202,response.text
        async with AsyncSessionLocal() as db:
            job=await db.get(TranscodeJob,uuid.UUID(response.json()['job_id']))
            assert job.configuration_snapshot['encoder']['preset']==settings.FFMPEG_PRESET
            assert job.configuration_snapshot['encoder']['video_bitrate_percent']==100


@pytest.mark.parametrize('changes',[{'enabled_qualities':[]},{'enabled_qualities':['720p','720p']},{'enabled_qualities':['8k']},{'preset':'h264_nvenc'},{'video_bitrate_percent':151},{'threads':99}])
def test_job_selection_is_strict(changes):
    with pytest.raises(ValidationError):
        EncoderSelection.model_validate({'preset':'medium','video_bitrate_percent':100,'enabled_qualities':['720p'],**changes})


def test_job_selection_respects_4k_deployment_policy(monkeypatch):
    monkeypatch.setattr(settings,'ENABLE_4K_TRANSCODING',False)
    with pytest.raises(ValidationError):
        EncoderSelection(preset='medium',video_bitrate_percent=100,enabled_qualities=['2160p'])
