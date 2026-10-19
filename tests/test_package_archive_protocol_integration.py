"""Full Studio-client to isolated backend archive upload integration test."""
from __future__ import annotations

import hashlib
import io
import json
import socket
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SyncSessionLocal
from app.models.api_key import ApiKey
from app.models.package import PackageSessionStatus, PackageUploadSession
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus
from app.security.api_key import generate_raw_api_key, hash_api_key
from studio.core.encoder import transcode_package_renditions
from studio.core.ffmpeg_finder import get_ffmpeg_path
from studio.core.packager import build_and_verify_package
from studio.core.uploader import upload_and_verify_package


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RecordingDisconnectTransport(httpx.BaseTransport):
    def __init__(self, drop_percent, captured_archive, state):
        self.inner = httpx.HTTPTransport()
        self.drop_percent = drop_percent
        self.archive_bytes = captured_archive
        self.state = state
        self.dropped = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read() if request.method in {"POST", "PUT"} else b""
        if request.url.path.endswith("/studio/packages/init"):
            self.state["init_payload"] = json.loads(body)
        if request.url.path.endswith("/archive/init"):
            archive_metadata = json.loads(body)
            self.state["archive_size"] = int(archive_metadata["size"])
            self.state["archive_sha256"] = str(archive_metadata["sha256"])
        response = self.inner.handle_request(request)
        response.read()
        if request.method == "PUT" and request.url.path.endswith("/archive/chunk"):
            offset = int(request.url.params["offset"])
            if offset == len(self.archive_bytes):
                self.archive_bytes.extend(body)
            elif offset + len(body) > len(self.archive_bytes):
                response.close()
                raise AssertionError("transport observed a non-contiguous archive write")
            if (self.drop_percent is not None and not self.dropped
                    and len(self.archive_bytes) * 100 >= self.state["archive_size"] * self.drop_percent):
                self.dropped = True
                response.close()
                raise httpx.ReadTimeout(f"simulated response loss after {self.drop_percent}% persisted")
        if request.method == "POST" and request.url.path.endswith("/archive/finalize"):
            if not self.state.get("archive_finalize_replayed"):
                self.state["archive_finalize_replayed"] = True
                duplicate = self.inner.handle_request(httpx.Request("POST", request.url, headers=request.headers))
                self.state["archive_finalize_duplicate_status"] = duplicate.status_code
                duplicate.read()
                duplicate.close()
        if (request.method == "POST" and request.url.path.endswith("/finalize")
                and "/archive/" not in request.url.path):
            # The first finalization dispatches the synchronous test verifier below;
            # the duplicate must observe the same terminal package without enqueueing.
            if not self.state.get("session_finalize_replayed"):
                self.state["session_finalize_replayed"] = True
                duplicate = self.inner.handle_request(httpx.Request("POST", request.url, headers=request.headers))
                self.state["session_finalize_duplicate_status"] = duplicate.status_code
                duplicate.read()
                duplicate.close()
        return response

    def close(self) -> None:
        self.inner.close()


def _make_synthetic_package(tmp_path: Path, ffmpeg: str) -> tuple[Path, dict]:
    source = tmp_path / "synthetic_480p.mp4"
    created = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=854x480:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000",
         "-t", "1.2", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-ar", "48000", "-ac", "2", "-shortest", str(source)],
        capture_output=True, text=True, timeout=90,
    )
    assert created.returncode == 0, created.stderr
    package = tmp_path / "synthetic-package"
    encoded = transcode_package_renditions(
        source, package, ["480p"], encoder_choice="cpu", subtitle_mode="softsub", preset="balanced",
    )
    result = build_and_verify_package(
        package_dir=package,
        title="Synthetic archive integration fixture",
        duration_seconds=encoded["duration_seconds"],
        renditions=encoded["renditions"],
        subtitles=[],
        fonts=[],
    )
    (package / "studio_package_identity.json").write_text(
        json.dumps({"synthetic": True}), encoding="utf-8"
    )
    # Exercise allowlisted cleanup and unknown-preserving final-only transport.
    (package / "480p" / "seg_999.ts").write_bytes(b"unreferenced intermediate")
    (package / "operator.log").write_text("unknown file must remain local", encoding="utf-8")
    return package, result["manifest"]


