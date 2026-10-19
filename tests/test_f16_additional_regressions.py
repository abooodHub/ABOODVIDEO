"""Contract coverage for ownership, resumption and actual runner dispatch."""
import asyncio
import hashlib
import threading
import time
import uuid
from types import SimpleNamespace
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import delete

from app.models.user import User, UserRole
from app.models.folder import Folder
from app.models.video import Video, VideoStatus
from app.core.database import AsyncSessionLocal
from test_f16_regressions import video


async def test_f01_queued_redis_outage_preserves_retry_budget_and_staging(monkeypatch):
    from app.workers import reconciler as m
    from redis.exceptions import ConnectionError
    record=SimpleNamespace(id=uuid.uuid4(),status="QUEUED_FOR_VERIFICATION",verification_job_id="fixture",finalize_attempts=3)
    db=AsyncMock();empty=MagicMock();empty.scalars.return_value.all.return_value=[]
    queued=MagicMock();queued.scalars.return_value.all.return_value=[record]
    db.execute.side_effect=[empty,empty,queued]
    @asynccontextmanager
    async def context():yield db
    monkeypatch.setattr(m,"get_db_context",context)
    monkeypatch.setattr(m.UploadService,"cleanup_expired_sessions",AsyncMock(return_value=0))
    monkeypatch.setattr(m.Job,"fetch",MagicMock(side_effect=ConnectionError("synthetic outage")))
    cleanup=MagicMock();monkeypatch.setattr(m.PackageStorageCleanup,"remove_terminal_staging",cleanup)
    assert await m.reconcile_stalled_package_jobs()==0
    assert record.status=="QUEUED_FOR_VERIFICATION" and record.finalize_attempts==3
    cleanup.assert_not_called()


async def test_f02_parallel_sessions_enforce_principal_limit_atomically(monkeypatch):
    from app.security.playback_session import create_playback_principal,create_playback_session,_skey,revoke_playback_principal
    from app.core.redis import get_session_client
    from app.core.config import settings
    redis=get_session_client();monkeypatch.setattr(settings,"PLAYBACK_MAX_CONCURRENT_SESSIONS",5)
    secret=await create_playback_principal(redis,user_id=None,external_user_id=None,plan="free",max_quality="480p",download_allowed=False,concurrent_stream_limit=2)
    ids=await asyncio.gather(*[create_playback_session(redis,principal_secret=secret,video_id=uuid.uuid4(),generation_id=uuid.uuid4(),authorization_type="public",max_quality="480p",download_allowed=False) for _ in range(12)])
    assert sum(await asyncio.gather(*[redis.exists(_skey(sid)) for sid in ids]))==2
    await revoke_playback_principal(redis,secret)


async def test_f03_owner_cannot_bulk_change_read_or_upload_to_foreign_destination(db_session,test_user):
    from app.api.v1.admin_videos import bulk_videos,BulkVideoRequest
    from app.api.v1.admin_subtitles import list_subtitles
    from app.api.v1.admin_transcodes import trigger_video_transcode
    from app.services.upload_service import UploadService,UploadException
    from app.services.drive_service import DriveService
    other=User(id=uuid.uuid4(),email=f"other-{uuid.uuid4()}@test.local",username=str(uuid.uuid4()),hashed_password="synthetic",role=UserRole.OWNER)
    db_session.add(other);await db_session.flush()
    own,foreign=video(test_user.id),video(other.id)
    folder=Folder(id=uuid.uuid4(),user_id=other.id,name="Foreign")
    db_session.add_all([own,foreign,folder]);await db_session.commit()
    with pytest.raises(HTTPException) as e:
        await bulk_videos(BulkVideoRequest(ids=[own.id,foreign.id],action="category",category="Changed"),Response(),db_session,test_user)
    assert e.value.status_code==404 and own.category is None
    with pytest.raises(HTTPException):await list_subtitles(foreign.id,test_user,db_session)
    with pytest.raises(HTTPException):await trigger_video_transcode(foreign.id,0,test_user,db_session)
    with pytest.raises(UploadException):await UploadService.initiate_upload(db_session,test_user,"Synthetic","source.mp4",1,folder_id=folder.id)
    with pytest.raises(ValueError):await DriveService.detect_media(db_session,scope_id=str(folder.id),owner_id=test_user.id)
    test_user.role=UserRole.ADMIN
    assert await list_subtitles(foreign.id,test_user,db_session)==[]
    test_user.role=UserRole.OWNER
    await db_session.delete(own);await db_session.delete(other);await db_session.commit()


