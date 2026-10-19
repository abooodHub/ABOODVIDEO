"""Resumable Upload System API Endpoints (Phase 2 - Protected for Owner & Admin Only)."""
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Header, UploadFile, File, Form, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.user import User
from app.security.auth import require_owner_or_admin
from app.security.csrf import check_csrf_protection
from app.services.upload_service import UploadService
from app.schemas.upload import (
    InitiateUploadRequest,
    InitiateUploadResponse,
    ChunkUploadResponse,
    UploadStatusResponse,
    FinalizeUploadResponse,
)

router = APIRouter(
    prefix="/uploads",
    tags=["Resumable Uploads"],
    dependencies=[Depends(check_csrf_protection)],
)


@router.post("/initiate", response_model=InitiateUploadResponse, status_code=status.HTTP_201_CREATED)
async def initiate_upload(
    payload: InitiateUploadRequest,
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Initiate a resumable upload session with disk reservation (Owner/Admin only)."""
    session = await UploadService.initiate_upload(
        db=db,
        user=current_user,
        title=payload.title,
        filename=payload.filename,
        file_size_bytes=payload.file_size_bytes,
        chunk_size_bytes=payload.chunk_size_bytes,
        description=payload.description,
        category=payload.category,
        series_name=payload.series_name,
        season_number=payload.season_number,
        episode_number=payload.episode_number,
        folder_id=payload.folder_id,
        series_id=payload.series_id,
        season_id=payload.season_id,
        visibility=payload.visibility,
        expected_sha256=payload.expected_sha256,
        mime_type=payload.mime_type,
    )
    response.headers["Cache-Control"] = "no-store"
    return InitiateUploadResponse(
        upload_id=session.id,
        video_id=session.video_id,
        filename=session.filename,
        file_size_bytes=session.file_size_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        status=session.status.value,
        expires_at=session.expires_at,
    )


@router.post("/{upload_id}/chunk", response_model=ChunkUploadResponse)
async def upload_chunk(
    upload_id: uuid.UUID,
    response: Response,
    chunk_index: int = Form(...),
    chunk_file: UploadFile = File(...),
    x_chunk_sha256: Optional[str] = Header(None, alias="X-Chunk-SHA256"),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Upload a single chunk of the file (Owner/Admin only)."""
    session = await UploadService.get_session(db, upload_id, current_user)
    chunk_bytes = await chunk_file.read(session.chunk_size_bytes + 1)

    updated = await UploadService.process_chunk(
        db=db,
        session=session,
        chunk_index=chunk_index,
        chunk_data=chunk_bytes,
        chunk_sha256=x_chunk_sha256,
    )

    pct = UploadService.calculate_progress(
        bytes_received=updated.bytes_received,
        file_size_bytes=updated.file_size_bytes,
        received_chunks_count=len(updated.received_chunks),
        total_chunks=updated.total_chunks,
    )
    response.headers["Cache-Control"] = "no-store"
    return ChunkUploadResponse(
        upload_id=updated.id,
        chunk_index=chunk_index,
        chunks_received_count=len(updated.received_chunks),
        total_chunks=updated.total_chunks,
        bytes_received=updated.bytes_received,
        progress_percent=pct,
        status=updated.status.value,
    )


@router.get("/{upload_id}/status", response_model=UploadStatusResponse)
async def get_upload_status(
    upload_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve detailed upload status, received chunks, and missing chunks (Owner/Admin only)."""
    session = await UploadService.get_session(db, upload_id, current_user)
    received_set = set(session.received_chunks)
    missing = [i for i in range(session.total_chunks) if i not in received_set]
    pct = UploadService.calculate_progress(
        bytes_received=session.bytes_received,
        file_size_bytes=session.file_size_bytes,
        received_chunks_count=len(session.received_chunks),
        total_chunks=session.total_chunks,
    )

    response.headers["Cache-Control"] = "no-store"
    return UploadStatusResponse(
        upload_id=session.id,
        video_id=session.video_id,
        status=session.status.value,
        filename=session.filename,
        file_size_bytes=session.file_size_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks,
        missing_chunks=missing,
        bytes_received=session.bytes_received,
        progress_percent=pct,
        expires_at=session.expires_at,
    )


@router.post("/{upload_id}/pause", response_model=UploadStatusResponse)
async def pause_upload(
    upload_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Pause an active upload session (Owner/Admin only)."""
    session = await UploadService.get_session(db, upload_id, current_user)
    updated = await UploadService.pause_upload(db, session)
    received_set = set(updated.received_chunks)
    missing = [i for i in range(updated.total_chunks) if i not in received_set]
    pct = UploadService.calculate_progress(
        bytes_received=updated.bytes_received,
        file_size_bytes=updated.file_size_bytes,
        received_chunks_count=len(updated.received_chunks),
        total_chunks=updated.total_chunks,
    )

    response.headers["Cache-Control"] = "no-store"
    return UploadStatusResponse(
        upload_id=updated.id,
        video_id=updated.video_id,
        status=updated.status.value,
        filename=updated.filename,
        file_size_bytes=updated.file_size_bytes,
        chunk_size_bytes=updated.chunk_size_bytes,
        total_chunks=updated.total_chunks,
        received_chunks=updated.received_chunks,
        missing_chunks=missing,
        bytes_received=updated.bytes_received,
        progress_percent=pct,
        expires_at=updated.expires_at,
    )


@router.post("/{upload_id}/resume", response_model=UploadStatusResponse)
async def resume_upload(
    upload_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Resume a paused upload session (Owner/Admin only)."""
    session = await UploadService.get_session(db, upload_id, current_user)
    updated = await UploadService.resume_upload(db, session)
    received_set = set(updated.received_chunks)
    missing = [i for i in range(updated.total_chunks) if i not in received_set]
    pct = UploadService.calculate_progress(
        bytes_received=updated.bytes_received,
        file_size_bytes=updated.file_size_bytes,
        received_chunks_count=len(updated.received_chunks),
        total_chunks=updated.total_chunks,
    )

    response.headers["Cache-Control"] = "no-store"
    return UploadStatusResponse(
        upload_id=updated.id,
        video_id=updated.video_id,
        status=updated.status.value,
        filename=updated.filename,
        file_size_bytes=updated.file_size_bytes,
        chunk_size_bytes=updated.chunk_size_bytes,
        total_chunks=updated.total_chunks,
        received_chunks=updated.received_chunks,
        missing_chunks=missing,
        bytes_received=updated.bytes_received,
        progress_percent=pct,
        expires_at=updated.expires_at,
    )


@router.post("/{upload_id}/cancel", response_model=UploadStatusResponse)
async def cancel_upload(
    upload_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Cancel an upload session and prune partial chunks from disk (Owner/Admin only)."""
    session = await UploadService.get_session(db, upload_id, current_user)
    updated = await UploadService.cancel_upload(db, session)
    response.headers["Cache-Control"] = "no-store"
    return UploadStatusResponse(
        upload_id=updated.id,
        video_id=updated.video_id,
        status=updated.status.value,
        filename=updated.filename,
        file_size_bytes=updated.file_size_bytes,
        chunk_size_bytes=updated.chunk_size_bytes,
        total_chunks=updated.total_chunks,
        received_chunks=updated.received_chunks,
        missing_chunks=[],
        bytes_received=updated.bytes_received,
        progress_percent=0.0,
        expires_at=updated.expires_at,
    )


@router.post("/{upload_id}/finalize", response_model=FinalizeUploadResponse)
async def finalize_upload(
    upload_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Assemble all chunks, validate via ffprobe, and transition video to UPLOADED (Owner/Admin only)."""
    session = await UploadService.get_session(db, upload_id, current_user)
    video, probe = await UploadService.finalize_upload(db, session)

    response.headers["Cache-Control"] = "no-store"
    return FinalizeUploadResponse(
        video_id=video.id,
        upload_id=session.id,
        title=video.title,
        status=video.status.value,
        duration_seconds=video.source_duration_seconds,
        resolution_width=video.source_resolution_width,
        resolution_height=video.source_resolution_height,
        codec=video.source_codec,
        size_bytes=video.source_size_bytes,
    )


@router.post("/cleanup")
async def cleanup_expired(
    response: Response,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Prune expired sessions and orphaned upload chunks (Admin/Owner only)."""
    count = await UploadService.cleanup_expired_sessions(db)
    response.headers["Cache-Control"] = "no-store"
    return {"cleaned_sessions_count": count}
