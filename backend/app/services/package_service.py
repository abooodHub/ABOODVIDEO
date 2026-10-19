"""Package Service managing Studio pre-encoded package lifecycle, validation, and staging."""
import hashlib
import json
import os
import re
import secrets
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
from fastapi import status
from sqlalchemy import select, update, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from redis import Redis
from rq import Queue

from app.core.config import settings
from app.core.exceptions import AboodVideoException, StorageException
from app.core.logging import logger
from app.models.user import User, UserRole
from app.models.api_key import ApiKey
from app.models.base import utc_now
from app.models.video import Video, VideoStatus, VideoVisibility
from app.models.folder import Folder
from app.models.series import Series, Season
from app.models.package import (
    PackageUploadSession,
    PackageSessionStatus,
    PackageFile,
    PackageFileStatus,
)
from app.schemas.package import (
    PackageInitRequest,
    PackageInitResponse,
    PackageDeclaredFileItem,
    PackageManifest,
    PackageFinalizeResponse,
)
from app.utils.file_io import file_io, is_storage_capacity_error
from app.utils.disk import check_package_storage_admission

ALLOWED_EXTENSIONS = {
    ".m3u8",
    ".ts",
    ".vtt",
    ".srt",
    ".ass",
    ".ssa",
    ".ttf",
    ".otf",
    ".woff2",
    ".jpg",
    ".png",
    ".webp",
    ".mp4",
}


class PackageStorageCleanup:
    """Fail-closed cleanup for paths owned by a package session only."""

    TERMINAL_STATUSES = frozenset((
        PackageSessionStatus.FAILED.value, PackageSessionStatus.CANCELLED.value,
        PackageSessionStatus.EXPIRED.value, PackageSessionStatus.READY.value,
    ))

    @staticmethod
    def _owned_staging_path(session: PackageUploadSession, staging_root: str | Path | None = None) -> Path | None:
        root = Path(staging_root or settings.STORAGE_PACKAGES_STAGING_PATH).resolve()
        candidate = Path(session.staging_path).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return None
        if len(relative.parts) != 2 or relative.parts != (str(session.video_id), str(session.id)):
            return None
        return candidate

    @classmethod
    def remove_terminal_staging(cls, session: PackageUploadSession, staging_root: str | Path | None = None) -> bool:
        """Idempotently remove exactly one terminal package's staging directory."""
        if session.status not in cls.TERMINAL_STATUSES:
            logger.info("Skipping staging cleanup for active package %s (%s)", session.id, session.status)
            return False
        candidate = cls._owned_staging_path(session, staging_root)
        if candidate is None:
            logger.warning("Refusing unsafe package staging cleanup for session %s", session.id)
            return False
        if not candidate.exists():
            return True
        if candidate.is_symlink() or not candidate.is_dir():
            logger.warning("Refusing non-directory or symlink package staging path for session %s", session.id)
            return False
        try:
            shutil.rmtree(candidate)
            logger.info("Removed terminal package staging for session %s: %s", session.id, candidate)
            return True
        except OSError as exc:
            logger.error("Failed to remove package staging for session %s: %s", session.id, exc)
            return False

    @staticmethod
    def remove_abandoned_promotion_temp(*, video_id: uuid.UUID, generation_id: uuid.UUID,
                                        active_generation_id: uuid.UUID | None, has_active_job: bool,
                                        videos_root: str | Path | None = None) -> bool:
        """Remove only the exact abandoned destination-local promotion temp directory."""
        if has_active_job or generation_id == active_generation_id:
            return False
        root = Path(videos_root or settings.STORAGE_VIDEOS_PATH).resolve()
        candidate = (root / str(video_id) / "generations" / f".tmp_{generation_id}").resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return False
        if relative.parts != (str(video_id), "generations", f".tmp_{generation_id}"):
            return False
        if not candidate.exists():
            return True
        if candidate.is_symlink() or not candidate.is_dir():
            return False
        try:
            shutil.rmtree(candidate)
            logger.info("Removed abandoned promotion temp for video %s generation %s", video_id, generation_id)
            return True
        except OSError as exc:
            logger.error("Failed to remove promotion temp for video %s generation %s: %s", video_id, generation_id, exc)
            return False