def _make_tar(package: Path, manifest: dict, *, corrupt_segment: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        paths = [item["path"] for item in manifest["files"]]
        paths.extend(["manifest.json", "studio_package_identity.json"])
        for rel in paths:
            path = package / rel
            data = path.read_bytes()
            if corrupt_segment and rel.endswith(".ts"):
                data = bytes([data[0] ^ 1]) + data[1:]
            item = tarfile.TarInfo(rel)
            item.size = len(data)
            item.mtime = 0
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def test_studio_client_archive_protocol_against_isolated_backend(tmp_path, monkeypatch):
    ffmpeg = get_ffmpeg_path()
    assert ffmpeg
    package, manifest = _make_synthetic_package(tmp_path, ffmpeg)
    port = _unused_loopback_port()
    app = __import__("app.main", fromlist=["create_application"]).create_application()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        access_log=False, lifespan="off",
    ))
    server_thread = threading.Thread(target=server.run, daemon=True, name="isolated-package-api")

    raw_key = generate_raw_api_key()
    api_key_id = uuid.uuid4()
    with SyncSessionLocal() as db:
        user = User(
            id=uuid.uuid4(), email=f"archive_{uuid.uuid4().hex}@example.test",
            username=f"archive_{uuid.uuid4().hex}", hashed_password="fixture",
            role=UserRole.OWNER, is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(ApiKey(
            id=api_key_id, user_id=user.id, name="Disposable archive integration key",
            prefix=raw_key[:12], key_hash=hash_api_key(raw_key),
            scopes=["packages:create", "packages:write", "packages:publish"], is_active=True,
        ))
        db.commit()

    import rq
    from app.workers.package_worker import verify_and_promote_package
    enqueued = []

    def verify_inline(_queue, func, *args, **kwargs):
        assert str(func).endswith("verify_and_promote_package")
        enqueued.append((func, args, kwargs))
        verify_and_promote_package(*args)
        return None

    monkeypatch.setattr(rq.Queue, "enqueue", verify_inline)
    monkeypatch.setenv("ABOODVIDEO_UPLOAD_CHUNK_BYTES", "1024")
    captured_archive = bytearray()
    transport_state = {}
    try:
        server_thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("isolated FastAPI test server did not start")

        base_url = f"http://127.0.0.1:{port}/api/v1"
        ids: dict[str, str] = {}
        for interrupt_percent in (1, 50, 95):
            transport = _RecordingDisconnectTransport(interrupt_percent, captured_archive, transport_state)
            with httpx.Client(transport=transport) as client:
                if not ids:
                    with pytest.raises(httpx.ReadTimeout, match="simulated response loss"):
                        upload_and_verify_package(
                            base_url, raw_key, package, manifest,
                            http_client=client, poll_timeout_seconds=1,
                            progress_callback=lambda item: ids.update({
                                "session_id": str(item["session_id"]),
                                "video_id": str(item["video_id"]),
                            }) if item.get("session_id") and item.get("video_id") else None,
                        )
                    assert transport.dropped
                    continue
                if interrupt_percent == 50:
                    # Re-send the exact init request to prove the key resolves to
                    # the original session and video instead of allocating copies.
                    replay = client.post(
                        f"{base_url}/studio/packages/init",
                        headers={"Authorization": f"Bearer {raw_key}"},
                        json=transport_state["init_payload"],
                    )
                    assert replay.status_code == 201
                    assert replay.json()["session_id"] == ids["session_id"]
                    assert replay.json()["video_id"] == ids["video_id"]
                    archive_size = transport_state["archive_size"]
                    archive_init = client.post(
                        f"{base_url}/studio/packages/{ids['session_id']}/archive/init",
                        headers={"Authorization": f"Bearer {raw_key}"},
                        json={"size": archive_size, "sha256": transport_state["archive_sha256"]},
                    )
                    assert archive_init.status_code == 200
                    assert archive_init.json()["offset"] == len(captured_archive)
                with pytest.raises(httpx.ReadTimeout, match="simulated response loss"):
                    upload_and_verify_package(
                        base_url, raw_key, package, manifest,
                        video_id=ids["video_id"], session_id=ids["session_id"],
                        http_client=client, poll_timeout_seconds=1,
                    )
                assert transport.dropped

        # The three response losses happened only after the server had persisted
        # the chunk; the final resumed client must complete the same session.
        final_transport = _RecordingDisconnectTransport(None, captured_archive, transport_state)
        with httpx.Client(transport=final_transport) as client:
            completed = upload_and_verify_package(
                base_url, raw_key, package, manifest,
                video_id=ids["video_id"], session_id=ids["session_id"],
                http_client=client, poll_timeout_seconds=20,
            )
            assert completed["success"] and completed["status"] == "READY"
            assert transport_state["archive_finalize_duplicate_status"] == 200
            assert transport_state["session_finalize_duplicate_status"] == 202
            session_status = client.get(
                f"{base_url}/studio/packages/{ids['session_id']}",
                headers={"Authorization": f"Bearer {raw_key}"},
            )
            assert session_status.status_code == 200
            assert session_status.json()["status"] == "READY"
            # Repeating the original init key after completion stays idempotent.
            replay = client.post(
                f"{base_url}/studio/packages/init",
                headers={"Authorization": f"Bearer {raw_key}"},
                json=transport_state["init_payload"],
            )
            assert replay.status_code == 201
            assert replay.json()["session_id"] == ids["session_id"]
            assert replay.json()["video_id"] == ids["video_id"]
            mismatched = dict(transport_state["init_payload"])
            mismatched_manifest = dict(mismatched["manifest"])
            mismatched_manifest["video"] = dict(mismatched_manifest["video"], title="Changed request")
            mismatched["manifest"] = mismatched_manifest
            rejected = client.post(
                f"{base_url}/studio/packages/init",
                headers={"Authorization": f"Bearer {raw_key}"},
                json=mismatched,
            )
            assert rejected.status_code == 409

        with tarfile.open(fileobj=io.BytesIO(captured_archive), mode="r:") as uploaded_tar:
            assert set(uploaded_tar.getnames()) == {
                item["path"] for item in manifest["files"]
            } | {"manifest.json", "studio_package_identity.json"}
        assert not (package / "480p" / "seg_999.ts").exists()
        assert (package / "operator.log").is_file()

        with SyncSessionLocal() as db:
            sessions = db.execute(select(PackageUploadSession).where(
                PackageUploadSession.created_by_key_id == api_key_id,
                PackageUploadSession.idempotency_key == transport_state["init_payload"]["idempotency_key"],
            )).scalars().all()
            assert len(sessions) == 1
            assert sessions[0].status == PackageSessionStatus.READY.value
            assert db.execute(select(func.count()).select_from(Video).where(Video.id == sessions[0].video_id)).scalar_one() == 1
            video = db.get(Video, sessions[0].video_id)
            assert video.status == VideoStatus.READY.value
            assert video.active_generation_id == sessions[0].target_generation_id
            generation_dir = Path(settings.STORAGE_VIDEOS_PATH) / str(video.id) / "generations" / str(video.active_generation_id)
            assert (generation_dir / "master.m3u8").is_file()
            assert len(enqueued) == 1

        def create_negative_session(client: httpx.Client, label: str) -> str:
            payload = json.loads(json.dumps(transport_state["init_payload"]))
            payload["idempotency_key"] = hashlib.sha256(f"negative-{label}".encode()).hexdigest()
            response = client.post(
                f"{base_url}/studio/packages/init",
                headers={"Authorization": f"Bearer {raw_key}"},
                json=payload,
            )
            assert response.status_code == 201, response.text
            return response.json()["session_id"]

        def send_archive(client: httpx.Client, session_id: str, archive_data: bytes, *, declared_sha: str | None = None, partial: bool = False) -> httpx.Response:
            sha = declared_sha or hashlib.sha256(archive_data).hexdigest()
            headers = {"Authorization": f"Bearer {raw_key}"}
            initialized = client.post(
                f"{base_url}/studio/packages/{session_id}/archive/init",
                headers=headers, json={"size": len(archive_data), "sha256": sha},
            )
            assert initialized.status_code == 200, initialized.text
            chunk = archive_data[:max(1, len(archive_data) // 2)] if partial else archive_data
            uploaded = client.put(
                f"{base_url}/studio/packages/{session_id}/archive/chunk?offset=0",
                headers={**headers, "Content-Type": "application/octet-stream"},
                content=chunk,
            )
            assert uploaded.status_code == 200, uploaded.text
            return client.post(
                f"{base_url}/studio/packages/{session_id}/archive/finalize",
                headers=headers,
            )

        def single_member_tar(name: str, *, symlink: bool = False) -> bytes:
            target = io.BytesIO()
            with tarfile.open(fileobj=target, mode="w:") as archive:
                entry = tarfile.TarInfo(name)
                if symlink:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "../../outside.ts"
                    entry.size = 0
                    archive.addfile(entry)
                else:
                    entry.size = 1
                    archive.addfile(entry, io.BytesIO(b"x"))
            return target.getvalue()

        original_tar = bytes(captured_archive)
        with httpx.Client(timeout=20) as client:
            incomplete_id = create_negative_session(client, "incomplete")
            incomplete = send_archive(client, incomplete_id, original_tar, partial=True)
            assert incomplete.status_code == 409

            bad_archive_id = create_negative_session(client, "corrupt")
            corrupt = b"not a tar archive"
            corrupt_result = send_archive(client, bad_archive_id, corrupt)
            assert corrupt_result.status_code == 400

            bad_archive_checksum_id = create_negative_session(client, "archive-checksum")
            bad_archive_checksum = send_archive(
                client, bad_archive_checksum_id, original_tar,
                declared_sha=hashlib.sha256(b"different bytes").hexdigest(),
            )
            assert bad_archive_checksum.status_code == 400

            bad_file_checksum_id = create_negative_session(client, "file-checksum")
            bad_file_checksum = send_archive(
                client, bad_file_checksum_id,
                _make_tar(package, manifest, corrupt_segment=True),
            )
            assert bad_file_checksum.status_code == 400

            for label, unsafe_tar in (
                ("traversal", single_member_tar("../escape.ts")),
                ("absolute", single_member_tar("/absolute.ts")),
                ("drive", single_member_tar("C:/absolute.ts")),
                ("symlink", single_member_tar("linked.ts", symlink=True)),
            ):
                unsafe_id = create_negative_session(client, label)
                unsafe_result = send_archive(client, unsafe_id, unsafe_tar)
                assert unsafe_result.status_code == 400

            for session_id in (
                incomplete_id, bad_archive_id, bad_archive_checksum_id, bad_file_checksum_id,
            ):
                status_result = client.get(
                    f"{base_url}/studio/packages/{session_id}",
                    headers={"Authorization": f"Bearer {raw_key}"},
                )
                assert status_result.status_code == 200
                assert status_result.json()["status"] != "READY"
        assert len(enqueued) == 1
    finally:
        server.should_exit = True
        if server_thread.is_alive():
            server_thread.join(timeout=5)
