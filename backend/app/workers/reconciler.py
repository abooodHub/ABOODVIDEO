"""Maintenance Reconciler: Recovers stalled QUEUED_FOR_VERIFICATION packages and abandoned transcode jobs."""
import argparse
import asyncio
import signal
import sys
import time
import warnings
from datetime import datetime, timezone, timedelta
from typing import Optional

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import FailedJobRegistry
from sqlalchemy import select, and_

from app.core.config import settings
from app.core.logging import logger
from app.core.database import get_db_context
from app.models.package import PackageUploadSession, PackageSessionStatus
from app.models.video import Video, VideoStatus, TranscodeJob, TranscodeJobStatus
from app.services.upload_service import UploadService
from app.services.package_service import PackageStorageCleanup
from app.workers.transcode_worker import get_worker_status
from rq.exceptions import NoSuchJobError
from redis.exceptions import RedisError

from sqlalchemy import select, and_, exists
from app.services.analytics_retention import prune_playback_analytics
from app.services.retention import cleanup_expired_user_sessions
TRANSCODE_ABANDONED_ERROR_CODE = "TRANSCODE_ABANDONED"
TRANSCODE_ABANDONED_MESSAGE = (
    "Processing worker/server interrupted before completion "
    "(worker lost or host restarted). Retry the transcode from the admin UI."
)

ACTIVE_JOB_STATUSES = (
    TranscodeJobStatus.QUEUED,
    TranscodeJobStatus.PROBING,
    TranscodeJobStatus.PROCESSING,
    TranscodeJobStatus.VERIFYING,
)


