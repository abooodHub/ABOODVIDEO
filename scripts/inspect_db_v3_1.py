import sys
import subprocess

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

code = r'''
import asyncio, re
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict, List
import uuid

from app.core.database import AsyncSessionLocal
from app.models.video import Video, VideoRendition, TranscodeJob, TranscodeJobStatus, VideoStatus
from app.models.share import Share
from app.models.audit import AuditLog
from sqlalchemy import select, func

TEST_TITLE_PATTERN = r"(test|sample|temp|seq_test|pause|missing|dup|checksum|auth|cancel|stale|expired|exp\b|fake|dummy|mock|demo|fixture|verification|تجربة|awesome|csrf)"
TEST_RE = re.compile(TEST_TITLE_PATTERN, re.IGNORECASE)

def _is_test(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(TEST_RE.search(text))

def _non_test(video_cls=Video) -> Any:
    return ~(
        video_cls.title.op("~*")(TEST_TITLE_PATTERN)
        | video_cls.source_filename.op("~*")(TEST_TITLE_PATTERN)
    )

def _normalize_quality(label: str, height: Optional[int] = None) -> str:
    clean = (label or "").strip()
    if (height and height >= 2160) or "2160" in clean:
        return "4K"
    return clean

async def main():
    async with AsyncSessionLocal() as db:
        # Videos
        video_rows = (
            await db.execute(
                select(Video)
                .where(_non_test(Video), Video.trashed_at.is_(None))
                .order_by(Video.created_at.desc(), Video.id.desc())
                .limit(6)
            )
        ).scalars().all()

        video_ids = [v.id for v in video_rows]
        rendition_map = {v.id: [] for v in video_rows}
        if video_ids:
            renditions = (
                await db.execute(
                    select(VideoRendition)
                    .where(
                        VideoRendition.video_id.in_(video_ids),
                        VideoRendition.is_ready.is_(True),
                    )
                    .order_by(VideoRendition.resolution_height.desc().nullslast())
                )
            ).scalars().all()
            for r in renditions:
                rendition_map[r.video_id].append(r)

        latest_videos = []
        for video in video_rows:
            ready_renditions = rendition_map.get(video.id, [])
            qualities_list = [_normalize_quality(r.quality_label, r.resolution_height) for r in ready_renditions]
            seen_q = set()
            unique_qualities = [q for q in qualities_list if not (q in seen_q or seen_q.add(q))]

            best_quality = unique_qualities[0] if unique_qualities else None
            q_count = len(unique_qualities)
            if best_quality and q_count > 1:
                quality_summary = f"{best_quality} · {q_count} جودات"
            elif best_quality:
                quality_summary = best_quality
            else:
                quality_summary = None

            latest_videos.append({
                "id": str(video.id),
                "title": video.title,
                "quality": best_quality,
                "quality_summary": quality_summary,
                "qualities": unique_qualities,
            })

        print("=== TEST LOGICAL VIDEOS ===")
        print(f"Total logical videos: {len(latest_videos)}")
        for v in latest_videos:
            print(f"  ID: {v['id']} | title: {v['title']} | quality: {v['quality']} | summary: {v['quality_summary']} | list: {v['qualities']}")

        # Jobs
        job_rows = (
            await db.execute(
                select(TranscodeJob, Video.title)
                .join(Video, TranscodeJob.video_id == Video.id)
                .where(
                    _non_test(Video),
                    TranscodeJob.status.in_([TranscodeJobStatus.QUEUED, TranscodeJobStatus.PROCESSING]),
                )
                .order_by(TranscodeJob.created_at.desc())
                .limit(5)
            )
        ).all()
        print(f"\n=== TEST JOBS (filtered non-test): {len(job_rows)} ===")

        # Activity failed jobs
        failed_jobs = (
            await db.execute(
                select(Video.title, TranscodeJob.completed_at, TranscodeJob.created_at)
                .join(Video, TranscodeJob.video_id == Video.id)
                .where(
                    _non_test(Video),
                    TranscodeJob.status == TranscodeJobStatus.FAILED,
                )
                .order_by(TranscodeJob.created_at.desc())
                .limit(3)
            )
        ).all()
        print(f"=== TEST FAILED JOBS (filtered non-test): {len(failed_jobs)} ===")

asyncio.run(main())
'''

res = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i -e PYTHONPATH=/app aboodvideo_api python"],
    input=code, text=True, encoding="utf-8", errors="replace", capture_output=True
)
print("STDOUT:\n", res.stdout)
if res.stderr:
    print("STDERR:\n", res.stderr)
