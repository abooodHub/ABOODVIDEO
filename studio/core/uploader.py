"""Upload engine and Server Verification Poller for ABOODVIDEO Studio."""
import os
import hashlib
import json
import tempfile
import shutil
import tarfile
import time
import uuid
import httpx
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
import threading
from studio.core.package_cleaner import cleanup_final_package_intermediates, build_final_package_inventory


def clean_server_url(url: str) -> str:
    """Ensure server URL does not end with a trailing slash."""
    url = url.strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


def test_connection(server_url: str, api_key: str, require_package_upload: bool = False,
                   host_header: Optional[str] = None) -> Dict[str, Any]:
    """Tests connection to ABOODVIDEO server and validates API key."""
    base_url = clean_server_url(server_url)
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    if host_header:
        headers["Host"] = host_header.strip()

    try:
        with httpx.Client(timeout=10.0) as client:
            # 1. Authoritative Studio Packages capability verification
            verify_res = client.get(f"{base_url}/studio/packages/verify", headers=headers)
            if verify_res.status_code == 200:
                data = verify_res.json()
                scopes = data.get("scopes") or []
                if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
                    scopes = []
                required_scopes = {"packages:create", "packages:write", "packages:publish"}
                if require_package_upload and ("*" not in scopes) and not required_scopes.issubset(set(scopes)):
                    missing = ", ".join(sorted(required_scopes - set(scopes)))
                    return {
                        "ok": False,
                        "server_status": "unauthorized",
                        "server_url": base_url,
                        "scopes": scopes,
                        "message": f"مفتاح API لا يملك صلاحيات رفع الحزم المطلوبة: {missing}.",
                    }
                return {
                    "ok": True,
                    "server_status": "ok",
                    "server_url": base_url,
                    "scopes": scopes,
                    "message": "تم الاتصال بالخادم والتحقق من مفتاح API بنجاح.",
                }
            if require_package_upload:
                if verify_res.status_code in (401, 403):
                    message = "مفتاح API غير صالح أو تنقصه الصلاحيات المطلوبة (packages:create, packages:write, packages:publish)."
                else:
                    message = f"تعذر التحقق من صلاحيات رفع الحزم على الخادم (HTTP {verify_res.status_code})."
                return {
                    "ok": False,
                    "server_status": "unauthorized" if verify_res.status_code in (401, 403) else "error",
                    "server_url": base_url,
                    "message": message,
                }
            elif verify_res.status_code in (401, 403):
                return {
                    "ok": False,
                    "server_status": "unauthorized",
                    "server_url": base_url,
                    "message": "مفتاح API غير صالح أو تنقصه الصلاحيات المطلوبة (packages:create, packages:write, packages:publish).",
                }

            # 2. Fallback to worker telemetry if packages verify is 404 (legacy server)
            res = client.get(f"{base_url}/health/worker", headers=headers)
            if res.status_code == 200:
                data = res.json()
                return {
                    "ok": True,
                    "server_status": data.get("status", "ok"),
                    "server_url": base_url,
                    "message": "تم الاتصال بالخادم والتحقق من مفتاح API بنجاح.",
                }
            elif res.status_code in (401, 403):
                return {
                    "ok": False,
                    "server_status": "unauthorized",
                    "server_url": base_url,
                    "message": "مفتاح API غير صالح أو تنقصه الصلاحيات المطلوبة (packages:create, packages:write, packages:publish).",
                }
            else:
                # Try generic health
                h_res = client.get(f"{base_url}/health")
                if h_res.status_code == 200:
                    return {
                        "ok": True,
                        "server_status": "ok",
                        "server_url": base_url,
                        "message": "الخادم يعمل، ولكن يرجى التحقق من صلاحيات مفتاح API.",
                    }
                return {
                    "ok": False,
                    "server_status": "error",
                    "server_url": base_url,
                    "message": f"استجاب الخادم برمز خطأ {res.status_code}.",
                }
    except Exception as e:
        return {
            "ok": False,
            "server_status": "unreachable",
            "server_url": base_url,
            "message": f"تعذر الاتصال بالخادم: {str(e)}",
        }