async def reconcile_stalled_package_jobs(threshold_minutes: int = 5) -> int:
    """
    Finds packages in QUEUED_FOR_VERIFICATION state that have not been started
    by a worker within the threshold window, verifies Redis queue status,
    and re-enqueues missing jobs using deterministic job IDs.
    Also handles packages stuck in VERIFYING or PROMOTING state beyond the threshold window,
    marking them as FAILED with a timeout message.
    Returns the count of recovered/handled packages.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    recovered_count = 0
    terminal_sessions = []

    async with get_db_context() as db:
        recovered_count += await UploadService.cleanup_expired_sessions(db)
        # Expiry applies only before verification has been queued. Active queue/
        # worker states are deliberately left to the positive-evidence checks below.
        expired_stmt = select(PackageUploadSession).where(
            PackageUploadSession.status.in_([
                PackageSessionStatus.INITIALIZED.value,
                PackageSessionStatus.UPLOADING.value,
                PackageSessionStatus.UPLOADED.value,
            ]),
            PackageUploadSession.expires_at < datetime.now(timezone.utc),
        )
        for session in (await db.execute(expired_stmt)).scalars().all():
            session.status = PackageSessionStatus.EXPIRED.value
            session.error_message = "Package upload session expired before verification."
            session.updated_at = datetime.now(timezone.utc)
            terminal_sessions.append(session)
            recovered_count += 1
        # 1. Check for sessions stuck in VERIFYING or PROMOTING
        stmt_stalled = select(PackageUploadSession).where(
            PackageUploadSession.status.in_([
                PackageSessionStatus.VERIFYING.value,
                PackageSessionStatus.PROMOTING.value,
            ]),
            PackageUploadSession.updated_at < cutoff,
        )
        res_stalled = await db.execute(stmt_stalled)
        stalled_verifying = res_stalled.scalars().all()

        if stalled_verifying:
            try:
                redis_conn_probe = Redis.from_url(settings.REDIS_QUEUE_URL)
            except Exception as e:
                logger.error(f"[RECONCILER] Redis connection error during VERIFYING check: {e}")
                redis_conn_probe = None

            for session in stalled_verifying:
                job_id = session.verification_job_id or f"pkg_verify_{session.id}"
                # Unknown is not evidence of abandonment. Never clean staging
                # when Redis cannot tell us whether a worker is using it.
                is_active = None

                if redis_conn_probe:
                    try:
                        import warnings
                        with warnings.catch_warnings():
                            warnings.filterwarnings("ignore", category=DeprecationWarning)
                            job = Job.fetch(job_id, connection=redis_conn_probe)
                            job_status = job.get_status()
                            # If RQ job is currently active, do NOT fail the package
                            job_status = getattr(job_status, "value", job_status)
                            if job_status in ("started", "queued", "deferred", "scheduled"):
                                is_active = True
                            elif job_status in ("finished", "failed", "canceled", "cancelled", "stopped"):
                                is_active = False
                            if is_active:
                                logger.info(
                                    f"[RECONCILER] Session {session.id} ({session.status}) has active RQ job {job_id} (status: {job_status}). Preserving state."
                                )
                    except NoSuchJobError:
                        is_active = False
                    except (RedisError, OSError):
                        logger.warning("[RECONCILER] Package activity unavailable; preserving session %s", session.id)
                    except Exception:
                        logger.exception("[RECONCILER] Cannot establish package activity; preserving session %s", session.id)

                if is_active is False:
                    logger.warning(
                        f"[RECONCILER] Package session {session.id} stuck in {session.status} since {session.updated_at} with no active RQ job. Marking FAILED."
                    )
                    session.status = PackageSessionStatus.FAILED.value
                    session.error_message = "Verification timeout: processing worker timed out or stopped unexpectedly."
                    session.updated_at = datetime.now(timezone.utc)
                    terminal_sessions.append(session)
                    recovered_count += 1

        # 2. Check for sessions stuck in QUEUED_FOR_VERIFICATION
        stmt_queued = select(PackageUploadSession).where(
            PackageUploadSession.status == PackageSessionStatus.QUEUED_FOR_VERIFICATION.value,
            PackageUploadSession.verification_started_at.is_(None),
            (PackageUploadSession.verification_enqueued_at < cutoff)
            | (PackageUploadSession.verification_enqueued_at.is_(None)),
        )
        res_queued = await db.execute(stmt_queued)
        stalled_queued = res_queued.scalars().all()

        if stalled_queued:
            try:
                redis_conn = Redis.from_url(settings.REDIS_QUEUE_URL)
                q = Queue("transcode", connection=redis_conn)
            except Exception as e:
                logger.error(f"Reconciliation failed: Redis connection error: {e}")
                redis_conn = None
                q = None

            if q:
                for session in stalled_queued:
                    job_id = session.verification_job_id or f"pkg_verify_{session.id}"
                    needs_enqueue = False

                    try:
                        import warnings
                        with warnings.catch_warnings():
                            warnings.filterwarnings("ignore", category=DeprecationWarning)
                            existing_job = Job.fetch(job_id, connection=redis_conn)
                            job_status = existing_job.get_status()
                            job_status = getattr(job_status, "value", job_status)
                            if job_status in ("failed", "canceled", "cancelled", "stopped", "finished"):
                                needs_enqueue = True
                    except NoSuchJobError:
                        needs_enqueue = True
                    except Exception:
                        logger.warning("[RECONCILER] Queue activity unavailable; preserving package %s", session.id)
                        continue

                    if needs_enqueue:
                        if session.finalize_attempts >= 3:
                            logger.warning(
                                f"[RECONCILER] Stalled package {session.id} exceeded max retries. Marking FAILED."
                            )
                            session.status = PackageSessionStatus.FAILED.value
                            session.error_message = "Verification timeout: max enqueue retries exceeded."
                            session.updated_at = datetime.now(timezone.utc)
                            terminal_sessions.append(session)
                            recovered_count += 1
                        else:
                            logger.warning(
                                f"[RECONCILER] Recovering stalled package {session.id}: re-enqueueing job {job_id}"
                            )
                            try:
                                import warnings
                                with warnings.catch_warnings():
                                    warnings.filterwarnings("ignore", category=DeprecationWarning)
                                    q.enqueue(
                                        "app.workers.package_worker.verify_and_promote_package",
                                        str(session.id),
                                        job_id=job_id,
                                        job_timeout=3600,
                                        result_ttl=86400,
                                    )
                                session.verification_job_id = job_id
                                session.verification_enqueued_at = datetime.now(timezone.utc)
                                session.finalize_attempts += 1
                                session.updated_at = datetime.now(timezone.utc)
                                recovered_count += 1
                            except Exception as e:
                                logger.error(f"Failed to re-enqueue job {job_id} during reconciliation: {e}")

        await db.commit()

        for session in terminal_sessions:
            PackageStorageCleanup.remove_terminal_staging(session)

    return recovered_count


def _rq_failed_job_ids(redis_conn: Redis, queue_name: str = "transcode") -> set:
    """Collect RQ job ids sitting in the failed registry (e.g. AbandonedJobError after a crash)."""
    failed_ids = set()
    try:
        registry = FailedJobRegistry(queue_name, connection=redis_conn)
        for job_id in registry.get_job_ids():
            failed_ids.add(str(job_id))
    except Exception as e:
        logger.warning(f"[RECONCILER] Could not read RQ failed registry: {e}")
    return failed_ids


def _rq_failed_job_args_contain(redis_conn: Redis, db_job_id: str, queue_name: str = "transcode") -> bool:
    """Check whether any failed RQ job references this DB transcode job id."""
    try:
        registry = FailedJobRegistry(queue_name, connection=redis_conn)
        for job_id in registry.get_job_ids():
            try:
                job = Job.fetch(job_id, connection=redis_conn)
            except Exception:
                continue
            args = job.args or ()
            if any(db_job_id in str(arg) for arg in args):
                return True
    except Exception as e:
        logger.warning(f"[RECONCILER] Could not scan RQ failed registry: {e}")
    return False


async def reconcile_stalled_transcode_jobs(
    threshold_minutes: int = 10,
    redis_conn: Optional[Redis] = None,
) -> int:
    """
    Detect transcode jobs left in an active DB state after their worker died
    (host crash, VM suspend, container kill) and mark them terminal.

    Classification requires positive evidence of abandonment — never wall-clock
    runtime alone — so a healthy long-running encode is never touched:
      - worker heartbeat is OFFLINE (no live/stale worker record), AND
        the job's progress has been stale past the threshold, OR
      - RQ has already moved the job to the failed registry (AbandonedJobError).

    The job is marked FAILED with code TRANSCODE_ABANDONED and the video is
    returned to its retryable state. No new transcode is enqueued here; the
    admin decides on Retry, which may reuse verified renditions.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    handled = 0
    owns_redis = False

    async with get_db_context() as db:
        stmt = select(TranscodeJob).where(
            TranscodeJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
            TranscodeJob.updated_at < cutoff,
        )
        jobs = (await db.execute(stmt)).scalars().all()
        if not jobs:
            return 0

        if redis_conn is None:
            try:
                redis_conn = Redis.from_url(settings.REDIS_QUEUE_URL)
                owns_redis = True
            except Exception as e:
                logger.error(f"[RECONCILER] Redis unavailable for transcode reconciliation: {e}")
                return 0

        failed_rq_ids = _rq_failed_job_ids(redis_conn)

        for job in jobs:
            job_id_str = str(job.id)
            stale_progress = job.updated_at < cutoff
            # A different healthy worker cannot establish this job's liveness.
            # Preserve old jobs with no assigned worker unless the whole pool
            # is offline or RQ provides positive abandonment evidence.
            worker_state = get_worker_status(redis_conn, job.worker_id) if job.worker_id else get_worker_status(redis_conn)
            worker_alive = worker_state.get("status") in ("ONLINE", "STALE")

            # Positive abandonment evidence:
            # 1) RQ already failed/abandoned the job, or
            # 2) no live worker heartbeat AND progress stale.
            rq_abandoned = job_id_str in failed_rq_ids or _rq_failed_job_args_contain(redis_conn, job_id_str)
            orphaned = (not worker_alive) and stale_progress

            if not (rq_abandoned or orphaned):
                continue

            reason = "rq_abandoned" if rq_abandoned else "worker_lost"
            logger.warning(
                f"[RECONCILER] Transcode job {job.id} abandoned ({reason}); "
                f"last progress at {job.updated_at}. Marking FAILED/{TRANSCODE_ABANDONED_ERROR_CODE}."
            )

            job.status = TranscodeJobStatus.FAILED
            job.error_code = TRANSCODE_ABANDONED_ERROR_CODE
            job.error_message = TRANSCODE_ABANDONED_MESSAGE
            job.completed_at = datetime.now(timezone.utc)
            job.eta_seconds = None
            job.updated_at = datetime.now(timezone.utc)

            video = await db.get(Video, job.video_id)
            if video and not video.active_generation_id and video.status in (
                VideoStatus.QUEUED,
                VideoStatus.PROBING,
                VideoStatus.PROCESSING,
                VideoStatus.VERIFYING,
            ):
                video.status = VideoStatus.FAILED
                video.updated_at = datetime.now(timezone.utc)

            handled += 1

        await db.commit()

    if owns_redis and redis_conn is not None:
        try:
            redis_conn.close()
        except Exception:
            pass

    return handled


