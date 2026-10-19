"""Authenticated direct HLS/MP4 import; intentionally does not enqueue workers."""
from __future__ import annotations

import shutil
import tempfile
import uuid
import json
import os
import portalocker
import re
import hashlib
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, UserRole
from app.models.audit import AuditLog
from app.models.video import Video, VideoRendition, VideoStatus, VideoVisibility
from app.security.auth import record_audit_log, require_owner_or_admin
from app.security.csrf import check_csrf_protection
from app.services.direct_import import (
    DirectImportError,
    _relative_path,
    extract_and_verify,
    inspect_package,
    publish_import,
    validate_share_id,
)

def require_direct_import_enabled():
    if not settings.ENABLE_DIRECT_IMPORT:
        raise HTTPException(503, "Direct import is disabled")


router = APIRouter(
    prefix="/admin/videos",
    tags=["Admin Direct Import"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection), Depends(require_direct_import_enabled)],
)

_SERVER_IMPORT_EXTENSIONS = {".m3u8", ".ts", ".m4s", ".mp4", ".m4a", ".aac", ".key", ".vtt"}
def _validate_requested_share_id(value: Any) -> str | None:
    """Validate the token_urlsafe(16) shape used by existing share links."""
    try:
        return validate_share_id(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _import_result(
    video_id: uuid.UUID, generation_id: uuid.UUID, inspection: Any,
    *, share_id: str | None = None, idempotent: bool = False,
) -> dict[str, Any]:
    return {
        "id": str(video_id), "generation_id": str(generation_id),
        "title": inspection.title, "media_type": inspection.media_type,
        "visibility": "PRIVATE", "status": "READY", "transcoding": False,
        "duration_seconds": inspection.duration, "width": inspection.width,
        "height": inspection.height, "video_codec": inspection.video_codec,
        "audio_codec": inspection.audio_codec, "size_bytes": inspection.size_bytes,
        "sha256": inspection.sha256,
        "playback_path": f"/watch/{share_id or video_id}",
        "share_id": share_id,
        "idempotent_replay": idempotent,
        "renditions": len(inspection.variants) if inspection.media_type == "hls" else 1,
    }


def _server_import_root() -> Path:
    """Return only the dedicated, read-only ABOODVIDEO import mount."""
    configured = Path(os.environ.get("ABV_IMPORT_PATH", "/app/import"))
    if configured.is_symlink():
        raise HTTPException(503, "ABOODVIDEO import directory is not a safe directory")
    try:
        root = configured.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(503, "ABOODVIDEO import directory is unavailable") from exc
    if not root.is_dir():
        raise HTTPException(503, "ABOODVIDEO import directory is unavailable")
    return root


def _server_candidate(root: Path, requested: Any) -> Path:
    if (not isinstance(requested, str) or not requested or len(requested) > 255
            or requested in (".", "..") or "/" in requested or "\\" in requested
            or any(ord(char) < 32 for char in requested)):
        raise HTTPException(422, "Choose one item directly inside the ABOODVIDEO import directory")
    candidate = root / requested
    try:
        if candidate.is_symlink():
            raise HTTPException(422, "Symbolic links are not allowed in the import directory")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(404, "Import item not found") from exc
    if resolved.parent != root or not resolved.is_relative_to(root) or not resolved.exists():
        raise HTTPException(404, "Import item not found")
    return resolved


def _walk_server_hls(candidate: Path, *, hash_files: bool = True) -> list[tuple[str, Path, int, str]]:
    if candidate.is_symlink() or not candidate.is_dir() or not (candidate / "master.m3u8").is_file():
        raise DirectImportError("HLS directory must contain master.m3u8 at its root")
    found: list[tuple[str, Path, int, str]] = []
    seen: set[str] = set()
    total = 0
    for current, dirs, names in os.walk(candidate, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink() or not current_path.resolve(strict=True).is_relative_to(candidate):
            raise DirectImportError("Symbolic links or paths outside the selected HLS directory are not allowed")
        for dirname in list(dirs):
            child = current_path / dirname
            if child.is_symlink():
                raise DirectImportError("Symbolic links are not allowed in HLS imports")
        dirs.sort()
        for filename in sorted(names):
            file_path = current_path / filename
            if file_path.is_symlink() or not file_path.is_file() or not file_path.resolve(strict=True).is_relative_to(candidate):
                raise DirectImportError("Only regular files inside the selected HLS directory can be imported")
            if file_path.suffix.lower() not in _SERVER_IMPORT_EXTENSIONS:
                raise DirectImportError(f"Unsupported HLS asset extension: {file_path.suffix or '(none)'}")
            relative = file_path.relative_to(candidate).as_posix()
            rel = str(PurePosixPath(relative))
            if rel.casefold() in seen:
                raise DirectImportError("Duplicate case-insensitive HLS path")
            _relative_path(rel)
            seen.add(rel.casefold())
            size = file_path.stat().st_size
            total += size
            if size <= 0 or size > settings.MAX_SINGLE_FILE_BYTES or total > settings.DIRECT_IMPORT_MAX_BYTES:
                raise DirectImportError("HLS files exceed the configured import size limits")
            digest = hashlib.sha256()
            if hash_files:
                with file_path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            if file_path.stat().st_size != size:
                raise DirectImportError("HLS source changed while it was being verified")
            found.append((rel, file_path, size, digest.hexdigest() if hash_files else ""))
            if len(found) > settings.MAX_PACKAGE_FILES:
                raise DirectImportError("HLS directory contains too many files")
    if "master.m3u8" not in {entry[0] for entry in found}:
        raise DirectImportError("HLS directory must contain master.m3u8")
    return found


def _package_server_import(candidate: Path, archive_path: Path) -> tuple[str, int]:
    """Create a checksummed temporary package from one allowlisted HLS tree or MP4."""
    if candidate.is_file() and candidate.suffix.lower() == ".mp4" and not candidate.is_symlink():
        size = candidate.stat().st_size
        if size <= 0 or size > settings.MAX_SINGLE_FILE_BYTES or size > settings.DIRECT_IMPORT_MAX_BYTES:
            raise DirectImportError("MP4 size is outside the configured import limits")
        digest = hashlib.sha256()
        with candidate.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        rel = candidate.name
        _relative_path(rel)
        files = [(rel, candidate, size, digest.hexdigest())]
        media_type, entrypoint = "mp4", rel
    elif candidate.is_dir():
        files = _walk_server_hls(candidate)
        media_type, entrypoint = "hls", "master.m3u8"
    else:
        raise DirectImportError("Choose an HLS directory or a .mp4 file from the dedicated import directory")

    title = candidate.stem[:255] or "Server import"
    manifest = {
        "format": "abv-direct-import-v1", "media_type": media_type,
        "title": title, "entrypoint": entrypoint,
        "files": [{"path": rel, "size": size, "sha256": digest} for rel, _, size, digest in files],
    }
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as package:
        package.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        for rel, path, size, expected_hash in files:
            if path.is_symlink() or not path.is_file():
                raise DirectImportError("Server import source changed during packaging")
            digest = hashlib.sha256()
            written = 0
            with path.open("rb") as source, package.open(rel, "w", force_zip64=True) as target:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    digest.update(chunk)
                    target.write(chunk)
            if written != size or digest.hexdigest() != expected_hash:
                raise DirectImportError("Server import source changed during packaging")
    return media_type, sum(item[2] for item in files)


@router.get("/direct-import/server-items")
async def list_server_import_items() -> dict[str, Any]:
    """List eligible direct children only; never browse the host filesystem."""
    root = _server_import_root()
    items = []
    for path in sorted(root.iterdir(), key=lambda entry: entry.name.casefold()):
        if path.name.startswith(".") or path.name in (".", "..") or path.is_symlink():
            continue
        if path.is_file() and path.suffix.lower() == ".mp4":
            items.append({"name": path.name, "media_type": "mp4", "size_bytes": path.stat().st_size})
        elif path.is_dir() and (path / "master.m3u8").is_file() and not (path / "master.m3u8").is_symlink():
            try:
                files = _walk_server_hls(path, hash_files=False)
                items.append({"name": path.name, "media_type": "hls", "size_bytes": sum(file[2] for file in files), "file_count": len(files)})
            except DirectImportError:
                continue
    return {"items": items}


@router.post("/direct-import/server", status_code=201)
async def import_server_item(
    request: Request, payload: dict[str, Any] = Body(...),
    current_user: User = Depends(require_owner_or_admin), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Import only one selected direct child of the dedicated read-only import mount."""
    if not settings.ENABLE_DIRECT_IMPORT:
        raise HTTPException(503, "Direct import is disabled")
    root = _server_import_root()
    share_id = _validate_requested_share_id(payload.get("share_id"))
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise HTTPException(422, "dry_run must be a boolean")
    candidate = _server_candidate(root, payload.get("name"))
    archive_root = Path(settings.STORAGE_TEMP_PATH) / "direct-import"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive = archive_root / f"server-{uuid.uuid4().hex}.zip"
    try:
        _package_server_import(candidate, archive)
        return await _register_archive(archive, request, current_user, db, share_id=share_id, dry_run=dry_run)
    except DirectImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        archive.unlink(missing_ok=True)


async def _save_bounded(upload: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("xb") as output:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > settings.DIRECT_IMPORT_MAX_BYTES:
                raise DirectImportError("direct-import upload limit exceeded")
            output.write(chunk)
    if total == 0:
        raise DirectImportError("empty import archive")
    return total


@router.post("/direct-import", status_code=201)
async def create_direct_import(
    request: Request,
    package: UploadFile = File(..., description="Checksummed abv-direct-import-v1 ZIP"),
    share_id: str | None = Form(default=None, description="Optional unassigned URL-safe share ID to preserve"),
    dry_run: bool = Form(default=False, description="Validate without publishing media or creating a record"),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Validate a safe package, atomically publish it, and create READY metadata."""
    if not settings.ENABLE_DIRECT_IMPORT:
        raise HTTPException(503, "Direct import is disabled")
    share_id = _validate_requested_share_id(share_id)
    if not package.filename or Path(package.filename).suffix.lower() != ".zip":
        raise HTTPException(415, "A ZIP package is required")
    root = Path(settings.STORAGE_TEMP_PATH) / "direct-import"
    root.mkdir(parents=True, exist_ok=True)
    archive_path = root / f"{uuid.uuid4().hex}.zip"
    try:
        await _save_bounded(package, archive_path)
        return await _register_archive(archive_path, request, current_user, db, share_id=share_id, dry_run=dry_run)
    except DirectImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await package.close()
        archive_path.unlink(missing_ok=True)


async def _register_archive(
    archive_path: Path, request: Request, current_user: User, db: AsyncSession,
    *, share_id: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Shared verified import transaction for ordinary and resumable uploads."""
    root = Path(settings.STORAGE_TEMP_PATH) / "direct-import"
    root.mkdir(parents=True, exist_ok=True)
    extracted = root / f"{uuid.uuid4().hex}.payload"
    video_id, generation_id = uuid.uuid4(), uuid.uuid4()
    target = Path(settings.STORAGE_VIDEOS_PATH) / str(video_id) / "generations" / str(generation_id)
    published = False
    try:
        try:
            with zipfile.ZipFile(archive_path) as package_zip:
                infos = package_zip.infolist()
                payload_bytes = sum(info.file_size for info in infos if not info.is_dir() and info.filename != "manifest.json")
                payload_files = sum(1 for info in infos if not info.is_dir() and info.filename != "manifest.json")
        except (OSError, zipfile.BadZipFile) as exc:
            raise DirectImportError("invalid direct-import ZIP archive") from exc
        if payload_files < 1 or payload_files > settings.MAX_PACKAGE_FILES:
            raise DirectImportError("ZIP contains an unsupported number of media files")
        if payload_bytes > settings.DIRECT_IMPORT_MAX_BYTES:
            raise DirectImportError("ZIP payload exceeds the configured media size limit")
        free_bytes = shutil.disk_usage(settings.STORAGE_BASE_PATH).free
        if free_bytes < payload_bytes * 2 + settings.STORAGE_SAFETY_MARGIN_BYTES:
            raise HTTPException(507, "Insufficient free space to validate and publish this package safely")
        extract_and_verify(archive_path, extracted)
        inspection, manifest, records = inspect_package(extracted)
        if inspection.media_type == "hls" and inspection.entrypoint != "master.m3u8":
            raise DirectImportError("HLS entrypoint must be master.m3u8 for the existing playback contract")
        duplicate = await db.scalar(select(AuditLog.target_id).where(
            AuditLog.action == "VIDEO_DIRECT_IMPORTED",
            AuditLog.user_id == current_user.id,
            AuditLog.target_entity == "video",
            AuditLog.metadata_json["source_sha256"].astext == inspection.sha256,
        ).limit(1))
        if duplicate:
            try:
                duplicate_video = await db.get(Video, uuid.UUID(duplicate))
            except (ValueError, TypeError):
                duplicate_video = None
            if (share_id and duplicate_video and duplicate_video.share_id == share_id
                    and duplicate_video.visibility == VideoVisibility.PRIVATE
                    and not duplicate_video.share_revoked and duplicate_video.trashed_at is None
                    and duplicate_video.status == VideoStatus.READY and duplicate_video.active_generation_id):
                result = _import_result(duplicate_video.id, duplicate_video.active_generation_id, inspection,
                                        share_id=share_id, idempotent=True)
                result["title"] = duplicate_video.title
                if dry_run:
                    result["dry_run"] = True
                else:
                    return JSONResponse(status_code=200, content=result)
                return result
            raise HTTPException(status_code=409, detail=f"This media content is already imported as video {duplicate}")
        if share_id:
            collision = await db.scalar(select(Video).where(Video.share_id == share_id).with_for_update())
            if collision:
                raise HTTPException(status_code=409, detail="share_id is already assigned to another video")
        if dry_run:
            return {
                "valid": True, "dry_run": True, "would_create": True,
                "title": inspection.title, "media_type": inspection.media_type,
                "visibility": "PRIVATE", "share_id": share_id,
                "duration_seconds": inspection.duration, "width": inspection.width,
                "height": inspection.height, "size_bytes": inspection.size_bytes,
                "sha256": inspection.sha256, "transcoding": False,
            }
        publish_import(extracted, target)
        published = True

        media_prefix = f"videos/{video_id}/generations/{generation_id}/"
        video = Video(
            id=video_id,
            user_id=current_user.id,
            title=inspection.title,
            status=VideoStatus.READY,
            source_filename=Path(inspection.entrypoint).name,
            source_size_bytes=inspection.size_bytes,
            source_duration_seconds=inspection.duration,
            source_resolution_width=inspection.width,
            source_resolution_height=inspection.height,
            source_codec=inspection.video_codec,
            source_path=media_prefix + inspection.entrypoint,
            visibility=VideoVisibility.PRIVATE,
            share_id=share_id,
            share_revoked=False,
            active_generation_id=generation_id,
            master_playlist_path=media_prefix + inspection.entrypoint if inspection.media_type == "hls" else None,
            transcode_progress_percent=100.0,
            is_test_content=False,
        )
        db.add(video)
        if inspection.media_type == "hls":
            used_labels: set[str] = set()
            for index, variant in enumerate(inspection.variants, start=1):
                height = int(variant["height"])
                label = f"{height}p"
                if label in used_labels:
                    label = f"{height}p-{index}"[:16]
                used_labels.add(label)
                full_playlist = media_prefix + variant["playlist"]
                db.add(VideoRendition(
                    id=uuid.uuid4(), video_id=video_id, quality_label=label,
                    resolution_width=int(variant["width"]), resolution_height=height,
                    is_vip_only=False,
                    video_codec="h264" if "avc1" in variant.get("codecs", "").lower() else "unknown",
                    audio_codec="aac" if "mp4a" in variant.get("codecs", "").lower() else None,
                    video_bitrate=int(variant["bandwidth"]), playlist_path=full_playlist,
                    size_bytes=inspection.size_bytes, is_ready=True,
                ))
        else:
            db.add(VideoRendition(
                id=uuid.uuid4(), video_id=video_id, quality_label="source",
                resolution_width=inspection.width, resolution_height=inspection.height,
                is_vip_only=False, video_codec=inspection.video_codec,
                audio_codec=inspection.audio_codec, playlist_path=media_prefix + inspection.entrypoint,
                size_bytes=inspection.size_bytes, is_ready=True,
            ))
        await record_audit_log(
            db=db, action="VIDEO_DIRECT_IMPORTED", user_id=current_user.id,
            target_entity="video", target_id=str(video_id), request=request,
            metadata_json={
                "media_type": inspection.media_type,
                "source_sha256": inspection.sha256,
                "source_size_bytes": inspection.size_bytes,
                "manifest_format": manifest["format"],
                "transcoding": False,
                "share_id": share_id,
            },
        )
        await db.commit()
        return _import_result(video_id, generation_id, inspection, share_id=share_id)
    except DirectImportError as exc:
        await db.rollback()
        if published:
            shutil.rmtree(target.parent.parent, ignore_errors=True)
        shutil.rmtree(extracted, ignore_errors=True)
        raise
    except IntegrityError as exc:
        await db.rollback()
        if published:
            shutil.rmtree(target.parent.parent, ignore_errors=True)
        raise HTTPException(status_code=409, detail="Import conflicts with an existing video or rendition") from exc
    except Exception:
        await db.rollback()
        if published:
            shutil.rmtree(target.parent.parent, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


def _resumable_root() -> Path:
    root = Path(settings.STORAGE_TEMP_PATH) / "direct-import-resumable"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise HTTPException(503, "Resumable storage path is not safe")
    os.chmod(root, 0o700)
    return root


@router.post("/direct-import/resumable")
async def begin_resumable_import(
    payload: dict[str, Any] = Body(...),
    current_user: User = Depends(require_owner_or_admin),
) -> dict[str, Any]:
    """Create an owner-bound, expiring upload for an already prepared ZIP package."""
    if not settings.ENABLE_DIRECT_IMPORT:
        raise HTTPException(503, "Direct import is disabled")
    filename, size = payload.get("filename"), payload.get("size")
    share_id = _validate_requested_share_id(payload.get("share_id"))
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise HTTPException(422, "dry_run must be a boolean")
    suffix = Path(filename).suffix.lower() if isinstance(filename, str) else ""
    folder_hls = payload.get("folder_hls") is True
    if (not isinstance(filename, str) or len(filename) > 255 or suffix not in (".zip", ".mp4", ".mkv")
            or any(ord(char) < 32 for char in filename) or "/" in filename or "\\" in filename):
        raise HTTPException(415, "Upload a prepared .zip package, browser-compatible .mp4, or storage-only .mkv")
    if folder_hls and suffix != ".zip":
        raise HTTPException(415, "Browser HLS folder uploads must use ZIP transport")
    if not isinstance(size, int) or size <= 0 or size > settings.DIRECT_IMPORT_MAX_BYTES:
        raise HTTPException(413, "Package size is outside configured limits")
    root = _resumable_root()
    now = datetime.now(timezone.utc)
    with (root / "admission.lock").open("a+b") as admission:
        portalocker.lock(admission, portalocker.LOCK_EX)
        reserved = 0
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            try:
                old = json.loads((child / "session.json").read_text(encoding="utf-8"))
                if datetime.fromisoformat(old["expires_at"]) <= now:
                    shutil.rmtree(child, ignore_errors=True)
                    continue
                reserved += max(0, int(old["reservation_bytes"]) - (child / "package.part").stat().st_size)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        reservation = size * (4 if suffix == ".mp4" else 3) if suffix in (".mp4", ".zip") else size
        if shutil.disk_usage(root).free < reserved + reservation + settings.STORAGE_SAFETY_MARGIN_BYTES:
            raise HTTPException(507, "Insufficient free space while preserving storage reserve")
        upload_id = uuid.uuid4().hex
        session_dir = root / upload_id
        session_dir.mkdir(mode=0o700)
        (session_dir / "package.part").touch(mode=0o600)
        metadata = {
            "owner_id": str(current_user.id), "filename": Path(filename).name,
            "size": size, "kind": "hlsfolder" if folder_hls else suffix[1:], "reservation_bytes": reservation,
            "share_id": share_id, "dry_run": dry_run,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        temp_meta = session_dir / "session.json.tmp"
        temp_meta.write_text(json.dumps(metadata), encoding="utf-8")
        os.chmod(temp_meta, 0o600)
        temp_meta.replace(session_dir / "session.json")
    return {"upload_id": upload_id, "offset": 0, "chunk_size": 8 * 1024 * 1024, "expires_in_seconds": 86400}


async def _owned_upload(upload_id: str, current_user: User) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise HTTPException(404, "Upload session not found")
    session_dir = _resumable_root() / upload_id
    if session_dir.is_symlink() or not session_dir.is_dir() or (session_dir / "session.json").is_symlink() or (session_dir / "package.part").is_symlink():
        raise HTTPException(404, "Upload session not found")
    try:
        metadata = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(404, "Upload session not found")
    if metadata.get("owner_id") != str(current_user.id):
        raise HTTPException(404, "Upload session not found")
    if datetime.fromisoformat(metadata["expires_at"]) <= datetime.now(timezone.utc):
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(410, "Upload session expired")
    return session_dir, metadata


@router.get("/direct-import/resumable/{upload_id}")
async def resumable_import_status(
    upload_id: str, current_user: User = Depends(require_owner_or_admin),
) -> dict[str, Any]:
    session_dir, metadata = await _owned_upload(upload_id, current_user)
    return {"upload_id": upload_id, "offset": (session_dir / "package.part").stat().st_size,
            "size": metadata["size"], "expires_at": metadata["expires_at"]}


@router.delete("/direct-import/resumable/{upload_id}", status_code=204, response_class=Response)
async def cancel_resumable_import(
    upload_id: str, current_user: User = Depends(require_owner_or_admin),
) -> Response:
    session_dir, _ = await _owned_upload(upload_id, current_user)
    with (_resumable_root() / f"{upload_id}.lock").open("a+b") as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        shutil.rmtree(session_dir)
    return Response(status_code=204)


@router.put("/direct-import/resumable/{upload_id}")
async def append_resumable_import_chunk(
    upload_id: str, request: Request, offset: int,
    current_user: User = Depends(require_owner_or_admin),
) -> dict[str, int]:
    session_dir, metadata = await _owned_upload(upload_id, current_user)
    chunk = bytearray()
    async for block in request.stream():
        if len(chunk) + len(block) > 8 * 1024 * 1024:
            raise HTTPException(413, "Chunk must be between 1 byte and 8 MiB")
        chunk.extend(block)
    if not chunk:
        raise HTTPException(413, "Chunk must be between 1 byte and 8 MiB")
    data_path = session_dir / "package.part"
    lock_path = _resumable_root() / f"{upload_id}.lock"
    with lock_path.open("a+b") as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        actual = data_path.stat().st_size
        if offset != actual:
            raise HTTPException(409, f"Offset mismatch; expected {actual}")
        if actual + len(chunk) > metadata["size"]:
            raise HTTPException(413, "Chunk exceeds declared package size")
        with data_path.open("ab") as output:
            output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        return {"offset": actual + len(chunk)}


@router.post("/direct-import/resumable/{upload_id}/finalize", status_code=201)
async def finalize_resumable_import(
    upload_id: str, request: Request, current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    session_dir, metadata = await _owned_upload(upload_id, current_user)
    uploaded_path = session_dir / "package.part"
    if uploaded_path.stat().st_size != metadata["size"]:
        raise HTTPException(409, "Upload is incomplete")
    try:
        kind = metadata.get("kind", "zip")
        if kind == "zip":
            result = await _register_archive(uploaded_path, request, current_user, db,
                                             share_id=metadata.get("share_id"), dry_run=metadata.get("dry_run", False))
        elif kind == "hlsfolder":
            normalized = _normalize_hls_folder_archive(uploaded_path, session_dir, metadata["filename"])
            try:
                result = await _register_archive(normalized, request, current_user, db,
                                                 share_id=metadata.get("share_id"), dry_run=metadata.get("dry_run", False))
            except Exception:
                normalized.unlink(missing_ok=True)
                raise
        elif kind == "mp4":
            archive_path = _wrap_native_mp4(uploaded_path, session_dir, metadata["filename"])
            result = await _register_archive(archive_path, request, current_user, db,
                                             share_id=metadata.get("share_id"), dry_run=metadata.get("dry_run", False))
        elif kind == "mkv":
            result = await _register_mkv(uploaded_path, request, current_user, db, metadata["filename"])
        else:
            raise HTTPException(415, "Unsupported ready upload type")
        shutil.rmtree(session_dir, ignore_errors=True)
        return result
    except DirectImportError as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise


def _wrap_native_mp4(source: Path, session_dir: Path, filename: str) -> Path:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    relative = "ready.mp4"
    manifest = {"format": "abv-direct-import-v1", "media_type": "mp4",
                "title": Path(filename).stem[:255] or "Ready MP4", "entrypoint": relative,
                "files": [{"path": relative, "size": size, "sha256": digest.hexdigest()}]}
    archive = session_dir / "prepared-package.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        zf.write(source, relative)
    return archive


def _hash_import_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _normalize_hls_folder_archive(source: Path, session_dir: Path, title: str) -> Path:
    """Safely turn browser-selected HLS folder ZIP bytes into our checksummed package."""
    unpacked = session_dir / "folder-unpacked"
    normalized = session_dir / "folder-verified.zip"
    if unpacked.exists() or normalized.exists():
        raise DirectImportError("HLS folder normalization workspace already exists")
    unpacked.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > settings.MAX_PACKAGE_FILES:
                raise DirectImportError("HLS folder has no files or exceeds the file-count limit")
            total = 0
            seen: set[str] = set()
            for info in infos:
                raw_name = info.filename.rstrip("/") if info.is_dir() else info.filename
                if not raw_name:
                    continue
                rel = str(_relative_path(raw_name))
                if rel.casefold() == "manifest.json":
                    raise DirectImportError("HLS folder must not provide its own manifest")
                if rel.casefold() in seen:
                    raise DirectImportError("HLS folder contains duplicate paths")
                seen.add(rel.casefold())
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise DirectImportError("Symbolic links are not allowed in HLS folder uploads")
                target = unpacked / Path(*PurePosixPath(rel).parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if target.suffix.lower() not in _SERVER_IMPORT_EXTENSIONS:
                    raise DirectImportError(f"Unsupported HLS asset extension: {target.suffix or '(none)'}")
                total += info.file_size
                if info.file_size <= 0 or info.file_size > settings.MAX_SINGLE_FILE_BYTES or total > settings.DIRECT_IMPORT_MAX_BYTES:
                    raise DirectImportError("HLS folder exceeds configured file or total-size limits")
                if info.compress_size and info.file_size / info.compress_size > 150:
                    raise DirectImportError("HLS folder archive compression ratio rejected")
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info, "r") as reader, target.open("xb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size or written > settings.DIRECT_IMPORT_MAX_BYTES:
                            raise DirectImportError("HLS folder member expanded beyond declared size")
                        writer.write(chunk)
                if written != info.file_size:
                    raise DirectImportError("HLS folder member size mismatch")
        if not (unpacked / "master.m3u8").is_file():
            raise DirectImportError("Selected HLS folder must contain master.m3u8 at its root")
        paths = sorted(path for path in unpacked.rglob("*") if path.is_file())
        if not paths or len(paths) > settings.MAX_PACKAGE_FILES:
            raise DirectImportError("HLS folder has no media or exceeds the file-count limit")
        records = []
        for path in paths:
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(unpacked):
                raise DirectImportError("Symbolic links or paths outside the HLS folder are not allowed")
            size, digest = _hash_import_file(path)
            records.append({"path":path.relative_to(unpacked).as_posix(), "size":size, "sha256":digest})
        manifest = {"format":"abv-direct-import-v1", "media_type":"hls", "title":Path(title).stem[:255] or "HLS folder", "entrypoint":"master.m3u8", "files":records}
        with zipfile.ZipFile(normalized, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as package:
            package.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
            for record in records:
                path = unpacked / Path(*PurePosixPath(record["path"]).parts)
                package.write(path, record["path"])
        return normalized
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise DirectImportError("Invalid HLS folder ZIP archive") from exc
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)


async def _register_mkv(source: Path, request: Request, current_user: User, db: AsyncSession, filename: str) -> dict[str, Any]:
    """Store MKV privately for authenticated download; never expose it as a browser-playable source."""
    if source.stat().st_size <= 0 or source.stat().st_size > settings.DIRECT_IMPORT_MAX_BYTES:
        raise HTTPException(413, "MKV size is outside configured limits")
    with source.open("rb") as stream:
        if stream.read(4) != b"\x1a\x45\xdf\xa3":
            raise HTTPException(422, "File does not have a Matroska EBML header")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    sha = digest.hexdigest()
    duplicate = await db.scalar(select(AuditLog.target_id).where(
        AuditLog.action == "VIDEO_READY_FILE_STORED", AuditLog.target_entity == "video",
        AuditLog.metadata_json["source_sha256"].astext == sha,
    ).limit(1))
    if duplicate:
        raise HTTPException(409, f"This MKV is already stored as video {duplicate}")
    video_id, generation_id = uuid.uuid4(), uuid.uuid4()
    safe_name = "download.mkv"
    media_prefix = f"videos/{video_id}/generations/{generation_id}/"
    target = Path(settings.STORAGE_VIDEOS_PATH) / str(video_id) / "generations" / str(generation_id) / safe_name
    target.parent.mkdir(parents=True, exist_ok=False)
    os.chmod(source, 0o640)
    os.replace(source, target)
    video = Video(
        id=video_id, user_id=current_user.id, title=Path(filename).stem[:255] or "MKV archive",
        status=VideoStatus.READY, source_filename=Path(filename).name, source_size_bytes=target.stat().st_size,
        source_path=media_prefix + safe_name, visibility=VideoVisibility.PRIVATE, share_id=None,
        share_revoked=False, active_generation_id=generation_id, transcode_progress_percent=100.0,
        is_test_content=False,
    )
    db.add(video)
    db.add(VideoRendition(
        id=uuid.uuid4(), video_id=video_id, quality_label="download", resolution_width=0,
        resolution_height=0, is_vip_only=False, video_codec="unknown", audio_codec=None,
        playlist_path=media_prefix + safe_name, size_bytes=target.stat().st_size, is_ready=True,
    ))
    try:
        await record_audit_log(db=db, action="VIDEO_READY_FILE_STORED", user_id=current_user.id,
            target_entity="video", target_id=str(video_id), request=request,
            metadata_json={"media_type":"mkv", "source_sha256":sha, "source_size_bytes":target.stat().st_size, "transcoding":False})
        await db.commit()
    except Exception:
        await db.rollback()
        shutil.rmtree(target.parent.parent.parent, ignore_errors=True)
        raise
    return {"id":str(video_id), "generation_id":str(generation_id), "title":video.title,
        "media_type":"mkv", "visibility":"PRIVATE", "status":"READY", "transcoding":False,
        "size_bytes":target.stat().st_size, "sha256":sha, "download_path":f"/api/v1/admin/videos/{video_id}/ready-file",
        "playback_note":"MKV playback is browser-dependent; download or upload compatible HLS/MP4."}


@router.get("/{video_id}/ready-file")
async def download_ready_file(
    video_id: uuid.UUID, current_user: User = Depends(require_owner_or_admin), db: AsyncSession = Depends(get_db),
) -> FileResponse:
    video = await db.scalar(select(Video).where(Video.id == video_id, Video.status == VideoStatus.READY))
    if not video or (video.user_id != current_user.id and current_user.role not in (UserRole.OWNER, UserRole.ADMIN)):
        raise HTTPException(404, "Ready file not found")
    if not video.source_path.lower().endswith(".mkv"):
        raise HTTPException(404, "This video does not have an MKV download")
    base = Path(settings.STORAGE_VIDEOS_PATH).resolve(strict=True)
    logical_path = PurePosixPath(video.source_path)
    if not logical_path.parts or logical_path.parts[0] != "videos":
        raise HTTPException(404, "Ready file not found")
    path = (base / Path(*logical_path.parts[1:])).resolve(strict=True)
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, "Ready file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=Path(video.source_filename).name,
                        headers={"Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})
