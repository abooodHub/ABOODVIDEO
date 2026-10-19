"""Scoped source intake for Studio's server-processing mode."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header, Response
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.user import User, UserRole
from app.models.video import Video, TranscodeJob
from app.security.api_key import require_all_api_key_scopes
from app.security.ownership import require_video_owner
from app.api.v1 import uploads
from app.api.v1.admin_transcodes import trigger_video_transcode
from app.schemas.upload import InitiateUploadRequest

router = APIRouter(prefix="/studio/source-uploads", tags=["Studio Server Processing"])


async def source_owner(key=Depends(require_all_api_key_scopes("uploads:create", "uploads:write", "videos:create")), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, key.user_id)
    if not user or not user.is_active or user.role not in (UserRole.OWNER, UserRole.ADMIN):
        raise HTTPException(403, "Server processing requires an active owner or admin")
    return user


@router.post("/initiate", status_code=201)
async def initiate(payload: InitiateUploadRequest, response: Response, user=Depends(source_owner), db: AsyncSession = Depends(get_db)):
    return await uploads.initiate_upload(payload, response, user, db)


@router.post("/{upload_id}/chunk")
async def chunk(upload_id: uuid.UUID, response: Response, chunk_index: int = Form(...), chunk_file: UploadFile = File(...), x_chunk_sha256: str = Header(None, alias="X-Chunk-SHA256"), user=Depends(source_owner), db: AsyncSession = Depends(get_db)):
    return await uploads.upload_chunk(upload_id, response, chunk_index, chunk_file, x_chunk_sha256, user, db)


@router.get("/{upload_id}/status")
async def upload_status(upload_id: uuid.UUID, response: Response, user=Depends(source_owner), db: AsyncSession = Depends(get_db)):
    return await uploads.get_upload_status(upload_id, response, user, db)


@router.post("/{upload_id}/finalize")
async def finalize(upload_id: uuid.UUID, response: Response, user=Depends(source_owner), db: AsyncSession = Depends(get_db)):
    return await uploads.finalize_upload(upload_id, response, user, db)


@router.post("/videos/{video_id}/transcode", status_code=202)
async def transcode(video_id: uuid.UUID, user=Depends(source_owner), db: AsyncSession = Depends(get_db)):
    require_video_owner(await db.get(Video, video_id), user)
    return await trigger_video_transcode(video_id, priority=0, current_user=user, db=db)


@router.get("/videos/{video_id}/status")
async def video_status(video_id: uuid.UUID, user=Depends(source_owner), db: AsyncSession = Depends(get_db)):
    video = require_video_owner(await db.get(Video, video_id), user)
    job = (await db.execute(select(TranscodeJob).where(TranscodeJob.video_id == video_id).order_by(desc(TranscodeJob.created_at)).limit(1))).scalar_one_or_none()
    return {"video_id": str(video_id), "status": video.status.value, "progress": job.progress_percent if job else 0, "error": job.error_message if job else None}