async def _run_one_cycle(package_threshold_minutes: int, transcode_threshold_minutes: int):
    """
    Run all reconciliation passes inside a SINGLE event loop, then dispose the
    async engine so the next cycle (a fresh asyncio.run) never reuses
    connections bound to a previous loop.
    """
    from app.core.database import close_db_connections

    try:
        recovered = await reconcile_stalled_package_jobs(threshold_minutes=package_threshold_minutes)
        abandoned = await reconcile_stalled_transcode_jobs(threshold_minutes=transcode_threshold_minutes)
        async with get_db_context() as db:
            recovered += await reconcile_failed_package_video_states(db)
            await cleanup_expired_user_sessions(db, batch_size=250)
            await prune_playback_analytics(db)
            await db.commit()
        return recovered, abandoned
    finally:
        await close_db_connections()


class ReconcilerService:
    def __init__(self, interval_seconds: int = 300):
        self.interval_seconds = interval_seconds
        self.running = True

    def stop(self, signum, frame):
        logger.info(f"Reconciler received signal {signum}, initiating graceful shutdown...")
        self.running = False

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        logger.info(
            f"=== ABOODVIDEO Reconciliation Service Started (Interval: {self.interval_seconds}s) ==="
        )
        while self.running:
            try:
                recovered, abandoned = asyncio.run(_run_one_cycle(5, 10))
                publish_reconciler_heartbeat()
                if recovered > 0 or abandoned > 0:
                    logger.info(
                        f"[RECONCILER] Cycle complete: {recovered} stalled packages recovered, "
                        f"{abandoned} abandoned transcode jobs marked terminal."
                    )
            except Exception as e:
                logger.error(f"[RECONCILER] Error in reconciliation loop: {e}", exc_info=True)

            # Sleep in 1-second ticks for responsive shutdown
            for _ in range(self.interval_seconds):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("=== ABOODVIDEO Reconciliation Service Stopped Gracefully ===")