def upload_and_verify_package(
    server_url: str,
    api_key: str,
    package_dir: Path,
    manifest: Dict[str, Any],
    video_id: Optional[str] = None,
    session_id: Optional[str] = None,
    destination: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    poll_timeout_seconds: int = 7200,
    http_client: Optional[httpx.Client] = None,
    host_header: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates the entire upload and server-side verification process:
    1. Source MKV Security Check: ensures no raw MKV is ever declared or uploaded.
    2. Resumption & Idempotency: reuses existing active session or skips already-uploaded files.
    3. Streams each file chunk-by-chunk with resume support via PUT /studio/packages/{session_id}/files/{file_id}.
    4. Finalizes session via POST /studio/packages/{session_id}/finalize (idempotent).
    5. Polls server verification until status becomes READY.

    The default also covers waiting behind complete server transcodes sharing
    the RQ workers. Five minutes can falsely fail a fully uploaded, healthy
    package before a worker is available. Explicit caller deadlines are retained;
    cancellation remains available throughout this bounded wait.
    """
    if isinstance(manifest, dict) and "manifest" in manifest and isinstance(manifest["manifest"], dict):
        manifest = manifest["manifest"]

    # Invariant: Never upload raw MKVs
    for declared in manifest.get("files", []):
        p_str = declared.get("path", "").lower()
        if p_str.endswith(".mkv"):
            raise ValueError(f"Security Policy Violation: Raw video file '{p_str}' must never be included in the upload package.")

    base_url = clean_server_url(server_url)
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    if host_header:
        headers["Host"] = host_header.strip()
    package_dir = Path(package_dir).resolve()
    cleanup_final_package_intermediates(package_dir, apply=True)
    final_inventory = build_final_package_inventory(package_dir, verify_hashes=True, cancel_event=cancel_event, progress_callback=progress_callback)
    disk_manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    if disk_manifest != manifest:
        raise ValueError("محتوى manifest.json تغيّر منذ التحقق المحلي للحزمة.")

    manage_client = http_client is None
    client = http_client if http_client is not None else httpx.Client(timeout=httpx.Timeout(60.0, read=120.0))

    try:
        active_session_id = session_id
        target_video_id = video_id
        declared_files: List[Dict[str, Any]] = []
        total_files = 0
        total_bytes = 0
        resumed_active = False
        skip_file_upload = False

        # Step 1: Check existing session if session_id is provided
        if active_session_id:
            try:
                chk_res = client.get(f"{base_url}/studio/packages/{active_session_id}", headers=headers)
                if chk_res.status_code == 200:
                    s_info = chk_res.json()
                    s_status = s_info.get("status")
                    target_video_id = s_info.get("video_id") or target_video_id

                    # 1A. If already verified and promoted to READY on server
                    if s_status == "READY":
                        if progress_callback:
                            progress_callback({
                                "stage": "completed",
                                "server_status": "READY",
                                "video_id": str(target_video_id),
                                "session_id": str(active_session_id),
                                "upload_progress_pct": 100.0,
                                "message": "الحزمة مكتملة ومعتمدة بالفعل على الخادم (READY ✓).",
                            })
                        return {
                            "success": True,
                            "session_id": str(active_session_id),
                            "video_id": str(target_video_id),
                            "status": "READY",
                            "server_url": base_url,
                        }

                    # Archive verification may have committed immediately before
                    # Studio closed; enqueue the already uploaded package idempotently.
                    if s_status == "UPLOADED":
                        fin_res = client.post(f"{base_url}/studio/packages/{active_session_id}/finalize", headers=headers)
                        if fin_res.status_code not in (200, 202):
                            raise RuntimeError(f"تعذر استكمال اعتماد الحزمة المرفوعة ({fin_res.status_code}).")
                        resumed_active = True
                        skip_file_upload = True

                    # 1B. If already queued or in verification
                    elif s_status in ("QUEUED_FOR_VERIFICATION", "VERIFYING", "PROMOTING"):
                        if progress_callback:
                            progress_callback({
                                "stage": "server_verifying",
                                "server_status": s_status,
                                "session_id": str(active_session_id),
                                "video_id": str(target_video_id),
                                "upload_progress_pct": 100.0,
                                "message": f"الحزمة مكتملة الرفع وتخضع للتحقق على الخادم ({s_status})...",
                            })
                        resumed_active = True
                        skip_file_upload = True

                    # 1C. If actively in staging / uploading: query files list for partial resumption
                    elif s_status in ("INITIALIZED", "STAGING", "UPLOADING"):
                        files_res = client.get(f"{base_url}/studio/packages/{active_session_id}/files", headers=headers)
                        if files_res.status_code == 200:
                            f_data = files_res.json()
                            declared_files = f_data.get("files", [])
                            total_files = len(declared_files)
                            total_bytes = sum(f.get("expected_size", 0) for f in declared_files)
                            resumed_active = True
                            skip_file_upload = False
            except Exception:
                resumed_active = False

        # Step 2: Initialize new session if not resumed
        if not resumed_active:
            if progress_callback:
                progress_callback({
                    "stage": "initializing_session",
                    "message": "جاري تهيئة جلسة الحزمة على الخادم...",
                    "progress_pct": 0.0,
                })

            init_payload: Dict[str, Any] = {"manifest": manifest}
            identity_payload = {
                "manifest": manifest,
                "video_id": str(target_video_id or ""),
                "destination": destination or {},
            }
            init_payload["idempotency_key"] = hashlib.sha256(
                json.dumps(identity_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if target_video_id:
                init_payload["video_id"] = str(target_video_id)
            if destination:
                if destination.get("folder_id"):
                    init_payload["folder_id"] = str(destination["folder_id"])
                if destination.get("series_id"):
                    init_payload["series_id"] = str(destination["series_id"])
                if destination.get("season_id"):
                    init_payload["season_id"] = str(destination["season_id"])
                if destination.get("episode_number") is not None:
                    init_payload["episode_number"] = int(destination["episode_number"])

            init_res = client.post(
                f"{base_url}/studio/packages/init",
                headers={**headers, "Content-Type": "application/json"},
                json=init_payload,
            )

            if init_res.status_code != 201:
                err_msg = init_res.text
                try:
                    err_msg = init_res.json().get("detail", err_msg)
                except Exception:
                    pass
                raise RuntimeError(f"فشل تهيئة جلسة الحزمة على الخادم ({init_res.status_code}): {err_msg}")

            session_data = init_res.json()
            active_session_id = session_data["session_id"]
            target_video_id = session_data["video_id"]
            total_files = session_data["total_files"]
            total_bytes = session_data["total_bytes"]
            declared_files = session_data["files"]
            skip_file_upload = False

        # Step 3: Send one uncompressed tar archive in resumable bounded chunks.
        if not skip_file_upload:
            if progress_callback:
                progress_callback({"stage": "session_initialized", "session_id": str(active_session_id), "video_id": str(target_video_id), "upload_progress_pct": 0.0})
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("تم إلغاء تجهيز الحزمة.")
            required = int(final_inventory["declared_bytes"]) + (len(final_inventory["files"]) + 2) * 10240 + 64 * 1024 * 1024
            if shutil.disk_usage(tempfile.gettempdir()).free < required:
                raise OSError("مساحة مجلد Windows المؤقت لا تكفي لتجهيز أرشيف الحزمة.")
            prepared = 0
            class ObservableReader:
                def __init__(self, stream): self.stream = stream
                def read(self, size=-1):
                    nonlocal prepared
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("تم إلغاء تجهيز الأرشيف.")
                    data = self.stream.read(size)
                    prepared += len(data)
                    if progress_callback:
                        progress_callback({"stage":"creating_archive", "preparation_progress": min(100, prepared * 100 / max(1, final_inventory["declared_bytes"]))})
                    return data
            fd, archive_name = tempfile.mkstemp(prefix="aboodvideo-upload-", suffix=".tar")
            os.close(fd)
            archive_path = Path(archive_name)
            try:
                with tarfile.open(archive_path, mode="w:") as archive:
                    declared_paths = {item["relative_path"].replace("\\", "/") for item in declared_files}
                    local_paths = {item["path"] for item in final_inventory["files"]}
                    if declared_paths != local_paths:
                        raise ValueError("قائمة ملفات الحزمة المحلية لا تطابق القائمة التي اعتمدها الخادم.")
                    for item in final_inventory["files"]:
                        rel_path = item["path"]
                        source = (package_dir / rel_path).resolve()
                        if package_dir not in source.parents or not source.is_file():
                            raise FileNotFoundError(f"الملف المطلوب رفعه مفقود أو مساره غير صالح: {rel_path}")
                        with source.open("rb") as stream:
                            archive.addfile(archive.gettarinfo(str(source), arcname=rel_path), ObservableReader(stream))
                    for extra in final_inventory["metadata_files"]:
                        archive.add(package_dir / extra, arcname=extra, recursive=False)
                archive_size = archive_path.stat().st_size
                archive_hash = hashlib.sha256()
                with archive_path.open("rb") as source:
                    hashed = 0
                    for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("تم إلغاء التحقق من الأرشيف.")
                        archive_hash.update(block)
                        hashed += len(block)
                        if progress_callback:
                            progress_callback({"stage":"hashing_archive", "preparation_progress":hashed * 100 / max(1,archive_size)})
                init_res = client.post(
                    f"{base_url}/studio/packages/{active_session_id}/archive/init",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"size": archive_size, "sha256": archive_hash.hexdigest()},
                )
                if init_res.status_code == 404:
                    # Archive endpoint not present on server (e.g. 1.1.0-clean); use per-file streaming upload
                    archive_path.unlink(missing_ok=True)
                    uploaded_bytes_total = 0
                    start_upload_time = time.time()
                    for f_idx, item in enumerate(declared_files):
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("تم إلغاء عملية الرفع؛ جلسة الخادم محفوظة للاستئناف.")
                        file_id = item["file_id"]
                        rel_path = item["relative_path"]
                        f_abs = package_dir / rel_path
                        if not f_abs.is_file():
                            raise FileNotFoundError(f"الملف المطلوب رفعه مفقود محلياً: {rel_path}")
                        file_size = f_abs.stat().st_size
                        media_type = item.get("media_type") or "application/octet-stream"
                        if item.get("status") == "COMPLETED":
                            uploaded_bytes_total += file_size
                            continue
                        max_retries = 3
                        success = False
                        for attempt in range(max_retries):
                            try:
                                with open(f_abs, "rb") as f_stream:
                                    def blocks():
                                        for block in iter(lambda: f_stream.read(1024 * 1024), b""):
                                            if cancel_event and cancel_event.is_set():
                                                raise RuntimeError("تم إلغاء الرفع؛ جلسة الخادم محفوظة للاستئناف.")
                                            yield block
                                    put_res = client.put(
                                        f"{base_url}/studio/packages/{active_session_id}/files/{file_id}",
                                        headers={**headers, "Content-Type": media_type, "Content-Length": str(file_size)},
                                        content=blocks(),
                                    )
                                if put_res.status_code == 200:
                                    success = True
                                    uploaded_bytes_total += file_size
                                    break
                                else:
                                    time.sleep(1.0 * (attempt + 1))
                            except Exception as e:
                                if attempt == max_retries - 1:
                                    raise RuntimeError(f"فشل رفع الملف {rel_path} بعد {max_retries} محاولات: {str(e)}")
                                time.sleep(1.0 * (attempt + 1))
                        if not success:
                            raise RuntimeError(f"فشل رفع الملف {rel_path} إلى الخادم.")
                        if progress_callback and (f_idx % 25 == 0 or f_idx == len(declared_files) - 1):
                            elapsed = max(0.1, time.time() - start_upload_time)
                            speed_bps = uploaded_bytes_total / elapsed
                            eta = int(max(0, total_bytes - uploaded_bytes_total) / max(1000, speed_bps))
                            progress_callback({
                                "stage": "uploading_files",
                                "session_id": str(active_session_id),
                                "video_id": str(target_video_id),
                                "file_index": f_idx + 1,
                                "total_files": len(declared_files),
                                "uploaded_bytes": uploaded_bytes_total,
                                "total_bytes": total_bytes,
                                "upload_progress_pct": round(uploaded_bytes_total * 100 / max(1, total_bytes), 1),
                                "upload_speed_mbps": round(speed_bps * 8 / (1024 * 1024), 2),
                                "upload_speed_formatted": f"{speed_bps / (1024 * 1024):.2f} MB/s",
                                "eta_seconds": eta,
                                "eta_formatted": time.strftime("%H:%M:%S", time.gmtime(eta)),
                            })
                elif init_res.status_code != 200:
                    raise RuntimeError(f"فشل تهيئة أرشيف الحزمة ({init_res.status_code}): {init_res.text}")
                else:
                    status_res = client.get(f"{base_url}/studio/packages/{active_session_id}/archive/status", headers=headers)
                    if status_res.status_code != 200:
                        raise RuntimeError(f"تعذر استعادة موضع الأرشيف ({status_res.status_code}): {status_res.text}")
                    offset = int(status_res.json().get("offset", 0))
                    if offset < 0 or offset > archive_size:
                        raise RuntimeError("موضع استئناف أرشيف الحزمة غير صالح.")
                    started = time.time()
                    starting_offset = offset
                    chunk_size = min(32 * 1024 * 1024, max(1, int(os.environ.get("ABOODVIDEO_UPLOAD_CHUNK_BYTES", str(16 * 1024 * 1024)))))
                    with archive_path.open("rb") as source:
                        source.seek(offset)
                        while offset < archive_size:
                            if cancel_event and cancel_event.is_set():
                                raise RuntimeError("تم إلغاء عملية الرفع؛ جلسة الخادم محفوظة للاستئناف.")
                            chunk = source.read(chunk_size)
                            response = client.put(
                                f"{base_url}/studio/packages/{active_session_id}/archive/chunk?offset={offset}",
                                headers={**headers, "Content-Type": "application/octet-stream"},
                                content=chunk,
                            )
                            if response.status_code != 200:
                                probe = client.get(f"{base_url}/studio/packages/{active_session_id}/archive/status", headers=headers)
                                server_offset = int(probe.json().get("offset", -1)) if probe.status_code == 200 else -1
                                if server_offset != offset + len(chunk):
                                    raise RuntimeError(f"فشل رفع كتلة الأرشيف عند {offset} بايت ({response.status_code}).")
                                offset = server_offset
                                source.seek(offset)
                            else:
                                offset = int(response.json().get("offset", offset + len(chunk)))
                            elapsed = max(0.1, time.time() - started)
                            speed_bps = (offset - starting_offset) / elapsed
                            eta = int(max(0, archive_size - offset) / max(1000, speed_bps))
                            if progress_callback:
                                progress_callback({
                                    "stage": "uploading_files", "session_id": str(active_session_id), "video_id": str(target_video_id),
                                    "uploaded_bytes": int(total_bytes * offset / max(1, archive_size)), "total_bytes": total_bytes,
                                    "upload_progress_pct": round(offset * 100 / max(1, archive_size), 1),
                                    "upload_speed_mbps": round(speed_bps * 8 / (1024 * 1024), 2),
                                    "upload_speed_formatted": f"{speed_bps / (1024 * 1024):.2f} MB/s",
                                    "eta_seconds": eta, "eta_formatted": time.strftime("%H:%M:%S", time.gmtime(eta)),
                                })
                    archive_final = client.post(f"{base_url}/studio/packages/{active_session_id}/archive/finalize", headers=headers)
                    if archive_final.status_code != 200:
                        raise RuntimeError(f"فشل التحقق من أرشيف الحزمة ({archive_final.status_code}): {archive_final.text}")
            finally:
                archive_path.unlink(missing_ok=True)

            # Step 4: Finalize Session (Idempotent)
            if progress_callback:
                progress_callback({
                    "stage": "finalizing",
                    "session_id": str(active_session_id),
                    "video_id": str(target_video_id),
                    "message": "اكتمل رفع الملفات، جاري إرسال طلب الاعتماد إلى الخادم...",
                    "upload_progress_pct": 100.0,
                })

            fin_res = client.post(
                f"{base_url}/studio/packages/{active_session_id}/finalize",
                headers=headers,
            )

            if fin_res.status_code not in (200, 202):
                raise RuntimeError(f"فشل اعتماد الحزمة ({fin_res.status_code}): {fin_res.text}")

        # Step 5: Server Verification Polling
        poll_start = time.time()
        final_video_state = None

        while time.time() - poll_start < poll_timeout_seconds:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("تم إلغاء عملية المراقبة.")

            time.sleep(1.5)
            s_res = client.get(f"{base_url}/studio/packages/{active_session_id}", headers=headers)
            if s_res.status_code == 200:
                s_info = s_res.json()
                s_status = s_info.get("status")

                if progress_callback:
                    progress_callback({
                        "stage": "server_verifying",
                        "server_status": s_status,
                        "session_id": str(active_session_id),
                        "video_id": str(target_video_id),
                        "error_message": s_info.get("error_message"),
                        "upload_progress_pct": 100.0,
                        "message": (
                            "جاري التحقق من التجزئات وقوائم التشغيل..." if s_status == "VERIFYING"
                            else ("جاري ترقية الجيل إلى وضع النشر..." if s_status == "PROMOTING"
                            else ("في طابور التحقق بالخادم..." if s_status == "QUEUED_FOR_VERIFICATION"
                            else s_status))
                        ),
                    })

                if s_status == "READY":
                    final_video_state = s_info
                    break
                elif s_status == "FAILED":
                    raise RuntimeError(f"فشل التحقق من الحزمة على الخادم: {s_info.get('error_message')}")
            else:
                pass

        if not final_video_state:
            raise TimeoutError("تجاوز خادم ABOODVIDEO مهلة التحقق من الحزمة المحددة.")

        if progress_callback:
            progress_callback({
                "stage": "completed",
                "server_status": "READY",
                "video_id": str(target_video_id),
                "session_id": str(active_session_id),
                "upload_progress_pct": 100.0,
                "message": "تم اعتماد الفيديو ونشره بنجاح وهو الآن جاهز للعرض!",
            })

        return {
            "success": True,
            "session_id": str(active_session_id),
            "video_id": str(target_video_id),
            "status": "READY",
            "server_url": base_url,
        }

    finally:
        if manage_client:
            client.close()