class PackageService:
    @staticmethod
    def validate_archive_member(name: str, *, is_regular_file: bool, size: int) -> str:
        """Return a normalized safe tar path or reject links, traversal and oversized metadata."""
        from pathlib import PurePosixPath
        normalized = name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (not is_regular_file or path.is_absolute() or not path.parts or ".." in path.parts
                or ":" in normalized or "\0" in normalized or len(normalized) > settings.MAX_PATH_LENGTH):
            raise AboodVideoException("Archive contains an unsafe path or non-regular entry.", status_code=status.HTTP_400_BAD_REQUEST)
        if normalized == "manifest.json" and size > settings.MAX_MANIFEST_BYTES:
            raise AboodVideoException("Archive manifest exceeds the allowed size.", status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if normalized == "studio_package_identity.json" and size > 65536:
            raise AboodVideoException("Archive identity metadata exceeds the allowed size.", status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        return normalized

    @staticmethod
    def _archive_sidecar(session: PackageUploadSession) -> Path:
        staging = Path(session.staging_path).resolve()
        root = Path(settings.STORAGE_PACKAGES_STAGING_PATH).resolve()
        if root not in staging.parents or len(staging.relative_to(root).parts) != 2:
            raise AboodVideoException("Unsafe package staging path.", status_code=status.HTTP_400_BAD_REQUEST)
        return staging / ".archive-upload.json"

    @classmethod
    async def archive_status(cls, db: AsyncSession, session_id: uuid.UUID, api_key: ApiKey) -> Dict[str, Any]:
        session = (await db.execute(select(PackageUploadSession).where(PackageUploadSession.id == session_id))).scalar_one_or_none()
        if not session or session.created_by_key_id != api_key.id:
            raise AboodVideoException("Package session not found or access denied.", status_code=status.HTTP_404_NOT_FOUND)
        import json
        sidecar = cls._archive_sidecar(session)
        metadata = json.loads(sidecar.read_text("utf-8")) if sidecar.is_file() else None
        archive = Path(session.staging_path) / ".package.tar.part"
        offset = archive.stat().st_size if archive.is_file() else 0
        return {"session_id": str(session_id), "offset": offset, "archive": metadata}

    @classmethod
    async def archive_init(cls, db: AsyncSession, session_id: uuid.UUID, api_key: ApiKey, size: int, sha256: str) -> Dict[str, Any]:
        import json, re
        session = (await db.execute(select(PackageUploadSession).where(PackageUploadSession.id == session_id))).scalar_one_or_none()
        if not session or session.created_by_key_id != api_key.id:
            raise AboodVideoException("Package session not found or access denied.", status_code=status.HTTP_404_NOT_FOUND)
        if session.status not in (PackageSessionStatus.INITIALIZED.value, PackageSessionStatus.UPLOADING.value):
            raise AboodVideoException("Package session does not accept archive data.", status_code=status.HTTP_409_CONFLICT)
        if size < 1 or size > settings.MAX_PACKAGE_ARCHIVE_BYTES or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise AboodVideoException("Archive size or SHA-256 is invalid.", status_code=status.HTTP_400_BAD_REQUEST)
        sidecar = cls._archive_sidecar(session)
        archive = Path(session.staging_path) / ".package.tar.part"
        prior = json.loads(sidecar.read_text("utf-8")) if sidecar.is_file() else None
        if prior and (prior.get("size") != size or prior.get("sha256") != sha256.lower()):
            raise AboodVideoException("An upload archive with different identity is already in progress.", status_code=status.HTTP_409_CONFLICT)
        if not prior:
            sidecar.write_text(json.dumps({"size": size, "sha256": sha256.lower()}), encoding="utf-8")
        offset = archive.stat().st_size if archive.is_file() else 0
        if offset > size:
            raise AboodVideoException("Stored archive exceeds declared size.", status_code=status.HTTP_409_CONFLICT)
        return {"session_id": str(session_id), "offset": offset, "size": size}

    @classmethod
    async def append_package_archive(cls, db: AsyncSession, session_id: uuid.UUID, api_key: ApiKey, offset: int, content: bytes) -> Dict[str, Any]:
        import json
        from sqlalchemy import select
        session = (await db.execute(select(PackageUploadSession).where(PackageUploadSession.id == session_id).with_for_update())).scalar_one_or_none()
        if not session or session.created_by_key_id != api_key.id:
            raise AboodVideoException("Package session not found or access denied.", status_code=status.HTTP_404_NOT_FOUND)
        sidecar = cls._archive_sidecar(session)
        if not sidecar.is_file():
            raise AboodVideoException("Initialize the archive before sending chunks.", status_code=status.HTTP_409_CONFLICT)
        metadata = json.loads(sidecar.read_text("utf-8"))
        archive = Path(session.staging_path) / ".package.tar.part"
        current = archive.stat().st_size if archive.is_file() else 0
        if offset != current or not content or len(content) > 32 * 1024 * 1024 or current + len(content) > int(metadata["size"]):
            raise AboodVideoException("Archive chunk offset or size is invalid.", status_code=status.HTTP_409_CONFLICT)
        def append():
            try:
                with archive.open("ab") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                # A failed write can append a prefix before ENOSPC is raised.
                # Preserve exactly the previously acknowledged resumable offset.
                if archive.exists():
                    with archive.open("r+b") as stream:
                        stream.truncate(current)
                        stream.flush()
                        os.fsync(stream.fileno())
                raise
        try:
            await file_io(append)
        except OSError as exc:
            if is_storage_capacity_error(exc):
                raise StorageException("Insufficient storage while saving package archive. Free space and retry from the acknowledged offset.") from exc
            raise
        return {"offset": current + len(content), "size": int(metadata["size"])}

    @classmethod
    async def finalize_package_archive(cls, db: AsyncSession, session_id: uuid.UUID, api_key: ApiKey) -> Dict[str, Any]:
        import json, tarfile, tempfile
        session = (await db.execute(select(PackageUploadSession).where(PackageUploadSession.id == session_id).with_for_update())).scalar_one_or_none()
        if not session or session.created_by_key_id != api_key.id:
            raise AboodVideoException("Package session not found or access denied.", status_code=status.HTTP_404_NOT_FOUND)
        if session.status == PackageSessionStatus.UPLOADED.value:
            return {"status": "UPLOADED", "idempotent": True}
        sidecar = cls._archive_sidecar(session)
        archive = Path(session.staging_path) / ".package.tar.part"
        if not sidecar.is_file() or not archive.is_file():
            raise AboodVideoException("Archive upload is incomplete.", status_code=status.HTTP_409_CONFLICT)
        metadata = json.loads(sidecar.read_text("utf-8"))
        if archive.stat().st_size != int(metadata["size"]):
            raise AboodVideoException("Archive byte count is incomplete.", status_code=status.HTTP_409_CONFLICT)
        def unpack():
            digest = hashlib.sha256()
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != metadata["sha256"]:
                raise AboodVideoException("Archive SHA-256 mismatch.", status_code=status.HTTP_400_BAD_REQUEST)
            manifest = PackageManifest.model_validate(session.manifest_data)
            expected = {Path(item.path).as_posix(): item for item in manifest.files}
            staging = Path(session.staging_path).resolve()
            extract = Path(tempfile.mkdtemp(prefix=".extract-", dir=staging))
            seen = set()
            try:
                with tarfile.open(archive, mode="r:") as tar:
                    members = tar.getmembers()
                    if len(members) > settings.MAX_PACKAGE_FILES + 2:
                        raise AboodVideoException("Archive contains too many entries.", status_code=status.HTTP_400_BAD_REQUEST)
                    total_size = 0
                    for member in members:
                        rel = cls.validate_archive_member(member.name, is_regular_file=member.isfile(), size=member.size)
                        if rel in seen:
                            raise AboodVideoException("Archive contains an unsafe or duplicate path.", status_code=status.HTTP_400_BAD_REQUEST)
                        seen.add(rel)
                        if rel in ("manifest.json", "studio_package_identity.json"):
                            continue
                        spec = expected.get(rel)
                        if not spec or member.size != spec.size_bytes or member.size > settings.MAX_SINGLE_FILE_BYTES:
                            raise AboodVideoException("Archive entry does not match the declared package manifest.", status_code=status.HTTP_400_BAD_REQUEST)
                        total_size += member.size
                        if total_size > settings.MAX_TOTAL_PACKAGE_BYTES:
                            raise AboodVideoException("Archive exceeds maximum unpacked size.", status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
                        source = tar.extractfile(member)
                        if source is None:
                            raise AboodVideoException("Archive entry is unreadable.", status_code=status.HTTP_400_BAD_REQUEST)
                        target = (extract / rel).resolve()
                        if extract.resolve() not in target.parents:
                            raise AboodVideoException("Archive path escapes package root.", status_code=status.HTTP_400_BAD_REQUEST)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        hasher = hashlib.sha256()
                        with target.open("wb") as out:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                hasher.update(chunk)
                                out.write(chunk)
                        if hasher.hexdigest().lower() != spec.sha256.lower():
                            raise AboodVideoException("A package file checksum does not match the manifest.", status_code=status.HTTP_400_BAD_REQUEST)
                    if set(expected) != {path for path in seen if path not in ("manifest.json", "studio_package_identity.json")}:
                        raise AboodVideoException("Archive is missing one or more manifest files.", status_code=status.HTTP_400_BAD_REQUEST)
                    for rel in expected:
                        target = staging / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(extract / rel, target)
                return len(expected)
            except (tarfile.TarError, OSError) as exc:
                if is_storage_capacity_error(exc):
                    raise StorageException("Insufficient storage while extracting package archive. Free space and retry; the archive is preserved.") from exc
                raise AboodVideoException(
                    "Archive is corrupt or cannot be safely read.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                ) from exc
            finally:
                shutil.rmtree(extract, ignore_errors=True)
        try:
            file_count = await file_io(unpack)
        except OSError as exc:
            if is_storage_capacity_error(exc):
                raise StorageException("Insufficient storage while preparing package extraction. Free space and retry; the archive is preserved.") from exc
            raise
        for pkg_file in (await db.execute(select(PackageFile).where(PackageFile.package_session_id == session_id))).scalars():
            pkg_file.status = PackageFileStatus.COMPLETED.value
            pkg_file.received_bytes = pkg_file.expected_size
            pkg_file.verified_at = utc_now()
        session.completed_bytes = session.total_bytes
        session.status = PackageSessionStatus.UPLOADED.value
        await db.commit()
        await file_io(archive.unlink, missing_ok=True)
        await file_io(sidecar.unlink, missing_ok=True)
        return {"status": "UPLOADED", "files": file_count, "bytes": session.total_bytes}

    @staticmethod
    def validate_manifest_security(manifest: PackageManifest) -> None:
        """
        Validates strict manifest limits and path security:
        - Rejects traversal, drive letters, UNC, NUL, and control characters.
        - Disallows unexpected extensions and duplicate normalized paths.
        - Verifies that all declared rendition, font, and subtitle paths exist in files list.
        - Verifies that all subtitle font references exist in declared fonts.
        """
        if len(manifest.files) > settings.MAX_PACKAGE_FILES:
            raise AboodVideoException(
                f"Package file count ({len(manifest.files)}) exceeds maximum limit ({settings.MAX_PACKAGE_FILES}).",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        total_bytes = sum(f.size_bytes for f in manifest.files)
        if total_bytes > settings.MAX_TOTAL_PACKAGE_BYTES:
            raise AboodVideoException(
                f"Package total bytes ({total_bytes}) exceeds maximum allowed ({settings.MAX_TOTAL_PACKAGE_BYTES}).",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        seen_paths = set()
        file_path_map = {}

        for f in manifest.files:
            p_str = f.path.strip()
            # 1. Path length check
            if len(p_str) > settings.MAX_PATH_LENGTH:
                raise AboodVideoException(
                    f"Path length exceeds {settings.MAX_PATH_LENGTH} characters: {p_str[:50]}...",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # 2. Rejection of illegal characters, absolute paths, drive letters, and traversal
            if (
                p_str.startswith("/")
                or p_str.startswith("\\")
                or ":" in p_str
                or "\0" in p_str
                or ".." in p_str
                or "%2e" in p_str.lower()
                or "%2f" in p_str.lower()
                or "%5c" in p_str.lower()
            ):
                raise AboodVideoException(
                    f"Illegal or dangerous path pattern in manifest: {p_str}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # 3. Path normalization and duplicate detection
            norm_p = Path(p_str).as_posix()
            if norm_p.startswith("/") or norm_p.startswith("../") or norm_p == "..":
                raise AboodVideoException(
                    f"Path escapes package root: {p_str}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if norm_p in seen_paths:
                raise AboodVideoException(
                    f"Duplicate normalized path declared in manifest: {norm_p}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            seen_paths.add(norm_p)
            file_path_map[norm_p] = f

            # 4. Extension check
            ext = Path(norm_p).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise AboodVideoException(
                    f"Disallowed file extension '{ext}' in manifest for file: {norm_p}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # 5. Checksum format check
            if not re.match(r"^[0-9a-fA-F]{64}$", f.sha256):
                raise AboodVideoException(
                    f"Invalid SHA-256 hash declared for file: {norm_p}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        # 6. Verify declared renditions map to manifest files
        for r in manifest.renditions:
            r_norm = Path(r.playlist_path).as_posix()
            if r_norm not in file_path_map:
                raise AboodVideoException(
                    f"Rendition playlist '{r.playlist_path}' declared in renditions but missing from files list.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        # 7. Verify declared fonts
        declared_font_map = {}
        for font in (manifest.fonts or []):
            f_p = font.path.strip()
            # Font path must be relative, no .. , no absolute, no urls
            if (
                f_p.startswith("/")
                or f_p.startswith("\\")
                or ":" in f_p
                or "\0" in f_p
                or ".." in f_p
                or "%2e" in f_p.lower()
                or "%2f" in f_p.lower()
                or "%5c" in f_p.lower()
            ):
                raise AboodVideoException(
                    f"Illegal or dangerous font path pattern: {f_p}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            f_norm = Path(f_p).as_posix()
            if f_norm.startswith("/") or f_norm.startswith("../") or f_norm == "..":
                raise AboodVideoException(
                    f"Font path escapes package root: {f_p}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            f_ext = Path(f_norm).suffix.lower()
            if f_ext not in (".ttf", ".otf"):
                raise AboodVideoException(
                    f"Disallowed font extension '{f_ext}' for font '{f_norm}'. Only .ttf and .otf are allowed.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if f_norm not in file_path_map:
                raise AboodVideoException(
                    f"Font file '{font.path}' declared in fonts but missing from files list.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            target_f = file_path_map[f_norm]
            if target_f.sha256.lower() != font.sha256.lower():
                raise AboodVideoException(
                    f"SHA-256 mismatch between font declaration '{font.path}' and manifest files entry.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            declared_font_map[f_norm] = font
            declared_font_map[font.filename] = font
            declared_font_map[font.family_name] = font

        # 8. Verify declared subtitles map to manifest files and formats
        VALID_SUBTITLE_FORMATS = {"vtt", "srt", "ass", "ssa"}
        for s in manifest.subtitles:
            s_norm = Path(s.path).as_posix()
            if s_norm not in file_path_map:
                raise AboodVideoException(
                    f"Subtitle file '{s.path}' declared in subtitles but missing from files list.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            s_ext = Path(s_norm).suffix.lower().lstrip(".")
            s_fmt = (s.format or s_ext).lower()
            if s_fmt not in VALID_SUBTITLE_FORMATS:
                raise AboodVideoException(
                    f"Disallowed subtitle format '{s_fmt}' for '{s.path}'. Allowed: {', '.join(VALID_SUBTITLE_FORMATS)}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            sub_file_entry = file_path_map[s_norm]
            if sub_file_entry.sha256.lower() != s.sha256.lower():
                raise AboodVideoException(
                    f"SHA-256 mismatch between subtitle declaration '{s.path}' and manifest files entry.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if s.fonts:
                for font_ref in s.fonts:
                    if font_ref not in declared_font_map:
                        raise AboodVideoException(
                            f"Subtitle '{s.path}' references undeclared font '{font_ref}'. Every referenced font must exist in manifest.",
                            status_code=status.HTTP_400_BAD_REQUEST,
                        )

    @classmethod
    async def init_package_session(
        cls,
        db: AsyncSession,
        req: PackageInitRequest,
        api_key: ApiKey,
    ) -> PackageInitResponse:
        """Initializes a new package upload session and sets up staging directory."""
        # Rollback expires ORM identities; preserve this scalar for conflict recovery.
        api_key_id = api_key.id
        cls.validate_manifest_security(req.manifest)

        total_bytes = sum(f.size_bytes for f in req.manifest.files)
        total_files = len(req.manifest.files)

        request_fingerprint = package_request_fingerprint(req)
        if req.idempotency_key:
            existing_result = await db.execute(
                select(PackageUploadSession)
                .where(
                    PackageUploadSession.created_by_key_id == api_key_id,
                    PackageUploadSession.idempotency_key == req.idempotency_key.lower(),
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                require_matching_request_fingerprint(existing.request_fingerprint, request_fingerprint)
                files = (await db.execute(
                    select(PackageFile).where(PackageFile.package_session_id == existing.id)
                )).scalars().all()
                return PackageInitResponse(
                    session_id=existing.id,
                    video_id=existing.video_id,
                    status=existing.status,
                    total_files=existing.total_files,
                    total_bytes=existing.total_bytes,
                    files=[PackageDeclaredFileItem(
                        file_id=item.id, relative_path=item.relative_path, expected_size=item.expected_size,
                        expected_sha256=item.expected_sha256, media_type=item.media_type,
                        status=item.status, received_bytes=item.received_bytes,
                    ) for item in files],
                )

        # Storage admission check (with per-filesystem evaluation)
        check_package_storage_admission(declared_total_bytes=total_bytes)

        # Authoritative destination contract validation
        target_folder = None
        target_series = None
        target_season = None

        if req.folder_id:
            f_stmt = select(Folder).where(Folder.id == req.folder_id)
            f_res = await db.execute(f_stmt)
            target_folder = f_res.scalar_one_or_none()
            if not target_folder or target_folder.trashed_at is not None:
                raise AboodVideoException("Target folder not found.", status_code=status.HTTP_404_NOT_FOUND)
            if target_folder.user_id != api_key.user_id:
                u_stmt = select(User).where(User.id == api_key.user_id)
                u_res = await db.execute(u_stmt)
                user_record = u_res.scalar_one_or_none()
                if not user_record or user_record.role != UserRole.ADMIN:
                    raise AboodVideoException("Access denied: Not authorized for target folder.", status_code=status.HTTP_403_FORBIDDEN)

        if req.series_id:
            s_stmt = select(Series).where(Series.id == req.series_id)
            s_res = await db.execute(s_stmt)
            target_series = s_res.scalar_one_or_none()
            if not target_series:
                raise AboodVideoException("Target series not found.", status_code=status.HTTP_404_NOT_FOUND)
            if target_series.owner_id != api_key.user_id:
                u_stmt = select(User).where(User.id == api_key.user_id)
                u_res = await db.execute(u_stmt)
                user_record = u_res.scalar_one_or_none()
                if not user_record or user_record.role != UserRole.ADMIN:
                    raise AboodVideoException("Access denied: Not authorized for target series.", status_code=status.HTTP_403_FORBIDDEN)
            if target_series.trashed_at is not None:
                raise AboodVideoException("Target series is in trash.", status_code=status.HTTP_400_BAD_REQUEST)
            if req.folder_id and target_series.parent_folder_id and target_series.parent_folder_id != req.folder_id:
                raise AboodVideoException("Target series does not belong to specified target folder.", status_code=status.HTTP_400_BAD_REQUEST)

        if req.season_id:
            season_stmt = select(Season).where(Season.id == req.season_id)
            season_res = await db.execute(season_stmt)
            target_season = season_res.scalar_one_or_none()
            if not target_season:
                raise AboodVideoException("Target season not found.", status_code=status.HTTP_404_NOT_FOUND)
            if target_season.trashed_at is not None:
                raise AboodVideoException("Target season is in trash.", status_code=status.HTTP_400_BAD_REQUEST)

            if req.series_id:
                if target_season.series_id != req.series_id:
                    raise AboodVideoException("Target season does not belong to specified target series.", status_code=status.HTTP_400_BAD_REQUEST)
            else:
                s_stmt = select(Series).where(Series.id == target_season.series_id)
                s_res = await db.execute(s_stmt)
                parent_series = s_res.scalar_one_or_none()
                if not parent_series:
                    raise AboodVideoException("Target season's parent series not found.", status_code=status.HTTP_404_NOT_FOUND)
                if parent_series.owner_id != api_key.user_id:
                    u_stmt = select(User).where(User.id == api_key.user_id)
                    u_res = await db.execute(u_stmt)
                    user_record = u_res.scalar_one_or_none()
                    if not user_record or user_record.role != UserRole.ADMIN:
                        raise AboodVideoException("Access denied: Not authorized for series containing target season.", status_code=status.HTTP_403_FORBIDDEN)
                req.series_id = target_season.series_id
                target_series = parent_series

        if req.episode_number is not None and req.episode_number < 0:
            raise AboodVideoException("Episode number must be non-negative.", status_code=status.HTTP_400_BAD_REQUEST)

        # Resolve or create Video entity
        if req.video_id:
            stmt = select(Video).where(Video.id == req.video_id)
            result = await db.execute(stmt)
            video = result.scalar_one_or_none()
            if not video:
                raise AboodVideoException("Target video_id not found.", status_code=status.HTTP_404_NOT_FOUND)
            # Only ADMIN may override another owner.
            if video.user_id != api_key.user_id:
                u_stmt = select(User).where(User.id == api_key.user_id)
                u_res = await db.execute(u_stmt)
                user_record = u_res.scalar_one_or_none()
                if not user_record or user_record.role != UserRole.ADMIN:
                    raise AboodVideoException("Access denied: Not authorized for target video.", status_code=status.HTTP_403_FORBIDDEN)
            if req.folder_id is not None:
                video.folder_id = req.folder_id
            if req.series_id is not None:
                video.series_id = req.series_id
            if req.season_id is not None:
                video.season_id = req.season_id
                if target_season:
                    video.season_number = target_season.season_number
            if req.episode_number is not None:
                video.episode_number = req.episode_number
        else:
            video = Video(
                user_id=api_key.user_id,
                title=req.manifest.video.title.strip(),
                description=req.manifest.video.description,
                status=VideoStatus.PROCESSING.value,
                visibility=VideoVisibility.UNLISTED.value,
                share_id=secrets.token_urlsafe(16),
                source_filename=f"studio_package_{uuid.uuid4().hex[:8]}.m3u8",
                source_size_bytes=total_bytes,
                source_duration_seconds=req.manifest.video.duration_seconds,
                folder_id=req.folder_id,
                series_id=req.series_id,
                season_id=req.season_id,
                episode_number=req.episode_number,
                season_number=target_season.season_number if target_season else None,
            )
            db.add(video)
            await db.flush()

        session_id = uuid.uuid4()
        target_generation_id = uuid.uuid4()
        staging_dir = Path(settings.STORAGE_PACKAGES_STAGING_PATH) / str(video.id) / str(session_id)
        staging_dir.mkdir(parents=True, exist_ok=True)

        package_session = PackageUploadSession(
            id=session_id,
            video_id=video.id,
            created_by_key_id=api_key_id,
            idempotency_key=req.idempotency_key.lower() if req.idempotency_key else None,
            request_fingerprint=request_fingerprint if req.idempotency_key else None,
            status=PackageSessionStatus.INITIALIZED.value,
            total_files=total_files,
            total_bytes=total_bytes,
            completed_bytes=0,
            manifest_data=req.manifest.model_dump(),
            staging_path=str(staging_dir),
            target_generation_id=target_generation_id,
            expires_at=utc_now() + timedelta(hours=48),
        )
        db.add(package_session)

        # Register all declared files
        file_items: List[PackageDeclaredFileItem] = []
        for f in req.manifest.files:
            file_id = uuid.uuid4()
            norm_path = Path(f.path).as_posix()
            pkg_file = PackageFile(
                id=file_id,
                package_session_id=session_id,
                relative_path=norm_path,
                expected_size=f.size_bytes,
                expected_sha256=f.sha256.lower(),
                media_type=f.media_type or "application/octet-stream",
                status=PackageFileStatus.PENDING.value,
                received_bytes=0,
            )
            db.add(pkg_file)
            file_items.append(
                PackageDeclaredFileItem(
                    file_id=file_id,
                    relative_path=norm_path,
                    expected_size=f.size_bytes,
                    expected_sha256=f.sha256.lower(),
                    media_type=pkg_file.media_type,
                    status=PackageFileStatus.PENDING.value,
                    received_bytes=0,
                )
            )

        try:
            await db.commit()
        except Exception as exc:
            # A concurrent retry with the same key may have committed first.
            if not req.idempotency_key or "uq_package_session_idempotency" not in str(exc):
                raise
            await db.rollback()
            shutil.rmtree(staging_dir, ignore_errors=True)
            existing = (await db.execute(select(PackageUploadSession).where(
                PackageUploadSession.created_by_key_id == api_key_id,
                PackageUploadSession.idempotency_key == req.idempotency_key.lower(),
            ))).scalar_one_or_none()
            if not existing:
                raise
            require_matching_request_fingerprint(existing.request_fingerprint, request_fingerprint)
            existing_files = (await db.execute(select(PackageFile).where(
                PackageFile.package_session_id == existing.id,
            ))).scalars().all()
            return PackageInitResponse(
                session_id=existing.id, video_id=existing.video_id, status=existing.status,
                total_files=existing.total_files, total_bytes=existing.total_bytes,
                files=[PackageDeclaredFileItem(
                    file_id=item.id, relative_path=item.relative_path, expected_size=item.expected_size,
                    expected_sha256=item.expected_sha256, media_type=item.media_type,
                    status=item.status, received_bytes=item.received_bytes,
                ) for item in existing_files],
            )
        logger.info(f"Package session initialized: {session_id} for video {video.id} ({total_files} files)")

        return PackageInitResponse(
            session_id=session_id,
            video_id=video.id,
            status=PackageSessionStatus.INITIALIZED.value,
            total_files=total_files,
            total_bytes=total_bytes,
            files=file_items,
        )

    @staticmethod
    async def _refresh_file_progress(db: AsyncSession, session: PackageUploadSession) -> None:
        """Recount persisted files after flushing; repairing a retry never adds twice."""
        await db.flush()
        completed, pending = (await db.execute(select(
            func.coalesce(func.sum(case((PackageFile.status == PackageFileStatus.COMPLETED.value, PackageFile.received_bytes), else_=0)), 0),
            func.count(PackageFile.id).filter(PackageFile.status != PackageFileStatus.COMPLETED.value),
        ).where(PackageFile.package_session_id == session.id))).one()
        session.completed_bytes = int(completed)
        session.status = PackageSessionStatus.UPLOADED.value if pending == 0 else PackageSessionStatus.UPLOADING.value

    @classmethod
    async def upload_file_chunk_stream(
        cls,
        db: AsyncSession,
        session_id: uuid.UUID,
        file_id: uuid.UUID,
        stream,
        api_key: Optional[ApiKey] = None,
    ) -> Dict[str, Any]:
        """
        Crash-safe streaming write using a unique request-specific part file.
        Enforces size cap, calculates incremental SHA-256, fsyncs, and renames atomically.
        """
        from app.utils.file_io import file_io
        # Lock the shared progress record first in every upload request. This
        # PostgreSQL lock also serializes writers in other API processes.
        session = (await db.execute(
            select(PackageUploadSession).where(PackageUploadSession.id == session_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if not session:
            raise AboodVideoException("Package session not found.", status_code=404)
        if api_key and session.created_by_key_id != api_key.id:
            raise AboodVideoException("Access denied: Not authorized for this package session.", status_code=403)
        if session.status not in (PackageSessionStatus.INITIALIZED.value, PackageSessionStatus.UPLOADING.value, PackageSessionStatus.UPLOADED.value):
            raise AboodVideoException("Package session no longer accepts file uploads.", status_code=409)
        if session.expires_at < utc_now():
            raise AboodVideoException("Package upload session expired.", status_code=409)
        # Lookup session and file
        stmt = (
            select(PackageFile)
            .join(PackageUploadSession)
            .where(
                PackageFile.id == file_id,
                PackageFile.package_session_id == session_id,
            )
        ).with_for_update(of=PackageFile).execution_options(populate_existing=True)
        result = await db.execute(stmt)
        pkg_file = result.scalar_one_or_none()

        if not pkg_file:
            raise AboodVideoException("File declaration not found in package session.", status_code=status.HTTP_404_NOT_FOUND)

        staging_dir = Path(session.staging_path)
        canonical_dest = staging_dir / pkg_file.relative_path

        # Idempotent re-upload check: if already completed and hash matches on disk
        if pkg_file.status == PackageFileStatus.COMPLETED.value and canonical_dest.exists():
            def valid_existing():
                if canonical_dest.stat().st_size != pkg_file.expected_size:
                    return False
                digest = hashlib.sha256()
                with canonical_dest.open("rb") as existing:
                    for block in iter(lambda: existing.read(1024 * 1024), b""):
                        digest.update(block)
                return digest.hexdigest().lower() == pkg_file.expected_sha256.lower()
            if await file_io(valid_existing):
                await cls._refresh_file_progress(db, session)
                await db.commit()
                return {
                    "file_id": str(file_id),
                    "status": "COMPLETED",
                    "idempotent": True,
                    "already_completed": True,
                    "received_bytes": pkg_file.expected_size,
                }

        part_file = staging_dir / f"{file_id}.{uuid.uuid4().hex}.part"
        hasher = hashlib.sha256()
        received_bytes = 0

        try:
            with open(part_file, "wb") as f_out:
                async for chunk in stream:
                    if not chunk:
                        continue
                    received_bytes += len(chunk)
                    if received_bytes > pkg_file.expected_size:
                        f_out.close()
                        if part_file.exists():
                            part_file.unlink()
                        raise AboodVideoException(
                            f"File size exceeded expected maximum ({pkg_file.expected_size} bytes).",
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        )
                    await file_io(hasher.update, chunk)
                    await file_io(f_out.write, chunk)
                await file_io(f_out.flush)
                await file_io(os.fsync, f_out.fileno())
        except (AboodVideoException, Exception) as e:
            if part_file.exists():
                try:
                    part_file.unlink()
                except Exception:
                    pass
            if isinstance(e, AboodVideoException):
                raise
            if is_storage_capacity_error(e):
                raise StorageException("Insufficient storage while receiving package file. Free space and retry; completed files are preserved.") from e
            raise AboodVideoException(f"Error streaming file: {e}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # EOF verification
        if received_bytes != pkg_file.expected_size:
            if part_file.exists():
                part_file.unlink()
            raise AboodVideoException(
                f"Received byte count ({received_bytes}) does not match expected size ({pkg_file.expected_size}).",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        calc_sha256 = hasher.hexdigest().lower()
        if calc_sha256 != pkg_file.expected_sha256:
            if part_file.exists():
                part_file.unlink()
            raise AboodVideoException(
                f"SHA-256 checksum mismatch for file {pkg_file.relative_path}. "
                f"Expected: {pkg_file.expected_sha256}, Calculated: {calc_sha256}",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # Atomic rename to canonical relative path inside staging
        canonical_dest.parent.mkdir(parents=True, exist_ok=True)
        await file_io(os.replace, part_file, canonical_dest)

        # Update database records
        pkg_file.status = PackageFileStatus.COMPLETED.value
        pkg_file.received_bytes = received_bytes
        pkg_file.verified_at = utc_now()

        await cls._refresh_file_progress(db, session)
        await db.commit()

        return {
            "file_id": str(file_id),
            "relative_path": pkg_file.relative_path,
            "status": "COMPLETED",
            "received_bytes": received_bytes,
            "sha256": calc_sha256,
        }

    @classmethod
    async def finalize_package_session(
        cls,
        db: AsyncSession,
        session_id: uuid.UUID,
        api_key: Optional[ApiKey] = None,
    ) -> PackageFinalizeResponse:
        """
        Asynchronously finalizes a package upload session.
        Validates all files uploaded, marks QUEUED_FOR_VERIFICATION,
        and enqueues background worker job with deterministic ID.
        """
        # Row-level lock on session
        stmt = (
            select(PackageUploadSession)
            .where(PackageUploadSession.id == session_id)
            .with_for_update(of=PackageUploadSession)
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise AboodVideoException("Package session not found.", status_code=status.HTTP_404_NOT_FOUND)

        if api_key and session.created_by_key_id != api_key.id:
            raise AboodVideoException("Access denied: Not authorized for this package session.", status_code=status.HTTP_403_FORBIDDEN)

        # Idempotency check: If already queued or in progress, return 202 with existing status
        if session.status in (
            PackageSessionStatus.QUEUED_FOR_VERIFICATION.value,
            PackageSessionStatus.VERIFYING.value,
            PackageSessionStatus.PROMOTING.value,
            PackageSessionStatus.READY.value,
        ):
            return PackageFinalizeResponse(
                package_id=session.id,
                status=session.status,
                verification_job_id=session.verification_job_id,
                message="Package verification already enqueued or in progress.",
            )

        # Verify that all files have been uploaded
        uncompleted_stmt = select(PackageFile).where(
            PackageFile.package_session_id == session_id,
            PackageFile.status != PackageFileStatus.COMPLETED.value,
        )
        uncompleted_files = (await db.execute(uncompleted_stmt)).scalars().all()

        if uncompleted_files:
            missing_paths = [f.relative_path for f in uncompleted_files[:10]]
            raise AboodVideoException(
                f"Cannot finalize: {len(uncompleted_files)} declared files have not completed upload.",
                status_code=status.HTTP_409_CONFLICT,
                details={"uncompleted_count": len(uncompleted_files), "sample_missing": missing_paths},
            )

        # Prepare deterministic RQ job ID
        job_id = f"pkg_verify_{session.id}"
        session.status = PackageSessionStatus.QUEUED_FOR_VERIFICATION.value
        session.verification_job_id = job_id
        session.verification_enqueued_at = utc_now()
        session.finalize_attempts += 1
        await db.commit()

        # Enqueue verification in RQ worker
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=DeprecationWarning)
                redis_conn = Redis.from_url(settings.REDIS_QUEUE_URL)
                q = Queue("transcode", connection=redis_conn)
                q.enqueue(
                    "app.workers.package_worker.verify_and_promote_package",
                    str(session.id),
                    job_id=job_id,
                    job_timeout=3600,
                    result_ttl=86400,
                )
            logger.info(f"Enqueued package verification job {job_id} for package {session.id}")
        except Exception as e:
            logger.critical(f"Failed to enqueue RQ verification job {job_id}: {e}")
            raise AboodVideoException(
                message="Failed to dispatch verification worker queue. Package state preserved; please retry finalize.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                details={"session_id": str(session.id), "job_id": job_id, "retry_after_seconds": 5},
            )

        return PackageFinalizeResponse(
            package_id=session.id,
            status=PackageSessionStatus.QUEUED_FOR_VERIFICATION.value,
            verification_job_id=job_id,
            message="Package verification enqueued asynchronously.",
        )


def package_request_fingerprint(req: PackageInitRequest) -> str:
    """Hash all normalized request semantics while excluding the retry key itself."""
    payload = req.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_matching_request_fingerprint(stored: Optional[str], expected: str) -> None:
    """Refuse replay when the original request cannot be proven equivalent."""
    if not stored:
        raise AboodVideoException(
            "The original idempotency request fingerprint is unavailable; refusing an unsafe replay.",
            status_code=status.HTTP_409_CONFLICT,
        )
    if stored != expected:
        raise AboodVideoException(
            "Idempotency key was already used for a different package request.",
            status_code=status.HTTP_409_CONFLICT,
        )
