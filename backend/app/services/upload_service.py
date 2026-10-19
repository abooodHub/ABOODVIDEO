"""Production Resumable Upload Service."""
from app.utils.file_io import file_io, is_storage_capacity_error
import os
import shutil
import hashlib
import uuid
import math
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.logging import logger
from app.core.exceptions import AboodVideoException, StorageException
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, VideoVisibility
from app.models.upload import UploadSession, UploadSessionStatus
from app.models.folder import Folder
from app.models.series import Series, Season
from app.services.video_validator import validate_video_file, InvalidVideoException
from app.storage.local import LocalStorageService
from app.utils.disk import probe_storage_health


class UploadException(AboodVideoException):
    def __init__(self, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None):
        super().__init__(message=message, status_code=status_code, details=details)


def calculate_upload_progress(
    bytes_received: int,
    file_size_bytes: int,
    received_chunks_count: int,
    total_chunks: int,
) -> float:
    """
    Upload progress calculation rule:
    100% strictly requires received_bytes == file_size_bytes AND received_chunks_count == total_chunks.
    If any chunk is missing or bytes < file_size, it will NEVER return 100.0 (capped at 99.99).
    """
    if file_size_bytes <= 0 or total_chunks <= 0:
        return 0.0

    if bytes_received >= file_size_bytes and received_chunks_count >= total_chunks:
        return 100.0

    raw_pct = (bytes_received / file_size_bytes) * 100.0
    pct = round(raw_pct, 2)
    if pct >= 100.0:
        pct = 99.99
    return pct


