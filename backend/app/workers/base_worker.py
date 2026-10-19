"""Base Worker Entrypoint for Transcoding & Maintenance."""
import time
from app.core.config import settings
from app.core.logging import logger


def run_worker():
    logger.info("=== [ABOODVIDEO Worker] Initializing Worker Environment ===")
    logger.info(f"Target Queue URL: {settings.REDIS_QUEUE_URL}")
    logger.info(f"Transcode Concurrency: {settings.TRANSCODE_CONCURRENCY}")
    logger.info("Worker foundation initialized. Ready for Phase 3 job processing.")

    # Keep worker process alive in background container
    while True:
        time.sleep(60)


if __name__ == "__main__":
    run_worker()
