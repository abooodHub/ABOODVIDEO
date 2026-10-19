"""Studio Machine API Endpoints for Pre-Encoded Packages Lifecycle and Resumable Staging."""
import uuid
from pathlib import Path
from typing import Optional, List
import re
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_cache_redis
from app.core.logging import logger
from app.models.api_key import ApiKey
from app.models.package import (
    PackageUploadSession,
    PackageSessionStatus,
    PackageFile,
    PackageFileStatus,
)
from app.schemas.package import (
    PackageInitRequest,
    PackageInitResponse,
    PackageSessionResponse,
    PackageFileListResponse,
    PackageDeclaredFileItem,
    PackageFinalizeResponse,
)
from app.security.api_key import (
    require_api_key_scope,
    require_all_api_key_scopes,
    check_api_key_rate_limit,
)
from app.services.package_service import PackageService

router = APIRouter(
    prefix="/studio/packages",
    tags=["Studio Packages Pipeline"],
)


@router.get("/{session_id}/archive/status")
async def package_archive_status(
    session_id: uuid.UUID,
    api_key: ApiKey = Depends(require_api_key_scope("packages:create", "packages:write", "packages:publish")),
    db: AsyncSession = Depends(get_db),
):
    return await PackageService.archive_status(db, session_id, api_key)


