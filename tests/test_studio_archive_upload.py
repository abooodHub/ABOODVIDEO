"""Local-only archive transport and confirmed-offset resume tests."""
from __future__ import annotations

import hashlib
import io
import json
import math
import tarfile

import httpx
import pytest

from studio.core.uploader import upload_and_verify_package


def test_default_verification_wait_covers_full_length_worker_queue():
    import inspect
    # Keep explicit short deadlines supported while the normal installed-client
    # path can wait behind a full-episode transcode without reporting failure.
    assert inspect.signature(upload_and_verify_package).parameters['poll_timeout_seconds'].default == 7200


@pytest.mark.parametrize("interrupt_percent", [1, 50, 95])
def test_archive_upload_resumes_at_confirmed_offset_without_new_session(tmp_path, monkeypatch, interrupt_percent):
    monkeypatch.setenv("ABOODVIDEO_UPLOAD_CHUNK_BYTES", "16384")
    package_dir = tmp_path / "package"
    (package_dir / "hls").mkdir(parents=True)
    payload = b"x" * 1_627_648
    (package_dir / "hls" / "segment.ts").write_bytes(payload)
    playlist = b"#EXTM3U\n#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
    (package_dir / "hls" / "index.m3u8").write_bytes(playlist)
    declared = [
        {"path": "hls/index.m3u8", "size_bytes": len(playlist), "sha256": hashlib.sha256(playlist).hexdigest()},
        {"path": "hls/segment.ts", "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
    ]
    manifest = {
        "manifest_version": "1.0",
        "video": {"title": "Synthetic archive fixture", "duration_seconds": 6.0},
        "renditions": [{"quality_label": "720p", "playlist_path": "hls/index.m3u8"}],
        "subtitles": [], "fonts": [], "files": declared,
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package_dir / "studio_package_identity.json").write_text("{}", encoding="utf-8")
    state = {"archive": bytearray(), "metadata": None, "chunks": 0, "drop_done": False, "ready": False, "init_calls": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/studio/packages/init"):
            state["init_calls"] += 1
            idempotency = request.read()
            assert len(json.loads(idempotency)["idempotency_key"]) == 64
            return httpx.Response(201, json={
                "session_id": "session-1", "video_id": "video-1", "total_files": 1,
                "total_bytes": len(payload),
                "files": [
                    {"relative_path": item["path"], "expected_size": item["size_bytes"], "file_id": f"file-{i}"}
                    for i, item in enumerate(declared, start=1)
                ],
            })
        if path.endswith("/studio/packages/session-1") and request.method == "GET":
            return httpx.Response(200, json={"status": "READY" if state["ready"] else "UPLOADING", "video_id": "video-1"})
        if path.endswith("/studio/packages/session-1/files"):
            return httpx.Response(200, json={"files": [
                {"relative_path": item["path"], "expected_size": item["size_bytes"], "file_id": f"file-{i}", "status": "PENDING"}
                for i, item in enumerate(declared, start=1)
            ]})
        if path.endswith("/archive/init"):
            state["metadata"] = request.read()
            return httpx.Response(200, json={"offset": len(state["archive"])})
        if path.endswith("/archive/status"):
            return httpx.Response(200, json={"offset": len(state["archive"])})
        if path.endswith("/archive/chunk"):
            chunk = request.read()
            assert int(request.url.params["offset"]) == len(state["archive"])
            state["archive"].extend(chunk)
            state["chunks"] += 1
            archive_size = int(json.loads(state["metadata"])["size"])
            target = max(1, math.ceil(math.ceil(archive_size / 16384) * interrupt_percent / 100))
            if not state["drop_done"] and state["chunks"] == target:
                state["drop_done"] = True
                raise httpx.ReadTimeout("synthetic connection loss")
            return httpx.Response(200, json={"offset": len(state["archive"])})
        if path.endswith("/archive/finalize"):
            metadata = json.loads(state["metadata"])
            assert len(state["archive"]) == metadata["size"]
            assert hashlib.sha256(state["archive"]).hexdigest() == metadata["sha256"]
            with tarfile.open(fileobj=io.BytesIO(state["archive"]), mode="r:") as archive:
                assert set(archive.getnames()) == {item["path"] for item in declared} | {
                    "manifest.json", "studio_package_identity.json"
                }
            return httpx.Response(200, json={"status": "UPLOADED"})
        if path.endswith("/finalize"):
            state["ready"] = True
            return httpx.Response(202, json={"status": "QUEUED_FOR_VERIFICATION"})
        raise AssertionError(f"unexpected local mock request: {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handle))
    session_ids = set()
    try:
        with pytest.raises(httpx.ReadTimeout):
            upload_and_verify_package(
                "http://local.test/api/v1", "test-key", package_dir, manifest,
                progress_callback=lambda data: session_ids.add(data["session_id"]) if data.get("session_id") else None,
                http_client=client, poll_timeout_seconds=0.1,
            )
        assert session_ids == {"session-1"}
        resumed = upload_and_verify_package(
            "http://local.test/api/v1", "test-key", package_dir, manifest,
            video_id="video-1", session_id="session-1", http_client=client, poll_timeout_seconds=0.1,
        )
        assert resumed["success"] and resumed["status"] == "READY"
        assert state["ready"] and state["drop_done"]
        assert state["init_calls"] == 1
    finally:
        client.close()