@pytest.mark.parametrize("destination",["folder","series","season","video"])
async def test_f03_package_destination_does_not_grant_owner_admin_override(db_session,test_user,tmp_path,monkeypatch,destination):
    from app.services import package_service as m
    from app.schemas.package import PackageInitRequest
    from app.models.series import Series,Season
    from app.core.exceptions import AboodVideoException
    from test_f16_regressions import synthetic_package
    other=User(id=uuid.uuid4(),email=f"foreign-{uuid.uuid4()}@test.local",username=str(uuid.uuid4()),hashed_password="synthetic",role=UserRole.OWNER)
    db_session.add(other);await db_session.flush()
    folder=Folder(id=uuid.uuid4(),user_id=other.id,name="Foreign")
    series=Series(id=uuid.uuid4(),owner_id=other.id,title="Foreign")
    db_session.add_all([folder,series]);await db_session.flush()
    season=Season(id=uuid.uuid4(),series_id=series.id,season_number=1,display_name="First")
    foreign=video(other.id);db_session.add_all([season,foreign]);await db_session.commit()
    obj={"folder":folder,"series":series,"season":season,"video":foreign}[destination]
    req=PackageInitRequest(manifest=synthetic_package(tmp_path),**{destination+"_id":obj.id})
    monkeypatch.setattr(m,"check_package_storage_admission",lambda **kw:None)
    with pytest.raises(AboodVideoException) as error:
        await m.PackageService.init_package_session(db_session,req,SimpleNamespace(id=uuid.uuid4(),user_id=test_user.id))
    assert error.value.status_code==403
    await db_session.delete(other);await db_session.commit()


async def test_f03_source_upload_session_is_owner_scoped(db_session,test_user,monkeypatch):
    from app.services.upload_service import UploadService,UploadException
    other=User(id=uuid.uuid4(),email=f"foreign-{uuid.uuid4()}@test.local",username=str(uuid.uuid4()),hashed_password="synthetic",role=UserRole.OWNER)
    db_session.add(other);await db_session.commit()
    monkeypatch.setattr(UploadService,"check_disk_reservation",AsyncMock())
    session=await UploadService.initiate_upload(db_session,other,"Synthetic","source.mp4",1)
    with pytest.raises(UploadException) as error:await UploadService.get_session(db_session,session.id,test_user)
    assert error.value.status_code==403
    test_user.role=UserRole.ADMIN
    assert (await UploadService.get_session(db_session,session.id,test_user)).id==session.id
    test_user.role=UserRole.OWNER
    await db_session.delete(other);await db_session.commit()