def main():
    parser = argparse.ArgumentParser(description="ABOODVIDEO Package Reconciler Service")
    parser.add_argument("--once", action="store_true", help="Run a single reconciliation cycle and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=settings.RECONCILER_INTERVAL_SECONDS,
        help="Loop interval in seconds (default 300s = 5m)",
    )
    args = parser.parse_args()

    if args.once:
        logger.info("Executing single reconciliation pass...")
        recovered, abandoned = asyncio.run(_run_one_cycle(0, 0))
        logger.info(f"Single pass complete. Packages recovered: {recovered}, abandoned transcode jobs: {abandoned}")
        sys.exit(0)
    else:
        service = ReconcilerService(interval_seconds=args.interval)
        service.run()





async def reconcile_failed_package_video_states(db) -> int:
    """Repair only PROCESSING videos orphaned by terminally failed packages."""
    failed_package_exists = exists().where(
        PackageUploadSession.video_id == Video.id,
        PackageUploadSession.status == PackageSessionStatus.FAILED.value,
    )
    live_package_exists = exists().where(
        PackageUploadSession.video_id == Video.id,
        PackageUploadSession.status.in_((
            PackageSessionStatus.INITIALIZED.value,
            PackageSessionStatus.UPLOADING.value,
            PackageSessionStatus.UPLOADED.value,
            PackageSessionStatus.QUEUED_FOR_VERIFICATION.value,
            PackageSessionStatus.VERIFYING.value,
            PackageSessionStatus.PROMOTING.value,
        )),
    )
    active_transcode_exists = exists().where(
        TranscodeJob.video_id == Video.id,
        TranscodeJob.status.in_(ACTIVE_JOB_STATUSES),
    )
    result = await db.execute(
        select(Video)
        .where(
            Video.status == VideoStatus.PROCESSING,
            Video.active_generation_id.is_(None),
            failed_package_exists,
            ~live_package_exists,
            ~active_transcode_exists,
        )
        .with_for_update(skip_locked=True)
    )
    videos = result.scalars().all()
    for video in videos:
        video.status = VideoStatus.FAILED
        logger.warning(
            "[RECONCILER] Marking orphaned package video %s FAILED after terminal package validation failure.",
            video.id,
        )
    return len(videos)



def publish_reconciler_heartbeat() -> None:
    """Publish a short-lived heartbeat in the configured cache namespace."""
    try:
        with Redis.from_url(settings.REDIS_CACHE_URL, socket_connect_timeout=2, socket_timeout=2) as client:
            client.set(
                RECONCILER_HEARTBEAT_KEY,
                str(time.time()),
                ex=max(settings.RECONCILER_INTERVAL_SECONDS * 3, 60),
            )
    except Exception as exc:
        logger.warning("[RECONCILER] Heartbeat publish failed (%s).", type(exc).__name__)


RECONCILER_HEARTBEAT_KEY = f"abv:{settings.ENVIRONMENT}:reconciler:heartbeat"

if __name__ == "__main__":
    main()
