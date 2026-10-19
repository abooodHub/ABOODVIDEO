"""Background Video Transcoding Worker with Heartbeat & Capability Self-Check (Phase 3)."""
import os
import sys
import time
import uuid
import socket
import signal
import shutil
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

import redis
from rq import Worker, Queue, Connection, get_current_job
from sqlalchemy import select, and_, delete

from app.core.config import settings
from app.core.logging import logger
from app.core.database import SyncSessionLocal
from app.models.video import Video, VideoStatus, VideoRendition, TranscodeJob, TranscodeJobStatus
from app.services.transcoder import (
    TranscodeError,
    InsufficientDiskSpaceError,
    TranscodeCancelledError,
    probe_source,
    select_quality_ladder,
    calculate_peak_disk_requirement,
    check_disk_headroom,
    derive_rfc6381_codecs,
    generate_poster,
    generate_master_playlist,
    verify_transcoded_output,
    execute_transcode_rendition,
)
from app.services.transcode_plan import build_legacy_transcode_configuration_snapshot, validate_transcode_configuration_snapshot
from app.services.rendition_checkpoint import (
    build_rendition_fingerprint,
    compute_source_sha256,
    copy_rendition_artifacts,
    find_reusable_renditions,
    write_checkpoint,
)


def check_worker_capabilities() -> None:
    """Strict capability self-check on worker startup."""
    logger.info("Verifying worker transcoding capabilities...")

    # 1. Check ffmpeg
    try:
        proc = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg binary returned non-zero exit code")
    except Exception as e:
        sys.stderr.write(f"FATAL: ffmpeg capability check failed: {e}\n")
        sys.exit(1)

    # 2. Check ffprobe
    try:
        proc = subprocess.run(["ffprobe", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError("ffprobe binary returned non-zero exit code")
    except Exception as e:
        sys.stderr.write(f"FATAL: ffprobe capability check failed: {e}\n")
        sys.exit(1)

    # 3. Check encoders
    try:
        proc = subprocess.run(["ffmpeg", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        encoders = proc.stdout
        if "libx264" not in encoders:
            raise RuntimeError("libx264 H.264 encoder is missing in FFmpeg build")
        if " aac" not in encoders:
            raise RuntimeError("aac audio encoder is missing in FFmpeg build")
        if "libwebp" not in encoders and "webp" not in encoders:
            logger.warning("libwebp encoder not found in FFmpeg; fallback image format will be used")
    except Exception as e:
        sys.stderr.write(f"FATAL: encoder capability check failed: {e}\n")
        sys.exit(1)

    logger.info("Worker transcoding capabilities verified successfully (ffmpeg, ffprobe, libx264, aac).")


class WorkerHeartbeat:
    """
    Two-key Redis Heartbeat Model:
    worker:heartbeat:{worker_id} - TTL 15s (current liveness)
    worker:last_seen:{worker_id} - TTL 3600s (last recorded timestamp)
    """
    def __init__(self, redis_conn: redis.Redis, worker_id: str):
        self.redis_conn = redis_conn
        self.worker_id = worker_id
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"heartbeat-{self.worker_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = time.time()
                pipe = self.redis_conn.pipeline()
                pipe.set(f"worker:heartbeat:{self.worker_id}", "alive", ex=15)
                pipe.set(f"worker:last_seen:{self.worker_id}", str(now), ex=3600)
                pipe.sadd("worker:registered_workers", self.worker_id)
                pipe.execute()
            except Exception as e:
                logger.warning(f"Error publishing worker heartbeat: {e}")
            self._stop_event.wait(5.0)


def get_worker_status(redis_conn: redis.Redis, worker_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Evaluate worker health:
    ONLINE: last_seen <= 15s AND live heartbeat exists
    STALE: 15s < last_seen <= 60s
    OFFLINE: last_seen > 60s OR no valid worker record
    """
    now = time.time()
    registered = [w.decode("utf-8") if isinstance(w, bytes) else str(w)
                  for w in (redis_conn.smembers("worker:registered_workers") or [])]
    workers_to_check = [worker_id] if worker_id else registered
    if worker_id and worker_id not in registered and worker_id.startswith("worker-"):
        # Older jobs stored the forked child PID. Its heartbeat belongs to the
        # parent on the same host; a worker on another host is never evidence.
        host_prefix = worker_id.rsplit("-", 1)[0] + "-"
        related = [w for w in registered if w.startswith(host_prefix)]
        if related:
            workers_to_check = related

    if not workers_to_check:
        return {"status": "OFFLINE", "worker_id": None, "last_seen_seconds_ago": None}

    # Evaluate all registered workers, return highest liveness found
    best_status = "OFFLINE"
    best_worker = None
    min_age = 999999.0

    for w in workers_to_check:
        w_id = w.decode("utf-8") if isinstance(w, bytes) else str(w)
        last_seen_str = redis_conn.get(f"worker:last_seen:{w_id}")
        if not last_seen_str:
            continue
        try:
            last_seen = float(last_seen_str.decode("utf-8") if isinstance(last_seen_str, bytes) else last_seen_str)
            age = max(0.0, now - last_seen)
        except (ValueError, TypeError):
            continue

        heartbeat_exists = redis_conn.exists(f"worker:heartbeat:{w_id}") > 0

        if age <= 15.0 and heartbeat_exists:
            status = "ONLINE"
        elif age <= 60.0:
            status = "STALE"
        else:
            status = "OFFLINE"

        if status == "ONLINE":
            return {"status": "ONLINE", "worker_id": w_id, "last_seen_seconds_ago": round(age, 1)}
        elif status == "STALE" and best_status != "ONLINE":
            best_status = "STALE"
            best_worker = w_id
            min_age = min(min_age, age)
        elif best_status == "OFFLINE":
            best_worker = w_id
            min_age = min(min_age, age)

    return {
        "status": best_status,
        "worker_id": best_worker,
        "last_seen_seconds_ago": round(min_age, 1) if min_age < 999999.0 else None,
    }


# Backward-compatible alias for health endpoint
evaluate_worker_heartbeat_status = get_worker_status


def process_transcode_job(job_id_str: str) -> None:
    """Core transcoding pipeline job processor executed by RQ worker."""
    job_uuid = uuid.UUID(job_id_str)
    rq_job = get_current_job()
    # RQ executes in a forked child; its PID has no parent heartbeat.
    # Bind the job to the exact worker name that publishes that heartbeat.
    worker_id = rq_job.worker_name if rq_job and rq_job.worker_name else f"worker-{socket.gethostname()}-{os.getpid()}"

    with SyncSessionLocal() as db:
        job = db.execute(select(TranscodeJob).where(TranscodeJob.id == job_uuid)).scalar_one_or_none()
        if not job:
            logger.error(f"Transcode job {job_id_str} not found in database")
            return

        video = db.execute(select(Video).where(Video.id == job.video_id)).scalar_one_or_none()
        if not video:
            logger.error(f"Video {job.video_id} for transcode job {job_id_str} not found")
            job.status = TranscodeJobStatus.FAILED
            job.error_code = "VIDEO_NOT_FOUND"
            job.error_message = "Associated video record not found"
            db.commit()
            return

        if job.cancel_requested:
            job.status = TranscodeJobStatus.CANCELLED
            db.commit()
            return

        # Initialize job
        job.status = TranscodeJobStatus.PROBING
        job.started_at = datetime.now(timezone.utc)
        job.worker_id = worker_id
        job.progress_percent = 5.0
        video.status = VideoStatus.PROBING
        video.transcode_progress_percent = 5.0
        db.commit()

        temp_dir = Path(settings.STORAGE_TEMP_PATH) / "transcodes" / str(video.id) / str(job.id)

        try:
            # Resolve source video path
            source_path = None
            if video.source_path:
                p = Path(video.source_path)
                if p.is_absolute() and p.exists():
                    source_path = p
                else:
                    alt_p = Path(settings.STORAGE_BASE_PATH) / video.source_path
                    if alt_p.exists():
                        source_path = alt_p

            if not source_path or not source_path.exists():
                candidates = list(Path(settings.STORAGE_UPLOADS_PATH).glob(f"{video.id}.*"))
                if candidates:
                    source_path = candidates[0]

            if not source_path or not source_path.exists():
                raise TranscodeError("Source video file not found on disk", error_code="SOURCE_NOT_FOUND")

            # 1. Probing Stage (0.0% -> 5.0%)
            logger.info(f"[{job.id}] Probing source: {source_path}")
            probe_info = probe_source(source_path)

            video.source_duration_seconds = probe_info["duration"]
            video.source_resolution_width = probe_info["width"]
            video.source_resolution_height = probe_info["height"]
            video.source_bitrate = probe_info["bitrate"]
            video.source_codec = probe_info["video_codec"]
            db.commit()

            # 2. Ladder Selection
            snapshot = job.configuration_snapshot
            if snapshot is None:
                snapshot = build_legacy_transcode_configuration_snapshot()
                job.configuration_snapshot = snapshot
                db.commit()
            snapshot = validate_transcode_configuration_snapshot(snapshot)
            encoding_config = {**snapshot["encoder"], "segment_duration_seconds": snapshot["hls"]["segment_duration_seconds"]}
            ladder = select_quality_ladder(
                probe_info["width"], probe_info["height"], probe_info["bitrate"], snapshot["enabled_qualities"], snapshot["quality_presets"], allow_4k=snapshot["policy"]["allow_4k_transcoding"], fallback_presets=snapshot.get("source_fallback_presets"),
            )
            logger.info(f"[{job.id}] Quality ladder selected: {[r['quality_label'] for r in ladder]}")

            # 3. Peak Disk Reservation Check
            active_gen_dir = None
            if video.active_generation_id:
                active_gen_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(video.active_generation_id)

            peak_bytes = calculate_peak_disk_requirement(
                video.source_size_bytes,
                probe_info["duration"],
                ladder,
                probe_info["has_audio"],
                active_gen_dir,
            )
            check_disk_headroom(Path(settings.STORAGE_BASE_PATH), peak_bytes)

            # 4. Processing Stage (5.0% -> 90.0%)
            temp_dir.mkdir(parents=True, exist_ok=True)
            job.status = TranscodeJobStatus.PROCESSING
            video.status = VideoStatus.PROCESSING
            db.commit()

            # 4a. Rendition checkpoint recovery: reuse verified completed renditions
            # from interrupted previous generations instead of re-encoding them.
            # Partial or unverifiable renditions are always re-encoded.
            try:
                source_sha256 = compute_source_sha256(source_path)
            except Exception as e:
                logger.warning(f"[{job.id}] Could not fingerprint source ({e}); checkpoints disabled for this run")
                source_sha256 = None

            reusable_renditions = find_reusable_renditions(
                video.id, ladder, probe_info, source_sha256, exclude_generation=job.id,
            )
            if reusable_renditions:
                recovered_labels = sorted(reusable_renditions.keys())
                regenerate_labels = [r["quality_label"] for r in ladder if r["quality_label"] not in reusable_renditions]
                logger.info(
                    f"[{job.id}] Checkpoint recovery: reusing {len(recovered_labels)} verified renditions "
                    f"{recovered_labels}; regenerating {regenerate_labels or 'none'}"
                )
            else:
                regenerate_labels = [r["quality_label"] for r in ladder]

            rendition_fingerprints = {}
            if source_sha256:
                for r in ladder:
                    rendition_fingerprints[r["quality_label"]] = build_rendition_fingerprint(
                        source_sha256, probe_info, r, encoding_config=encoding_config,
                    )

            progress_state = {"current": 5.0}

            def check_cancelled() -> bool:
                with SyncSessionLocal() as check_db:
                    curr_job = check_db.execute(select(TranscodeJob.cancel_requested).where(TranscodeJob.id == job_uuid)).scalar_one_or_none()
                    return bool(curr_job)

            def progress_callback(pct: float, fps: Optional[float], speed: Optional[float], eta: Optional[int]) -> None:
                with SyncSessionLocal() as update_db:
                    update_db.execute(
                        select(TranscodeJob).where(TranscodeJob.id == job_uuid)
                    )
                    j = update_db.get(TranscodeJob, job_uuid)
                    v = update_db.get(Video, video.id)
                    if j and v:
                        j.progress_percent = pct
                        j.ffmpeg_fps = fps
                        j.ffmpeg_speed = speed
                        j.eta_seconds = eta
                        v.transcode_progress_percent = pct
                        update_db.commit()

            for idx, rendition in enumerate(ladder):
                if check_cancelled():
                    raise TranscodeCancelledError()

                job.current_rendition = rendition["quality_label"]
                db.commit()

                rendition_dir = temp_dir / rendition["quality_label"]

                weight_per_rendition = 85.0 / len(ladder)
                rendition_base = 5.0 + (idx * weight_per_rendition)

                reusable = reusable_renditions.get(rendition["quality_label"])
                if reusable:
                    logger.info(
                        f"[{job.id}] Reusing verified {rendition['quality_label']} rendition "
                        f"from generation {reusable['generation']}"
                        + (" (retroactive verification)" if reusable["verification"].get("retroactive") else "")
                    )
                    copy_rendition_artifacts(reusable["source_dir"], rendition_dir)
                    if rendition["quality_label"] in rendition_fingerprints:
                        write_checkpoint(rendition_dir, rendition_fingerprints[rendition["quality_label"]])
                    progress_state["current"] = min(89.9, rendition_base + weight_per_rendition)
                    if progress_callback:
                        progress_callback(progress_state["current"], None, None, None)
                    continue

                execute_transcode_rendition(
                    source_path=source_path,
                    rendition=rendition,
                    rendition_dir=rendition_dir,
                    fps=probe_info["fps"],
                    has_audio=probe_info["has_audio"],
                    duration=probe_info["duration"],
                    rendition_index=idx,
                    total_renditions=len(ladder),
                    progress_state=progress_state,
                    check_cancelled_fn=check_cancelled,
                    progress_callback=progress_callback,
                    encoding_config=encoding_config,
                )
                if rendition["quality_label"] in rendition_fingerprints:
                    try:
                        write_checkpoint(rendition_dir, rendition_fingerprints[rendition["quality_label"]])
                    except Exception as e:
                        logger.warning(f"[{job.id}] Could not write checkpoint for {rendition['quality_label']}: {e}")

            # 5. Poster Generation (90.0% -> 93.0%)
            if check_cancelled():
                raise TranscodeCancelledError()

            poster_path = temp_dir / "poster.webp"
            generate_poster(source_path, probe_info["duration"], poster_path)
            job.progress_percent = 93.0
            video.transcode_progress_percent = 93.0
            db.commit()

            # 6. Master Playlist Generation with Real RFC 6381 Codecs
            rendition_configs = []
            for r in ladder:
                seg_000 = temp_dir / r["quality_label"] / "seg_000.ts"
                rfc_codecs = derive_rfc6381_codecs(seg_000, probe_info["has_audio"])
                rc = dict(r)
                rc["rfc_codecs"] = rfc_codecs
                rendition_configs.append(rc)

            generate_master_playlist(rendition_configs, temp_dir / "master.m3u8")

            # 7. Verification Stage (93.0% -> 98.0%)
            if check_cancelled():
                raise TranscodeCancelledError()

            job.status = TranscodeJobStatus.VERIFYING
            video.status = VideoStatus.VERIFYING
            job.progress_percent = 95.0
            video.transcode_progress_percent = 95.0
            db.commit()

            verify_transcoded_output(temp_dir, ladder, probe_info["duration"])

            job.progress_percent = 98.0
            video.transcode_progress_percent = 98.0
            db.commit()

            # 8. Safe Generation-Based Publishing
            target_gen_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(job.id)
            target_gen_dir.parent.mkdir(parents=True, exist_ok=True)

            if target_gen_dir.exists():
                shutil.rmtree(target_gen_dir)

            shutil.move(str(temp_dir), str(target_gen_dir))

            # 9. Atomic DB Pointer Activation
            video.active_generation_id = job.id
            video.master_playlist_path = f"videos/{video.id}/generations/{job.id}/master.m3u8"
            video.poster_path = f"videos/{video.id}/generations/{job.id}/poster.webp"
            video.status = VideoStatus.READY
            video.transcode_progress_percent = 100.0

            job.status = TranscodeJobStatus.COMPLETED
            job.progress_percent = 100.0
            job.completed_at = datetime.now(timezone.utc)
            job.error_code = None
            job.error_message = None

            # The DB catalog must describe the newly active generation only.
            # A reprocess may intentionally disable a previously published quality.
            published_labels = [r["quality_label"] for r in rendition_configs]
            db.execute(delete(VideoRendition).where(
                VideoRendition.video_id == video.id,
                VideoRendition.quality_label.not_in(published_labels),
            ))

            # Upsert rendition records
            for r in rendition_configs:
                rendition_db = db.execute(
                    select(VideoRendition).where(
                        and_(
                            VideoRendition.video_id == video.id,
                            VideoRendition.quality_label == r["quality_label"],
                        )
                    )
                ).scalar_one_or_none()

                q_dir = target_gen_dir / r["quality_label"]
                total_bytes = sum(f.stat().st_size for f in q_dir.glob("*") if f.is_file())

                if not rendition_db:
                    rendition_db = VideoRendition(
                        video_id=video.id,
                        quality_label=r["quality_label"],
                        resolution_width=r["width"],
                        resolution_height=r["height"],
                        video_codec="h264",
                        audio_codec="aac" if probe_info["has_audio"] else None,
                        video_bitrate=r["video_bitrate_kbps"] * 1000,
                        audio_bitrate=(r["audio_bitrate_kbps"] * 1000) if probe_info["has_audio"] else None,
                        playlist_path=f"videos/{video.id}/generations/{job.id}/{r['quality_label']}/index.m3u8",
                        size_bytes=total_bytes,
                        is_ready=True,
                        is_vip_only=r.get("is_vip_only", False),
                    )
                    db.add(rendition_db)
                else:
                    rendition_db.resolution_width = r["width"]
                    rendition_db.resolution_height = r["height"]
                    rendition_db.video_codec = "h264"
                    rendition_db.audio_codec = "aac" if probe_info["has_audio"] else None
                    rendition_db.video_bitrate = r["video_bitrate_kbps"] * 1000
                    rendition_db.audio_bitrate = (r["audio_bitrate_kbps"] * 1000) if probe_info["has_audio"] else None
                    rendition_db.playlist_path = f"videos/{video.id}/generations/{job.id}/{r['quality_label']}/index.m3u8"
                    rendition_db.size_bytes = total_bytes
                    rendition_db.is_ready = True
                    rendition_db.is_vip_only = r.get("is_vip_only", False)

            db.commit()
            logger.info(f"[{job.id}] Transcoding successfully published generation {job.id} for video {video.id}")

            # Optional source deletion
            if settings.DELETE_SOURCE_AFTER_SUCCESS and source_path.exists():
                try:
                    source_path.unlink()
                    video.source_path = None
                    db.commit()
                except Exception as e:
                    logger.warning(f"Could not remove source video file: {e}")

        except TranscodeCancelledError:
            logger.warning(f"[{job.id}] Transcode cancelled for video {video.id}")
            job.status = TranscodeJobStatus.CANCELLED
            job.error_code = "TRANSCODE_CANCELLED"
            job.error_message = "Cancelled by user request"
            if not video.active_generation_id:
                video.status = VideoStatus.CANCELLED
            db.commit()
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

        except Exception as e:
            logger.exception(f"[{job.id}] Transcode failed: {e}")
            job.status = TranscodeJobStatus.FAILED
            job.error_code = getattr(e, "error_code", "TRANSCODE_FAILED")
            job.error_message = str(e)[:1000]
            if not video.active_generation_id:
                video.status = VideoStatus.FAILED
            db.commit()
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)


def run_worker():
    """Main worker entry point."""
    logger.info("=== [ABOODVIDEO Worker] Starting Background Transcode Worker ===")
    check_worker_capabilities()

    worker_id = f"worker-{socket.gethostname()}-{os.getpid()}"
    redis_conn = redis.from_url(settings.REDIS_QUEUE_URL)

    heartbeat = WorkerHeartbeat(redis_conn, worker_id)
    heartbeat.start()

    logger.info(f"Worker {worker_id} registered. Listening on queue 'transcode'...")

    def handle_shutdown(signum, frame):
        logger.info(f"Received signal {signum}, stopping heartbeat and shutting down...")
        heartbeat.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        with Connection(redis_conn):
            worker = Worker(["transcode"], name=worker_id)
            worker.work()
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    run_worker()