async def test_f15_disposable_schema_has_nonnullable_explicit_flag(db_session):
    from sqlalchemy import text
    record=(await db_session.execute(text("SELECT is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='videos' AND column_name='is_test_content'"))).one()
    assert record.is_nullable=="NO" and record.column_default=="false"
    assert (await db_session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()=="0019_explicit_test_content"


@pytest.mark.parametrize("damage",["missing","corrupt"])
async def test_f04_f16_retry_repairs_file_and_exact_progress(db_session,test_user,tmp_path,damage):
    from test_f16_regressions import seed_package
    from app.services.package_service import PackageService
    content=b"verified synthetic segment"*100
    v,s,files=await seed_package(db_session,test_user.id,tmp_path/"package",{"a.ts":content})
    async def stream():yield content
    await PackageService.upload_file_chunk_stream(db_session,s.id,files[0].id,stream())
    target=tmp_path/"package/a.ts"
    if damage=="missing":target.unlink()
    else:target.write_bytes(b"x"*len(content))
    await PackageService.upload_file_chunk_stream(db_session,s.id,files[0].id,stream())
    assert target.read_bytes()==content
    assert s.completed_bytes==len(content) and s.status=="UPLOADED"
    # A duplicate retry also repairs a stale historical completion transition.
    s.completed_bytes=0;s.status="UPLOADING";await db_session.commit()
    reply=await PackageService.upload_file_chunk_stream(db_session,s.id,files[0].id,stream())
    assert reply["idempotent"] and s.completed_bytes==len(content) and s.status=="UPLOADED"
    await db_session.execute(delete(Video).where(Video.id==v.id));await db_session.commit()


def test_f07_manual_resume_preserves_selected_batches(tmp_path,monkeypatch):
    from studio.core.batch_runner import BatchRunner
    from studio.core.batch_store import BatchStore
    monkeypatch.setattr(BatchRunner,"_report_ready_local_episodes",lambda self:None)
    runner=BatchRunner(BatchStore(tmp_path/"jobs.db"))
    runner._manual_upload_batches=["first","second"];runner._pause_requested=True
    seen=[];runner.start_batch_upload=lambda **kw:seen.append(kw) or {"status":"STARTED"}
    runner.resume_batch()
    assert seen==[{"batch_ids":["first","second"]}] and not runner._pause_requested
    runner.shutdown()


@pytest.mark.parametrize("mode",["local","server"])
def test_f10_runner_dispatches_mode_and_enforces_caps(tmp_path,monkeypatch,mode):
    from studio.core import batch_runner as m
    from studio.core.batch_store import BatchStore
    monkeypatch.setattr(m.BatchRunner,"_report_ready_local_episodes",lambda self:None)
    monkeypatch.setattr(m,"load_studio_config",lambda:dict(processing_mode=mode,cpu_concurrent_jobs=2,gpu_concurrent_jobs=1))
    store=BatchStore(tmp_path/"jobs.db");batch=store.create_batch(str(tmp_path/"series"))
    store.add_episodes(batch["id"],[dict(source_path=str(tmp_path/f"{i}.mkv"),filename=f"{i}.mkv",order_index=i,state="PENDING",settings_json={"encoder_choice":"cpu"}) for i in range(1,5)])
    runner=m.BatchRunner(store);calls=[]
    def work(ep):
        calls.append(ep["id"]);time.sleep(.06)
        store.update_episode(ep["id"],state="READY" if mode=="server" else "READY_TO_UPLOAD")
        return True
    setattr(runner,"_process_server_episode" if mode=="server" else "_process_episode_impl",work)
    setattr(runner,"_process_episode_impl" if mode=="server" else "_process_server_episode",lambda ep:pytest.fail("wrong execution mode"))
    runner.start_batch(batch["id"]);runner._worker_thread.join(5)
    assert not runner.is_running and len(calls)==4 and runner._max_observed_encodes==2
    assert runner._active_encodes_count==0
    runner.shutdown()


def test_f10_stop_after_current_prohibits_next_claim(tmp_path,monkeypatch):
    from studio.core import batch_runner as m
    from studio.core.batch_store import BatchStore
    monkeypatch.setattr(m.BatchRunner,"_report_ready_local_episodes",lambda self:None)
    monkeypatch.setattr(m,"load_studio_config",lambda:dict(processing_mode="server",cpu_concurrent_jobs=1,gpu_concurrent_jobs=1))
    store=BatchStore(tmp_path/"jobs.db");batch=store.create_batch(str(tmp_path/"series"))
    store.add_episodes(batch["id"],[dict(source_path=str(tmp_path/f"{i}.mkv"),filename=f"{i}.mkv",order_index=i,state="PENDING") for i in range(1,4)])
    runner=m.BatchRunner(store);seen=[]
    def work(ep):
        seen.append(ep["id"]);runner.stop_after_current();store.update_episode(ep["id"],state="READY");return True
    runner._process_server_episode=work
    runner.start_batch(batch["id"]);runner._worker_thread.join(5)
    assert len(seen)==1 and store.get_batch(batch["id"])["status"]=="PAUSED"
    runner.shutdown()


async def test_f10_source_api_resumable_upload_finalize_and_queue_contract(db_session,test_user,monkeypatch):
    from app.main import create_application
    from app.api.v1.studio_sources import source_owner
    from app.services import upload_service as m
    from app.api.v1 import admin_transcodes
    app=create_application();app.dependency_overrides[source_owner]=lambda:test_user
    data=b"Synthetic source fixture; no real media"
    monkeypatch.setattr(m.UploadService,"check_disk_reservation",AsyncMock())
    monkeypatch.setattr(m,"validate_video_file",lambda path:dict(duration_seconds=1,resolution_width=640,resolution_height=360,bitrate=1000,codec="h264",size_bytes=path.stat().st_size))
    enqueue=MagicMock();monkeypatch.setattr(admin_transcodes,"Queue",lambda *a,**kw:SimpleNamespace(enqueue=enqueue))
    base="/api/v1/studio/source-uploads"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
        response=await c.post(base+"/initiate",json=dict(title="Synthetic",filename="fixture.mp4",file_size_bytes=len(data),chunk_size_bytes=20,expected_sha256=hashlib.sha256(data).hexdigest()))
        assert response.status_code==201,response.text
        session=response.json();sid=session["upload_id"];vid=session["video_id"]
        assert session["chunk_size_bytes"]==20
        for i in range(session["total_chunks"]):
            block=data[i*20:(i+1)*20]
            res=await c.post(f"{base}/{sid}/chunk",data={"chunk_index":i},files={"chunk_file":("part",block)},headers={"X-Chunk-SHA256":hashlib.sha256(block).hexdigest()})
            assert res.status_code==200,res.text
            status=(await c.get(f"{base}/{sid}/status")).json()
            assert i in status["received_chunks"]
        res=await c.post(f"{base}/{sid}/finalize");assert res.status_code==200,res.text
        assert (await c.post(f"{base}/{sid}/finalize")).status_code==200
        res=await c.post(f"{base}/videos/{vid}/transcode");assert res.status_code==202,res.text
        enqueue.assert_called_once()
        assert (await c.get(f"{base}/videos/{vid}/status")).json()["status"]=="QUEUED"
    await db_session.execute(delete(Video).where(Video.id==uuid.UUID(vid)));await db_session.commit()
