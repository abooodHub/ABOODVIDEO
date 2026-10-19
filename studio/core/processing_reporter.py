"""Best-effort, coalesced reporting of local Studio processing to the website."""
from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger("studio.processing_reporter")


class LocalProcessingReporter:
    def __init__(self) -> None:
        self.studio_id = uuid.uuid4().hex
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=256)
        self._thread = threading.Thread(target=self._run, name="studio-processing-reporter", daemon=True)
        self._thread.start()

    def report(self, server_url: str, api_key: str, host_header: Optional[str], payload: Dict[str, Any]) -> None:
        if not server_url or not api_key:
            return
        item = {"server_url": server_url, "api_key": api_key, "host_header": host_header, "payload": payload}
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Never block encoding because a remote dashboard is unavailable.
            logger.debug("Local processing report queue is full")

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                base = item["server_url"].rstrip("/")
                if not base.endswith("/api/v1"):
                    base += "/api/v1"
                headers = {"Authorization": f"Bearer {item['api_key']}", "Content-Type": "application/json"}
                if item.get("host_header"):
                    headers["Host"] = str(item["host_header"])
                with httpx.Client(timeout=4.0) as client:
                    response = client.post(base + "/studio/processing/heartbeat", json=item["payload"], headers=headers)
                    if response.status_code >= 400:
                        logger.debug("Processing heartbeat rejected: HTTP %s", response.status_code)
            except (httpx.HTTPError, TimeoutError, ValueError, OSError) as exc:
                # Deliberately exclude request data, URL, and auth headers from logs.
                logger.debug("Processing heartbeat unavailable (%s)", type(exc).__name__)
            except Exception as exc:
                logger.debug("Processing heartbeat error (%s)", type(exc).__name__)

