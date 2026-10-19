"""Background RQ Worker Job for Pre-Encoded Studio Package Verification and Promotion."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

from sqlalchemy import select, and_, delete

from app.core.config import settings
from app.core.logging import logger
from app.core.database import SyncSessionLocal
from app.models.video import (
    Video,
    VideoStatus,
    TranscodeJob,
    TranscodeJobStatus,
    VideoRendition,
    VideoSubtitle,
    VideoFontAsset,
)
from app.models.package import (
    PackageUploadSession,
    PackageSessionStatus,
    PackageFile,
    PackageFileStatus,
)
from app.services.subtitles import SubtitleService
from app.services.package_service import PackageStorageCleanup
from app.services.transcoder import derive_rfc6381_codecs, generate_master_playlist


def verify_and_promote_package(session_id: str) -> None:
    """
    Executes independent server-side verification and atomic generation promotion for a Studio package.
    Runs inside the background RQ worker process.
    """
    logger.info(f"=== Starting Package Verification Worker for session {session_id} ===")
    s_uuid = uuid.UUID(session_id)

    with SyncSessionLocal() as db:
        # 1. Fetch Session and Files
        stmt = select(PackageUploadSession).where(PackageUploadSession.id == s_uuid)
        session = db.execute(stmt).scalar_one_or_none()
        if not session:
            logger.error(f"Package session {session_id} not found in database.")
            return

        session.status = PackageSessionStatus.VERIFYING.value
        session.verification_started_at = datetime.now(timezone.utc)
        db.commit()

        staging_dir = Path(session.staging_path).resolve()
        if not staging_dir.exists():
            error_message = f"Staging directory not found: {staging_dir}"
            session = _fail_package_verification(db, s_uuid, error_message)
            PackageStorageCleanup.remove_terminal_staging(session)
            logger.error(error_message)
            return

        files_stmt = select(PackageFile).where(PackageFile.package_session_id == s_uuid)
        pkg_files = db.execute(files_stmt).scalars().all()
        pkg_files_map = {f.relative_path: f for f in pkg_files}

        try:
            # 2. Checksum and Integrity Verification of All Files
            logger.info(f"Verifying {len(pkg_files)} declared files in {staging_dir}...")
            for f in pkg_files:
                f_path = staging_dir / f.relative_path
                if not f_path.exists():
                    raise ValueError(f"Declared file missing on disk: {f.relative_path}")

                if f_path.stat().st_size != f.expected_size:
                    raise ValueError(
                        f"Size mismatch for {f.relative_path}: expected {f.expected_size}, got {f_path.stat().st_size}"
                    )

                # Incremental SHA-256 check
                hasher = hashlib.sha256()
                with open(f_path, "rb") as fp:
                    while chunk := fp.read(1048576):  # 1MB chunks
                        hasher.update(chunk)
                computed_hash = hasher.hexdigest().lower()
                if computed_hash != f.expected_sha256.lower():
                    raise ValueError(
                        f"SHA-256 hash mismatch for {f.relative_path}: expected {f.expected_sha256}, got {computed_hash}"
                    )

            # 3. Deep Playlist Security & URI Validation
            logger.info("Parsing and validating HLS playlist URIs for safety...")
            m3u8_files = [f for f in pkg_files if f.relative_path.endswith(".m3u8")]
            for m in m3u8_files:
                m_path = staging_dir / m.relative_path
                m_parent_rel = Path(m.relative_path).parent

                with open(m_path, "r", encoding="utf-8", errors="ignore") as fp:
                    for line_num, line in enumerate(fp, start=1):
                        line = line.strip()
                        if not line:
                            continue

                        # Extract URI targets
                        target_uri = None
                        if not line.startswith("#"):
                            target_uri = line
                        elif line.startswith("#EXT-X-STREAM-INF:") or line.startswith("#EXT-X-MEDIA:"):
                            uri_match = re.search(r'URI="([^"]+)"', line)
                            if uri_match:
                                target_uri = uri_match.group(1)

                        if target_uri:
                            # Security checks: Reject external and dangerous URI schemes
                            lower_uri = target_uri.lower()
                            if (
                                lower_uri.startswith("http://")
                                or lower_uri.startswith("https://")
                                or lower_uri.startswith("//")
                                or lower_uri.startswith("file:")
                                or lower_uri.startswith("data:")
                                or lower_uri.startswith("javascript:")
                                or target_uri.startswith("/")
                                or "\\" in target_uri
                                or ".." in target_uri
                            ):
                                raise ValueError(
                                    f"Illegal or dangerous URI '{target_uri}' in {m.relative_path} (line {line_num})"
                                )

                            # Resolve normalized path relative to playlist
                            norm_target = (m_parent_rel / target_uri).as_posix()
                            norm_target = re.sub(r"^\./", "", norm_target)
                            if norm_target not in pkg_files_map:
                                raise ValueError(
                                    f"Playlist {m.relative_path} references undeclared file '{norm_target}'"
                                )
                            if not (staging_dir / norm_target).exists():
                                raise ValueError(
                                    f"Playlist {m.relative_path} references non-existent file '{norm_target}'"
                                )

            # 4. Probe Renditions and Derive Canonical Codecs via ffprobe
            manifest = session.manifest_data
            probed_rendition_configs = []
            logger.info("Probing rendition video streams with ffprobe...")

            for r in manifest.get("renditions", []):
                q_label = r["quality_label"]
                pl_path = staging_dir / r["playlist_path"]
                pl_parent = Path(r["playlist_path"]).parent

                # Locate first .ts segment from variant playlist
                first_seg_path = None
                with open(pl_path, "r", encoding="utf-8") as fp:
                    for line in fp:
                        line = line.strip()
                        if line and not line.startswith("#") and line.endswith(".ts"):
                            first_seg_path = staging_dir / pl_parent / line
                            break

                if not first_seg_path or not first_seg_path.exists():
                    raise ValueError(f"Could not find valid .ts segment for rendition {q_label}")

                # Probe true codecs using server derive_rfc6381_codecs
                true_codecs = derive_rfc6381_codecs(first_seg_path, has_audio=True)

                # Probe actual resolution via ffprobe
                probe_cmd = [
                    "ffprobe",
                    "-v", "quiet",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-print_format", "json",
                    str(first_seg_path),
                ]
                res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                probed_w = r["width"]
                probed_h = r["height"]
                if res.returncode == 0:
                    probe_data = json.loads(res.stdout)
                    v_streams = probe_data.get("streams", [])
                    if v_streams:
                        probed_w = v_streams[0].get("width", probed_w)
                        probed_h = v_streams[0].get("height", probed_h)

                probed_rendition_configs.append(
                    {
                        "quality_label": q_label,
                        "width": probed_w,
                        "height": probed_h,
                        "video_bitrate_kbps": r["video_bitrate_bps"] // 1000,
                        "audio_bitrate_kbps": r.get("audio_bitrate_bps", 128000) // 1000,
                        "rfc_codecs": true_codecs,
                        "playlist_path": r["playlist_path"],
                        "segment_count": r["segment_count"],
                    }
                )

            # 5. Generate Server-Owned Canonical master.m3u8
            logger.info("Generating canonical server-owned master.m3u8...")
            canonical_master_path = staging_dir / "master.m3u8"
            generate_master_playlist(
                output_path=canonical_master_path,
                rendition_configs=probed_rendition_configs,
            )

            # 6. Process Fonts & Subtitles
            fonts_to_register = []
            font_id_map = {}
            for f in manifest.get("fonts", []):
                f_path = staging_dir / f["path"]
                if not f_path.exists():
                    raise ValueError(f"Declared font file missing: {f['path']}")
                f_id = uuid.uuid4()
                f_norm_path = Path(f["path"]).as_posix()
                f_ext = Path(f["path"]).suffix.lower()
                mime = "font/otf" if f_ext == ".otf" else "font/ttf"
                font_rec = VideoFontAsset(
                    id=f_id,
                    video_id=session.video_id,
                    generation_id=session.target_generation_id,
                    family_name=f["family_name"],
                    filename=f.get("filename", Path(f["path"]).name),
                    relative_path=f_norm_path,
                    sha256=f["sha256"].lower(),
                    size_bytes=f["size_bytes"],
                    mime_type=mime,
                )
                fonts_to_register.append(font_rec)
                font_id_map[f_norm_path] = str(f_id)
                font_id_map[f["family_name"]] = str(f_id)
                font_id_map[f.get("filename", Path(f["path"]).name)] = str(f_id)

            subtitles_to_register = []
            for s in manifest.get("subtitles", []):
                s_path = staging_dir / s["path"]
                if not s_path.exists():
                    raise ValueError(f"Declared subtitle file missing: {s['path']}")

                sub_id = uuid.uuid4()
                s_norm_path = Path(s["path"]).as_posix()
                ext = Path(s["path"]).suffix.lower().lstrip(".")
                fmt = (s.get("format") or ext).lower()

                # Map associated font IDs
                associated_font_ids = []
                for f_ref in s.get("fonts", []):
                    if f_ref in font_id_map:
                        fid = font_id_map[f_ref]
                        if fid not in associated_font_ids:
                            associated_font_ids.append(fid)
                if not associated_font_ids and fonts_to_register and fmt in ("ass", "ssa"):
                    associated_font_ids = [str(fr.id) for fr in fonts_to_register]

                if fmt in ("ass", "ssa"):
                    # TRUE ASS Softsub: Immutable generation asset, preserve original ASS, no WebVTT conversion
                    subtitles_to_register.append(
                        VideoSubtitle(
                            id=sub_id,
                            video_id=session.video_id,
                            generation_id=session.target_generation_id,
                            language_code=s["language_code"],
                            language_name=s["language_name"],
                            label=s["label"],
                            is_default=s.get("is_default", False),
                            format=fmt,
                            playback_renderer="libass",
                            is_playable=True,
                            source_path=s_norm_path,
                            webvtt_path=None,
                            font_asset_ids=associated_font_ids,
                        )
                    )
                else:
                    # VTT or SRT: Process for HTML5 playback
                    sub_content = s_path.read_bytes()
                    sub_filename = Path(s["path"]).name
                    source_rel, vtt_rel, detected_fmt, is_playable = SubtitleService.store_subtitle(
                        video_id=session.video_id,
                        subtitle_id=sub_id,
                        filename=sub_filename,
                        content_bytes=sub_content,
                    )
                    subtitles_to_register.append(
                        VideoSubtitle(
                            id=sub_id,
                            video_id=session.video_id,
                            generation_id=session.target_generation_id,
                            language_code=s["language_code"],
                            language_name=s["language_name"],
                            label=s["label"],
                            is_default=s.get("is_default", False),
                            format=fmt,
                            playback_renderer="html5",
                            is_playable=is_playable,
                            source_path=s_norm_path,
                            webvtt_path=vtt_rel,
                            font_asset_ids=[],
                        )
                    )

            # 7. Safe Promotion to Final Generation Directory
            session.status = PackageSessionStatus.PROMOTING.value
            db.commit()

            final_gen_dir = (
                Path(settings.STORAGE_VIDEOS_PATH)
                / str(session.video_id)
                / "generations"
                / str(session.target_generation_id)
            ).resolve()

            final_gen_dir.parent.mkdir(parents=True, exist_ok=True)
            st_dev_staging = staging_dir.stat().st_dev
            st_dev_dest = final_gen_dir.parent.stat().st_dev

            logger.info(f"Promoting generation: st_dev staging={st_dev_staging}, dest={st_dev_dest}")

            if st_dev_staging == st_dev_dest:
                # Same filesystem: Atomic in-place directory rename
                os.replace(staging_dir, final_gen_dir)
            else:
                # Cross-filesystem promotion with destination-local temp directory
                dest_temp_dir = final_gen_dir.parent / f".tmp_{session.target_generation_id}"
                if dest_temp_dir.exists():
                    shutil.rmtree(dest_temp_dir)
                dest_temp_dir.mkdir(parents=True, exist_ok=True)

                for root, dirs, files in os.walk(staging_dir):
                    rel = Path(root).relative_to(staging_dir)
                    target_root = dest_temp_dir / rel
                    target_root.mkdir(parents=True, exist_ok=True)
                    for file in files:
                        src_f = Path(root) / file
                        dst_f = target_root / file
                        shutil.copy2(src_f, dst_f)
                        with open(dst_f, "rb") as f_check:
                            os.fsync(f_check.fileno())

                # Atomic rename on destination filesystem
                os.replace(dest_temp_dir, final_gen_dir)
                shutil.rmtree(staging_dir, ignore_errors=True)

            # 8. Database Transaction: Activate Generation
            video = db.execute(select(Video).where(Video.id == session.video_id)).scalar_one()

            # Clean prior renditions, fonts, and subtitles for idempotent promotion
            db.execute(delete(VideoRendition).where(VideoRendition.video_id == video.id))
            db.execute(delete(VideoFontAsset).where(VideoFontAsset.video_id == video.id))
            db.execute(delete(VideoSubtitle).where(VideoSubtitle.video_id == video.id))

            # Insert VideoRenditions
            for r in probed_rendition_configs:
                rendition = VideoRendition(
                    id=uuid.uuid4(),
                    video_id=video.id,
                    quality_label=r["quality_label"],
                    resolution_width=r["width"],
                    resolution_height=r["height"],
                    video_bitrate=r["video_bitrate_kbps"] * 1000,
                    audio_bitrate=r["audio_bitrate_kbps"] * 1000,
                    video_codec=r["rfc_codecs"].split(",")[0],
                    audio_codec=r["rfc_codecs"].split(",")[1] if "," in r["rfc_codecs"] else "mp4a.40.2",
                    playlist_path=f"generations/{session.target_generation_id}/{r['playlist_path']}",
                    is_ready=True,
                )
                db.add(rendition)

            # Insert Fonts & Subtitles
            for f_rec in fonts_to_register:
                db.add(f_rec)
            for sub in subtitles_to_register:
                db.add(sub)

            # Activate Video
            video.active_generation_id = session.target_generation_id
            video.status = VideoStatus.READY.value
            video.source_duration_seconds = manifest["video"]["duration_seconds"]
            video.master_playlist_path = f"generations/{session.target_generation_id}/master.m3u8"

            poster_src = final_gen_dir / "poster.webp"
            if poster_src.exists():
                video.poster_path = f"generations/{session.target_generation_id}/poster.webp"

            # Update Session Status
            session.status = PackageSessionStatus.READY.value
            db.commit()
            PackageStorageCleanup.remove_terminal_staging(session)

            logger.info(
                f"=== Successfully Verified & Promoted Package {session_id} for Video {video.id} (Gen: {session.target_generation_id}) ==="
            )

        except Exception as e:
            logger.error(f"Package verification failed for session {session_id}: {e}", exc_info=True)
            db.rollback()
            session = _fail_package_verification(db, s_uuid, str(e))
            PackageStorageCleanup.remove_terminal_staging(session)
            raise


ACTIVE_TRANSCODE_STATUSES = (
    TranscodeJobStatus.QUEUED,
    TranscodeJobStatus.PROBING,
    TranscodeJobStatus.PROCESSING,
    TranscodeJobStatus.VERIFYING,
)


LIVE_PACKAGE_STATUSES = (
    PackageSessionStatus.INITIALIZED.value,
    PackageSessionStatus.UPLOADING.value,
    PackageSessionStatus.UPLOADED.value,
    PackageSessionStatus.QUEUED_FOR_VERIFICATION.value,
    PackageSessionStatus.VERIFYING.value,
    PackageSessionStatus.PROMOTING.value,
)


def _fail_package_verification(db, session_id: uuid.UUID, error_message: str) -> PackageUploadSession:
    """Atomically fail validation and terminalize only an unpublished, idle package video."""
    session = db.execute(
        select(PackageUploadSession).where(PackageUploadSession.id == session_id)
    ).scalar_one()
    session.status = PackageSessionStatus.FAILED.value
    session.error_message = error_message

    video = db.execute(
        select(Video).where(Video.id == session.video_id).with_for_update()
    ).scalar_one_or_none()
    if (
        video
        and video.status == VideoStatus.PROCESSING
        and video.active_generation_id is None
    ):
        active_job_id = db.execute(
            select(TranscodeJob.id)
            .where(
                TranscodeJob.video_id == video.id,
                TranscodeJob.status.in_(ACTIVE_TRANSCODE_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none()
        other_live_session_id = db.execute(
            select(PackageUploadSession.id)
            .where(
                PackageUploadSession.video_id == video.id,
                PackageUploadSession.id != session.id,
                PackageUploadSession.status.in_(LIVE_PACKAGE_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none()
        if active_job_id is None and other_live_session_id is None:
            video.status = VideoStatus.FAILED

    # Package failure and its eligible Video transition share this transaction.
    db.commit()
    return session
