"""Resumable source upload and server conversion; no local FFmpeg invocation."""
import hashlib
import time
from pathlib import Path
import httpx


def process_on_server(source_path, server_url, api_key, host_header, metadata, saved, progress, cancelled, persist, client=None, poll_seconds=7200):
    source = Path(source_path)
    base = server_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    base += "/studio/source-uploads"
    headers = {"Authorization": f"Bearer {api_key}"}
    if host_header:
        headers["Host"] = host_header
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, read=120))
    try:
        sid, vid = saved.get("upload_id"), saved.get("video_id")
        def check():
            if cancelled.is_set():
                raise RuntimeError("تم إلغاء الرفع للخادم؛ يمكن استئناف المصدر المحفوظ.")
        if sid:
            response = client.get(f"{base}/{sid}/status", headers=headers)
            response.raise_for_status()
            state = response.json()
        else:
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                read = 0
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    check(); digest.update(block); read += len(block)
                    progress("hashing_source", read * 100 / max(1, source.stat().st_size))
            payload = {**metadata, "filename": source.name, "file_size_bytes": source.stat().st_size, "chunk_size_bytes": 5 * 1024 * 1024, "expected_sha256": digest.hexdigest()}
            response = client.post(base + "/initiate", headers=headers, json=payload)
            response.raise_for_status(); state = response.json()
            sid, vid = state["upload_id"], state["video_id"]
            persist({"upload_id":sid, "video_id":vid})
        if state["status"] != "COMPLETED":
            chunk_size = state["chunk_size_bytes"]
            received = set(state.get("received_chunks", []))
            with source.open("rb") as stream:
                for index in range(state["total_chunks"]):
                    check()
                    if index in received:
                        continue
                    stream.seek(index * chunk_size); block = stream.read(chunk_size)
                    response = client.post(f"{base}/{sid}/chunk", headers={**headers, "X-Chunk-SHA256":hashlib.sha256(block).hexdigest()}, data={"chunk_index":index}, files={"chunk_file":("chunk.bin",block,"application/octet-stream")})
                    response.raise_for_status()
                    progress("UPLOADING", response.json()["progress_percent"])
            response = client.post(f"{base}/{sid}/finalize", headers=headers)
            response.raise_for_status()
        status_url = f"{base}/videos/{vid}/status"
        response = client.get(status_url, headers=headers); response.raise_for_status()
        if response.json()["status"] not in {"READY", "QUEUED", "PROBING", "PROCESSING", "VERIFYING"}:
            response = client.post(f"{base}/videos/{vid}/transcode", headers=headers)
            response.raise_for_status()
        started = time.monotonic()
        while time.monotonic() - started < poll_seconds:
            check(); response = client.get(status_url, headers=headers); response.raise_for_status(); state = response.json()
            progress(state["status"], state.get("progress", 0))
            if state["status"] == "READY":
                return vid
            if state["status"] in {"FAILED", "CANCELLED"}:
                raise RuntimeError(state.get("error") or "تعذر التحويل على الخادم.")
            cancelled.wait(1)
        raise TimeoutError("التحويل مستمر على الخادم؛ يمكن متابعة الحالة دون إعادة رفع المصدر.")
    finally:
        if owned:
            client.close()