class UploadService:
    calculate_progress = staticmethod(calculate_upload_progress)

    @staticmethod
    async def check_disk_reservation(db: AsyncSession, incoming_bytes: int) -> None:
        """
        Disk Reservation check: verifies that incoming upload bytes plus all currently
        reserved bytes across active uploads will not violate hard storage thresholds.
        """
        # 1. Total storage stats
        storage_info = probe_storage_health()
        free_bytes = storage_info["free_bytes"]
        total_bytes = storage_info["total_bytes"]
        used_bytes = storage_info["used_bytes"]

        if storage_info["status"] == "unhealthy":
            raise StorageException(
                "Storage is unhealthy or disk threshold exceeded. Uploads are currently paused."
            )

        # 2. Query currently reserved bytes from active uncompleted sessions
        active_statuses = [
            UploadSessionStatus.INITIATED,
            UploadSessionStatus.UPLOADING,
            UploadSessionStatus.PAUSED,
        ]
        stmt = (
            select(
                func.coalesce(
                    func.sum(UploadSession.file_size_bytes - UploadSession.bytes_received),
                    0,
                )
            ).where(
                and_(
                    UploadSession.status.in_(active_statuses),
                    UploadSession.expires_at > datetime.now(timezone.utc),
                )
            )
        )
        result = await db.execute(stmt)
        active_reserved_bytes = result.scalar() or 0

        total_projected_used = used_bytes + active_reserved_bytes + incoming_bytes
        projected_free = total_bytes - total_projected_used
        projected_used_pct = (total_projected_used / total_bytes) * 100

        if (
            projected_free < settings.STORAGE_MIN_FREE_BYTES
            or projected_used_pct >= settings.STORAGE_HARD_THRESHOLD_PERCENT
        ):
            raise StorageException(
                "Insufficient storage: Upload exceeds reserved disk thresholds.",
                details={
                    "free_bytes": free_bytes,
                    "active_reserved_bytes": active_reserved_bytes,
                    "incoming_bytes": incoming_bytes,
                    "projected_free_bytes": projected_free,
                    "min_free_floor": settings.STORAGE_MIN_FREE_BYTES,
                },
            )

    @classmethod
    async def initiate_upload(
        cls,
        db: AsyncSession,
        user: User,
        title: str,
        filename: str,
        file_size_bytes: int,
        chunk_size_bytes: int = 5242880,
        description: Optional[str] = None,
        category: Optional[str] = None,
        series_name: Optional[str] = None,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
        folder_id: Optional[uuid.UUID] = None,
        visibility=None,
        expected_sha256: Optional[str] = None,
        mime_type: Optional[str] = None,
        series_id: Optional[uuid.UUID] = None,
        season_id: Optional[uuid.UUID] = None,
    ) -> UploadSession:
        """Initiate a resumable upload session with disk reservation."""
        if file_size_bytes > settings.MAX_UPLOAD_SIZE_BYTES:
            raise UploadException(
                f"File size ({file_size_bytes} bytes) exceeds max allowed upload size ({settings.MAX_UPLOAD_SIZE_BYTES} bytes)"
            )

        limit = settings.MAX_ACTIVE_UPLOADS_PER_USER
        if limit:
            # Lock per owner in the same transaction as reservation creation.
            # Paused sessions still reserve disk and retain their slot.
            await db.execute(select(User.id).where(User.id == user.id).with_for_update())
            active = await db.scalar(select(func.count()).select_from(UploadSession).where(
                UploadSession.user_id == user.id,
                UploadSession.status.in_([
                    UploadSessionStatus.INITIATED, UploadSessionStatus.UPLOADING, UploadSessionStatus.PAUSED,
                ]),
                UploadSession.expires_at > datetime.now(timezone.utc),
            ))
            if active >= limit:
                raise UploadException("Active upload session limit reached", status_code=429)

        # Validate folder if specified
        if folder_id:
            folder_stmt = select(Folder).where(Folder.id == folder_id)
            folder_res = await db.execute(folder_stmt)
            folder = folder_res.scalar_one_or_none()
            if not folder or folder.trashed_at is not None or (user.role != UserRole.ADMIN and folder.user_id != user.id):
                raise UploadException(f"Target folder not found: {folder_id}", status_code=404)

        target_season = await db.get(Season, season_id) if season_id else None
        if season_id and (not target_season or target_season.trashed_at is not None):
            raise UploadException("Target season not found", status_code=404)
        if target_season:
            if series_id and series_id != target_season.series_id:
                raise UploadException("Target season does not belong to the selected series")
            series_id = target_season.series_id
        target_series = await db.get(Series, series_id) if series_id else None
        if series_id and (not target_series or target_series.trashed_at is not None or
                          (user.role != UserRole.ADMIN and target_series.owner_id != user.id)):
            raise UploadException("Target series not found", status_code=404)
        if target_series:
            if folder_id and target_series.parent_folder_id and folder_id != target_series.parent_folder_id:
                raise UploadException("Target series does not belong to the selected folder")
            series_name = target_series.title
        if target_season:
            season_number = target_season.season_number

        # Check and enforce disk reservation
        await cls.check_disk_reservation(db, file_size_bytes)

        total_chunks = math.ceil(file_size_bytes / chunk_size_bytes)
        upload_id = uuid.uuid4()
        video_id = uuid.uuid4()

        # Temporary directory for chunk parts
        temp_dir = Path(settings.STORAGE_TEMP_PATH) / "uploads" / str(upload_id)
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Create Video record
        video = Video(
            id=video_id,
            user_id=user.id,
            folder_id=folder_id,
            series_id=series_id,
            season_id=season_id,
            title=title,
            description=description,
            category=category.strip() or None if category else None,
            series_name=series_name.strip() or None if series_name else None,
            season_number=season_number,
            episode_number=episode_number,
            visibility=visibility or VideoVisibility.PRIVATE,
            share_id=secrets.token_urlsafe(16) if visibility in (VideoVisibility.UNLISTED, VideoVisibility.PUBLIC, VideoVisibility.PASSWORD_PROTECTED) else None,
            status=VideoStatus.UPLOADING,
            source_filename=filename,
            source_size_bytes=file_size_bytes,
            upload_progress_percent=0.0,
        )
        db.add(video)

        # Create UploadSession record (expires in 24 hours)
        session = UploadSession(
            id=upload_id,
            video_id=video_id,
            user_id=user.id,
            status=UploadSessionStatus.INITIATED,
            filename=filename,
            file_size_bytes=file_size_bytes,
            chunk_size_bytes=chunk_size_bytes,
            total_chunks=total_chunks,
            received_chunks=[],
            bytes_received=0,
            expected_sha256=expected_sha256,
            mime_type=mime_type,
            temp_directory=str(temp_dir),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        return session

    @classmethod
    async def get_session(
        cls,
        db: AsyncSession,
        upload_id: uuid.UUID,
        user: User,
    ) -> UploadSession:
        """Retrieve upload session with user authorization checks."""
        stmt = select(UploadSession).where(UploadSession.id == upload_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise UploadException("Upload session not found", status_code=404)

        # Only ADMIN can act on a different owner's source upload.
        if session.user_id != user.id and user.role != UserRole.ADMIN:
            raise UploadException("Unauthorized access to upload session", status_code=403)

        # Check expiration
        if session.expires_at < datetime.now(timezone.utc) and session.status != UploadSessionStatus.COMPLETED:
            if session.status != UploadSessionStatus.EXPIRED:
                session.status = UploadSessionStatus.EXPIRED
                await db.commit()
            raise UploadException("Upload session has expired", status_code=410)

        return session

    @classmethod
    async def process_chunk(
        cls,
        db: AsyncSession,
        session: UploadSession,
        chunk_index: int,
        chunk_data: bytes,
        chunk_sha256: Optional[str] = None,
    ) -> UploadSession:
        """
        Uploads and validates a chunk:
        - Validates session state
        - Validates index and size
        - Verifies SHA256 checksum if provided
        - Handles duplicate chunks idempotently
        - Writes chunk to disk
        - Updates PostgreSQL progress
        """
        # Lock before inspecting state or touching files. populate_existing is
        # essential because get_session already loaded this row in this request.
        session = (await db.execute(select(UploadSession).where(UploadSession.id == session.id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if session.status == UploadSessionStatus.CANCELLED:
            raise UploadException("Upload session was cancelled", status_code=410)
        if session.status == UploadSessionStatus.EXPIRED:
            raise UploadException("Upload session has expired", status_code=410)
        if session.status == UploadSessionStatus.COMPLETED:
            raise UploadException("Upload session is already completed", status_code=409)
        if session.status == UploadSessionStatus.PAUSED:
            raise UploadException("Upload session is paused. Call resume before uploading chunks.", status_code=409)

        if chunk_index < 0 or chunk_index >= session.total_chunks:
            raise UploadException(f"Invalid chunk_index {chunk_index}. Total chunks: {session.total_chunks}")

        # Chunk size validation
        expected_size = session.chunk_size_bytes
        if chunk_index == session.total_chunks - 1:
            # Last chunk can be smaller
            expected_size = session.file_size_bytes - ((session.total_chunks - 1) * session.chunk_size_bytes)

        if len(chunk_data) != expected_size:
            raise UploadException(
                f"Chunk {chunk_index} size mismatch: received {len(chunk_data)} bytes, expected {expected_size} bytes."
            )

        # Checksum validation if provided
        if chunk_sha256:
            computed_sha = await file_io(lambda: hashlib.sha256(chunk_data).hexdigest())
            if computed_sha.lower() != chunk_sha256.lower():
                raise UploadException(
                    f"Chunk {chunk_index} checksum mismatch: computed {computed_sha}, expected {chunk_sha256}"
                )

        chunk_dir = Path(session.temp_directory)
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_file = chunk_dir / f"chunk_{chunk_index}.part"
        if chunk_index in session.received_chunks and chunk_file.is_file():
            def same_chunk():
                return hashlib.sha256(chunk_file.read_bytes()).digest() == hashlib.sha256(chunk_data).digest()
            if not await file_io(same_chunk):
                raise UploadException("A different chunk was already accepted at this index", status_code=409)
            return session

        # Write chunk atomically to disk with unique temp name to prevent write races
        tmp_write_file = chunk_dir / f".tmp_chunk_{chunk_index}_{uuid.uuid4().hex[:8]}.tmp"
        def write_chunk():
            try:
                with open(tmp_write_file, "wb") as f:
                    f.write(chunk_data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_write_file, chunk_file)
            finally:
                tmp_write_file.unlink(missing_ok=True)
        try:
            await file_io(write_chunk)
        except OSError as exc:
            if is_storage_capacity_error(exc):
                raise StorageException("Insufficient storage while saving upload chunk. Free space and retry; acknowledged chunks are preserved.") from exc
            raise

        locked_session = session

        if locked_session.status in (UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED):
            raise UploadException(f"Upload session was {locked_session.status.value}", status_code=410)
        if locked_session.status == UploadSessionStatus.COMPLETED:
            raise UploadException("Upload session is already completed", status_code=409)
        if locked_session.status == UploadSessionStatus.PAUSED:
            raise UploadException("Upload session is paused. Call resume before uploading chunks.", status_code=409)

        # Duplicate chunk idempotency: if already received, don't double count
        is_duplicate = chunk_index in locked_session.received_chunks
        if not is_duplicate:
            updated_received = list(locked_session.received_chunks)
            updated_received.append(chunk_index)
            locked_session.received_chunks = sorted(updated_received)
            locked_session.bytes_received += len(chunk_data)

        # Progress calculation
        pct = calculate_upload_progress(
            bytes_received=locked_session.bytes_received,
            file_size_bytes=locked_session.file_size_bytes,
            received_chunks_count=len(locked_session.received_chunks),
            total_chunks=locked_session.total_chunks,
        )
        locked_session.status = UploadSessionStatus.UPLOADING

        # Synchronize Video record
        stmt = select(Video).where(Video.id == locked_session.video_id)
        result = await db.execute(stmt)
        video = result.scalar_one_or_none()
        if video:
            video.upload_progress_percent = pct

        await db.commit()
        await db.refresh(locked_session)
        return locked_session

    @classmethod
    async def pause_upload(cls, db: AsyncSession, session: UploadSession) -> UploadSession:
        session = (await db.execute(select(UploadSession).where(UploadSession.id == session.id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if session.status in (UploadSessionStatus.COMPLETED, UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED):
            raise UploadException(f"Cannot pause session in {session.status.value} state")
        session.status = UploadSessionStatus.PAUSED
        await db.commit()
        await db.refresh(session)
        return session

    @classmethod
    async def resume_upload(cls, db: AsyncSession, session: UploadSession) -> UploadSession:
        session = (await db.execute(select(UploadSession).where(UploadSession.id == session.id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if session.status in (UploadSessionStatus.COMPLETED, UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED):
            raise UploadException(f"Cannot resume session in {session.status.value} state")
        session.status = UploadSessionStatus.UPLOADING
        await db.commit()
        await db.refresh(session)
        return session

    @classmethod
    async def cancel_upload(cls, db: AsyncSession, session: UploadSession) -> UploadSession:
        session = (await db.execute(select(UploadSession).where(UploadSession.id == session.id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if session.status == UploadSessionStatus.COMPLETED:
            raise UploadException("Cannot cancel an already completed upload")

        session.status = UploadSessionStatus.CANCELLED

        # StorageService constrains cleanup to the configured storage root.
        try:
            await LocalStorageService().delete(session.temp_directory)
        except StorageException:
            logger.warning("Refused unsafe upload storage cleanup for session %s", session.id)

        # Mark Video cancelled
        stmt = select(Video).where(Video.id == session.video_id)
        result = await db.execute(stmt)
        video = result.scalar_one_or_none()
        if video:
            video.status = VideoStatus.CANCELLED
            video.upload_progress_percent = 0.0

        await db.commit()
        await db.refresh(session)
        return session

    @classmethod
    async def finalize_upload(
        cls,
        db: AsyncSession,
        session: UploadSession,
    ) -> Tuple[Video, Dict[str, Any]]:
        """
        Atomic Finalization & ffprobe Validation:
        - Check idempotency (double finalization)
        - Check all chunks present
        - Assemble chunks atomically
        - Verify overall SHA256 if expected
        - Clean up temporary parts
        - Run ffprobe inspection
        - Mark Video as UPLOADED
        """
        session = (await db.execute(select(UploadSession).where(UploadSession.id == session.id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
        if session.status in (UploadSessionStatus.CANCELLED, UploadSessionStatus.EXPIRED, UploadSessionStatus.PAUSED):
            raise UploadException(f"Upload session is {session.status.value}", status_code=409)
        # Double finalization idempotency
        stmt = select(Video).where(Video.id == session.video_id)
        result = await db.execute(stmt)
        video = result.scalar_one()

        if session.status == UploadSessionStatus.COMPLETED:
            return video, {
                "duration_seconds": video.source_duration_seconds,
                "resolution_width": video.source_resolution_width,
                "resolution_height": video.source_resolution_height,
                "codec": video.source_codec,
            }

        # Verify all chunks are accounted for
        received_set = set(session.received_chunks)
        missing_chunks = [i for i in range(session.total_chunks) if i not in received_set]
        if missing_chunks:
            raise UploadException(
                f"Cannot finalize: missing {len(missing_chunks)} chunks: {missing_chunks[:10]}",
                status_code=400,
                details={"missing_chunks": missing_chunks},
            )

        chunk_dir = Path(session.temp_directory)
        assembled_tmp = chunk_dir.parent / f"{session.id}_assembled.tmp"

        # Assemble chunks sequentially into temp file
        hasher = hashlib.sha256()
        try:
            def assemble_and_probe():
                with open(assembled_tmp, "wb") as outfile:
                    for idx in range(session.total_chunks):
                        part_file = chunk_dir / f"chunk_{idx}.part"
                        if not part_file.exists():
                            raise UploadException(f"Chunk file missing from disk: {part_file.name}", status_code=500)
                        with open(part_file, "rb") as infile:
                            while chunk := infile.read(1024 * 1024):
                                hasher.update(chunk)
                                outfile.write(chunk)

                final_sha256 = hasher.hexdigest()

                # Verify client expected SHA256 if specified
                if session.expected_sha256 and session.expected_sha256.lower() != final_sha256.lower():
                    assembled_tmp.unlink(missing_ok=True)
                    raise UploadException(
                        f"Final file SHA256 mismatch: calculated {final_sha256}, expected {session.expected_sha256}",
                        status_code=400,
                    )

                # Move atomically to final storage path
                ext = Path(session.filename).suffix or ".mp4"
                target_dir = Path(settings.STORAGE_UPLOADS_PATH) / str(session.video_id)
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file = target_dir / f"source{ext}"

                os.replace(assembled_tmp, target_file)

                # Clean up chunks
                shutil.rmtree(chunk_dir, ignore_errors=True)

                # Execute ffprobe validation ONLY (Rule 8)
                probe_result = validate_video_file(target_file)

                return final_sha256, target_file, probe_result
            final_sha256, target_file, probe_result = await file_io(assemble_and_probe)

            # Update Video with metadata
            video.status = VideoStatus.UPLOADED
            video.source_duration_seconds = probe_result["duration_seconds"]
            video.source_resolution_width = probe_result["resolution_width"]
            video.source_resolution_height = probe_result["resolution_height"]
            video.source_bitrate = probe_result["bitrate"]
            video.source_codec = probe_result["codec"]
            video.source_path = str(target_file)
            video.source_size_bytes = probe_result["size_bytes"]
            video.upload_progress_percent = 100.0

            # Update UploadSession
            session.status = UploadSessionStatus.COMPLETED
            session.final_sha256 = final_sha256
            session.target_path = str(target_file)

            await db.commit()
            await db.refresh(video)
            await db.refresh(session)

            return video, probe_result

        except InvalidVideoException:
            # ffprobe failed: update video status to FAILED
            video.status = VideoStatus.FAILED
            session.status = UploadSessionStatus.CANCELLED
            await db.commit()
            raise

        except UploadException:
            if assembled_tmp.exists():
                assembled_tmp.unlink(missing_ok=True)
            raise

        except Exception as e:
            if assembled_tmp.exists():
                assembled_tmp.unlink(missing_ok=True)
            if is_storage_capacity_error(e):
                raise StorageException("Insufficient storage while assembling upload. Free space and retry finalization; uploaded chunks are preserved.") from e
            logger.error(f"Error during finalization: {e}", exc_info=True)
            raise UploadException("Upload finalization failed due to an internal server error.", status_code=500)

    @classmethod
    async def cleanup_expired_sessions(cls, db: AsyncSession) -> int:
        """Terminally reconcile expired or inactive uploads and release reservations."""
        now = datetime.now(timezone.utc)
        inactivity_cutoff = now - timedelta(seconds=settings.UPLOAD_INACTIVITY_TIMEOUT_SECONDS)
        stmt = (
            select(UploadSession)
            .where(
                and_(
                    (UploadSession.expires_at < now) | (UploadSession.updated_at < inactivity_cutoff),
                    UploadSession.status.in_([
                        UploadSessionStatus.INITIATED,
                        UploadSessionStatus.UPLOADING,
                        UploadSessionStatus.PAUSED,
                    ]),
                )
            )
        )
        result = await db.execute(stmt)
        expired_sessions = result.scalars().all()

        cleaned_count = 0
        for s in expired_sessions:
            s.status = UploadSessionStatus.EXPIRED
            try:
                await LocalStorageService().delete(s.temp_directory)
            except StorageException:
                logger.warning("Refused unsafe stale-upload storage cleanup for session %s", s.id)
            video = (await db.execute(select(Video).where(Video.id == s.video_id))).scalar_one_or_none()
            if video and video.status == VideoStatus.UPLOADING:
                video.status = VideoStatus.CANCELLED
                video.upload_progress_percent = 0.0
            cleaned_count += 1

        if cleaned_count > 0:
            await db.commit()
            logger.info(f"Cleaned up {cleaned_count} expired upload sessions.")

        return cleaned_count