@router.post("/{session_id}/archive/init")
async def package_archive_init(
    session_id: uuid.UUID,
    payload: dict,
    api_key: ApiKey = Depends(require_api_key_scope("packages:write")),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await PackageService.archive_init(db, session_id, api_key, int(payload.get("size", 0)), str(payload.get("sha256", "")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Archive size or SHA-256 is invalid.") from exc


@router.put("/{session_id}/archive/chunk")
async def package_archive_chunk(
    session_id: uuid.UUID,
    request: Request,
    offset: int = Query(..., ge=0),
    api_key: ApiKey = Depends(require_api_key_scope("packages:write")),
    db: AsyncSession = Depends(get_db),
):
    content_length = int(request.headers.get("content-length", "0"))
    if content_length < 1 or content_length > 32 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Archive chunks must be between 1 byte and 32 MiB.")
    return await PackageService.append_package_archive(db, session_id, api_key, offset, await request.body())


@router.post("/{session_id}/archive/finalize")
async def package_archive_finalize(
    session_id: uuid.UUID,
    api_key: ApiKey = Depends(require_api_key_scope("packages:write")),
    db: AsyncSession = Depends(get_db),
):
    return await PackageService.finalize_package_archive(db, session_id, api_key)


@router.get("/verify", status_code=status.HTTP_200_OK)
async def verify_studio_package_key(
    api_key: ApiKey = Depends(
        require_all_api_key_scopes("packages:create", "packages:write", "packages:publish")
    ),
):
    """
    Studio API Key Probe & Capability Verification (/api/v1/studio/packages/verify):
    Verifies that the provided Bearer key is active, unrevoked, and possesses ALL required
    scopes for the Studio pipeline: packages:create, packages:write, packages:publish.
    Returns HTTP 200 with effective scopes, or HTTP 401/403.
    """
    return {
        "ok": True,
        "status": "ok",
        "prefix": api_key.prefix,
        "scopes": api_key.scopes,
        "message": "Studio API key verified successfully.",
    }


@router.post("/init", response_model=PackageInitResponse, status_code=status.HTTP_201_CREATED)
async def init_package(
    req: PackageInitRequest,
    request: Request,
    api_key: ApiKey = Depends(require_api_key_scope("packages:create")),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_cache_redis),
):
    """
    Initializes a new Studio package upload session with server-validated manifest.
    Allocates target generation ID, staging directory, and server-side file_ids.
    Rate limited by internal api_key.id.
    """
    await check_api_key_rate_limit(
        api_key_id=api_key.id,
        action="init",
        limit=settings.RATE_LIMIT_STUDIO_INIT_ATTEMPTS,
        window_seconds=settings.RATE_LIMIT_STUDIO_INIT_WINDOW_SECONDS,
        redis=redis,
    )
    return await PackageService.init_package_session(db=db, req=req, api_key=api_key)


@router.put("/{session_id}/files/{file_id}")
async def upload_package_file(
    session_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    api_key: ApiKey = Depends(require_api_key_scope("packages:write")),
    db: AsyncSession = Depends(get_db),
):
    """
    Crash-safe streaming upload of a declared package file using opaque server file_id.
    Streams to <file_id>.part, validates SHA-256 and byte size on the fly, fsyncs,
    and atomically renames to canonical destination. Idempotent on same-hash re-upload.
    """
    return await PackageService.upload_file_chunk_stream(
        db=db,
        session_id=session_id,
        file_id=file_id,
        stream=request.stream(),
        api_key=api_key,
    )


@router.post("/{session_id}/finalize", response_model=PackageFinalizeResponse, status_code=status.HTTP_202_ACCEPTED)
async def finalize_package(
    session_id: uuid.UUID,
    request: Request,
    api_key: ApiKey = Depends(require_api_key_scope("packages:publish")),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_cache_redis),
):
    """
    Asynchronously finalizes a completed package upload session.
    Verifies all files are present, atomically transitions state to QUEUED_FOR_VERIFICATION,
    enqueues an RQ background job, and returns HTTP 202 Accepted immediately.
    Rate limited by internal api_key.id.
    """
    await check_api_key_rate_limit(
        api_key_id=api_key.id,
        action="finalize",
        limit=settings.RATE_LIMIT_STUDIO_FINALIZE_ATTEMPTS,
        window_seconds=settings.RATE_LIMIT_STUDIO_FINALIZE_WINDOW_SECONDS,
        redis=redis,
    )
    return await PackageService.finalize_package_session(
        db=db,
        session_id=session_id,
        api_key=api_key,
    )


@router.get("/{session_id}", response_model=PackageSessionResponse)
async def get_package_session(
    session_id: uuid.UUID,
    api_key: ApiKey = Depends(
        require_api_key_scope("packages:create", "packages:write", "packages:publish", "videos:read")
    ),
    db: AsyncSession = Depends(get_db),
):
    """Polls the current status and verification progress of a package upload session."""
    stmt = select(PackageUploadSession).where(PackageUploadSession.id == session_id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package session not found.")

    if session.created_by_key_id != api_key.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: Not authorized for this package session.")

    progress_pct = (
        round((session.completed_bytes / session.total_bytes) * 100, 2)
        if session.total_bytes > 0
        else 0.0
    )

    return PackageSessionResponse(
        id=session.id,
        video_id=session.video_id,
        status=session.status,
        total_files=session.total_files,
        total_bytes=session.total_bytes,
        completed_bytes=session.completed_bytes,
        progress_pct=min(100.0, progress_pct),
        verification_job_id=session.verification_job_id,
        error_message=session.error_message,
        created_at=session.created_at,
        updated_at=session.updated_at,
        expires_at=session.expires_at,
    )


@router.get("/{session_id}/files", response_model=PackageFileListResponse)
async def list_package_files(
    session_id: uuid.UUID,
    status_filter: Optional[str] = Query(None, alias="status"),
    api_key: ApiKey = Depends(
        require_api_key_scope("packages:create", "packages:write", "packages:publish", "videos:read")
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Lists declared files for a package session.
    Enables Studio to reconnect after network interruption and query missing files (?status=PENDING).
    """
    s_stmt = select(PackageUploadSession).where(PackageUploadSession.id == session_id)
    session = (await db.execute(s_stmt)).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package session not found.")

    if session.created_by_key_id != api_key.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: Not authorized for this package session.")

    query = select(PackageFile).where(PackageFile.package_session_id == session_id)
    if status_filter:
        query = query.where(PackageFile.status == status_filter.upper())

    files = (await db.execute(query)).scalars().all()

    # Counts
    pending_stmt = select(PackageFile).where(
        PackageFile.package_session_id == session_id,
        PackageFile.status == PackageFileStatus.PENDING.value,
    )
    pending_files = (await db.execute(pending_stmt)).scalars().all()

    items = [
        PackageDeclaredFileItem(
            file_id=f.id,
            relative_path=f.relative_path,
            expected_size=f.expected_size,
            expected_sha256=f.expected_sha256,
            media_type=f.media_type,
            status=f.status,
            received_bytes=f.received_bytes,
        )
        for f in files
    ]

    return PackageFileListResponse(
        session_id=session_id,
        status=session.status,
        files=items,
        pending_count=len(pending_files),
        completed_count=session.total_files - len(pending_files),
    )


@router.delete("/{session_id}")
async def cancel_package_session(
    session_id: uuid.UUID,
    api_key: ApiKey = Depends(require_api_key_scope("packages:create", "packages:publish")),
    db: AsyncSession = Depends(get_db),
):
    """Cancels an unverified package upload session and cleans up staged files."""
    stmt = select(PackageUploadSession).where(PackageUploadSession.id == session_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package session not found.")

    if session.created_by_key_id != api_key.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: Not authorized for this package session.")

    if session.status in (PackageSessionStatus.PROMOTING.value, PackageSessionStatus.READY.value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot cancel a package that is currently promoting or ready.",
        )

    session.status = PackageSessionStatus.CANCELLED.value
    await db.commit()

    # Clean staging directory
    staging_path = Path(session.staging_path)
    if staging_path.exists():
        import shutil
        shutil.rmtree(staging_path, ignore_errors=True)

    return {"status": "cancelled", "session_id": str(session_id)}
