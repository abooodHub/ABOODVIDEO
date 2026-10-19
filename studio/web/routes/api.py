"""Local REST & WebSocket API routes for ABOODVIDEO Studio UI."""
import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, File, UploadFile, HTTPException, WebSocket, WebSocketDisconnect, Request
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from studio.config import (
    load_studio_config, save_studio_config, validate_converted_output_folder,
    get_converted_output_root, TEMP_DIR, USER_DATA_DIR, VERSION,
)
from studio.core.hardware import get_full_telemetry, benchmark_encoder, detect_encoder_capabilities
from studio.core.probe import probe_video
from studio.core.fonts import (
    analyze_ass_fonts,
    attach_font_file,
    list_attached_fonts,
    import_fonts_from_zip,
    get_font_library_summary,
)
from studio.core.uploader import test_connection
from studio.core.job_runner import StudioJob
from studio.core.picker import select_video_file, select_subtitle_file, select_folder

router = APIRouter(prefix="/api")

# Active jobs registry
active_jobs: Dict[str, StudioJob] = {}
current_active_job: Optional[StudioJob] = None


class ProbeRequest(BaseModel):
    video_path: str


class FontsAnalyzeRequest(BaseModel):
    ass_path: str
    video_path: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    server_url: Optional[str] = None
    api_key: Optional[str] = None
    default_encoder: Optional[str] = None
    hls_segment_duration: Optional[int] = None
    auto_cleanup_package: Optional[bool] = None
    encoder_preference: Optional[str] = None
    processing_mode: Optional[str] = None
    converted_output_folder: Optional[str] = None
    font_library_folder: Optional[str] = None
    output_organization: Optional[str] = None
    cpu_concurrent_jobs: Optional[int] = None
    gpu_concurrent_jobs: Optional[int] = None
    default_preset: Optional[str] = None
    include_360p: Optional[bool] = None
    first_run_complete: Optional[bool] = None



class TestConnectionRequest(BaseModel):
    server_url: str
    api_key: str = ""


class EncoderTestRequest(BaseModel):
    encoder: str = "auto"


class ClearTempRequest(BaseModel):
    confirmed: bool = False


class StartJobRequest(BaseModel):
    video_path: str
    title: Optional[str] = None
    selected_qualities: List[str]
    encoder_choice: str = "auto"
    subtitle_mode: str = "none"
    subtitle_path: Optional[str] = None
    embedded_subtitle_index: Optional[int] = None
    hardsub_style: Optional[Dict[str, Any]] = None
    subtitle_lang: str = "ar"
    subtitle_label: str = "العربية"
    server_url: Optional[str] = None
    api_key: Optional[str] = None
    video_id: Optional[str] = None
    auto_upload: bool = True


def _require_localhost(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Localhost access only")


@router.get("/hardware")
async def get_hardware_status():
    """Get live CPU, GPU, and RAM telemetry."""
    return await run_in_threadpool(get_full_telemetry)


@router.get("/config")
async def get_config():
    """Get Studio configuration."""
    cfg = load_studio_config()
    public = dict(cfg)
    public.pop("api_key", None)
    public["has_api_key"] = bool(cfg.get("api_key"))
    public["requires_output_folder"] = not bool(cfg.get("converted_output_folder"))
    from studio.config import DEFAULT_DB_PATH, LOGS_DIR, TEMP_DIR
    public["appdata_root"] = str(USER_DATA_DIR.resolve())
    public["jobs_db_path"] = str(DEFAULT_DB_PATH.resolve())
    public["logs_path"] = str(LOGS_DIR.resolve())
    public["temp_root"] = str(TEMP_DIR.resolve())
    return public


@router.post("/config")
async def update_config(req: ConfigUpdateRequest):
    """Update Studio configuration."""
    update_data = {k: v for k, v in req.dict().items() if v is not None}
    if "converted_output_folder" in update_data:
        output = validate_converted_output_folder(update_data["converted_output_folder"])
        update_data["converted_output_folder"] = str(output)
        update_data["first_run_complete"] = True
    if "font_library_folder" in update_data:
        font_root = Path(update_data["font_library_folder"]).expanduser().resolve()
        if not font_root.is_dir():
            raise HTTPException(status_code=400, detail="مجلد الخطوط المحدد غير موجود.")
        update_data["font_library_folder"] = str(font_root)
    if update_data.get("processing_mode") not in (None, "local", "server"):
        raise HTTPException(status_code=400, detail="وضع المعالجة غير صالح.")
    if update_data.get("output_organization") not in (None, "automatic", "original"):
        raise HTTPException(status_code=400, detail="تنظيم الإخراج غير صالح.")
    if update_data.get("default_preset") not in (None, "fast", "balanced", "quality"):
        raise HTTPException(status_code=400, detail="إعداد الجودة غير صالح.")
    for key in ("cpu_concurrent_jobs", "gpu_concurrent_jobs"):
        if key in update_data and not 1 <= int(update_data[key]) <= 4:
            raise HTTPException(status_code=400, detail="عدد المهام المتزامنة يجب أن يكون بين 1 و4.")
    save_studio_config(update_data)
    return {"ok": True, "config": await get_config()}


@router.post("/test-connection")
async def test_server_connection(req: TestConnectionRequest):
    """Test connection and authentication to ABOODVIDEO server."""
    key = req.api_key.strip() or load_studio_config().get("api_key", "")
    return await run_in_threadpool(test_connection, req.server_url, key, host_header=load_studio_config().get("server_host_header"))


@router.get("/server/status")
async def get_server_status():
    """Return public health/readiness plus authenticated Studio capability status."""
    import httpx
    cfg = load_studio_config()
    api_base = str(cfg.get("server_url") or "").rstrip("/")
    origin = api_base[:-7] if api_base.endswith("/api/v1") else api_base
    result: Dict[str, Any] = {"connected": False, "health": None, "readiness": None, "studio_version": VERSION}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            headers = {"Host": cfg["server_host_header"]} if cfg.get("server_host_header") else {}
            health = await client.get(f"{origin}/health", headers=headers)
            readiness = await client.get(f"{origin}/readiness", headers=headers)
        result.update({"connected": health.status_code == 200 and readiness.status_code == 200, "health": health.status_code, "readiness": readiness.status_code})
    except Exception as exc:
        result["error"] = str(exc)
    if cfg.get("api_key"):
        auth = await run_in_threadpool(test_connection, api_base, cfg["api_key"], host_header=cfg.get("server_host_header"))
        result["authenticated"] = bool(auth.get("ok"))
        result["scopes"] = auth.get("scopes", [])
    else:
        result["authenticated"] = False
        result["scopes"] = []
    return result


@router.post("/system/select-video")
async def system_select_video(request: Request):
    """Opens native Windows file picker for video selection (127.0.0.1 only)."""
    _require_localhost(request)
    path = await run_in_threadpool(select_video_file)
    if not path:
        return {"cancelled": True, "path": ""}
    return {"cancelled": False, "path": str(Path(path).resolve())}


@router.post("/system/select-subtitle")
async def system_select_subtitle(request: Request):
    """Opens native Windows file picker for subtitle selection (127.0.0.1 only)."""
    _require_localhost(request)
    path = await run_in_threadpool(select_subtitle_file)
    if not path:
        return {"cancelled": True, "path": ""}
    return {"cancelled": False, "path": str(Path(path).resolve())}


@router.post("/system/select-folder")
async def system_select_folder(request: Request):
    """Opens native Windows folder picker (127.0.0.1 only)."""
    _require_localhost(request)
    path = await run_in_threadpool(select_folder)
    if not path:
        return {"cancelled": True, "path": ""}
    return {"cancelled": False, "path": str(Path(path).resolve())}


@router.post("/system/select-output-folder")
async def system_select_output_folder(request: Request):
    _require_localhost(request)
    path = await run_in_threadpool(select_folder, title="اختر مجلد حفظ النسخ المحوّلة")
    if not path:
        return {"cancelled": True, "path": ""}
    resolved = validate_converted_output_folder(path)
    save_studio_config({"converted_output_folder": str(resolved), "first_run_complete": True})
    return {"cancelled": False, "path": str(resolved)}


@router.post("/system/open-output-folder")
async def system_open_output_folder(request: Request):
    _require_localhost(request)
    path = get_converted_output_root(require_configured=True)
    if os.name == "nt":
        os.startfile(str(path))
    return {"ok": True, "path": str(path)}


@router.post("/system/select-font-folder")
async def system_select_font_folder(request: Request):
    """Open a native folder picker for the external font library."""
    _require_localhost(request)
    path = await run_in_threadpool(select_folder, title="اختر مجلد الخطوط الدائم")
    if not path:
        return {"cancelled": True, "path": ""}
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="مجلد الخطوط المحدد غير موجود.")
    return {"cancelled": False, "path": str(resolved)}


@router.post("/system/open-font-folder")
async def system_open_font_folder(request: Request):
    _require_localhost(request)
    config = load_studio_config()
    path = Path(str(config.get("font_library_folder") or "")).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="مجلد الخطوط المحدد غير موجود.")
    if os.name == "nt":
        os.startfile(str(path))
    return {"ok": True, "path": str(path)}


def _directory_stats(root: Path) -> Dict[str, int]:
    count = 0
    size = 0
    if root.is_dir():
        for item in root.rglob("*"):
            if item.is_file() and not item.is_symlink():
                count += 1
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
    return {"file_count": count, "size_bytes": size}


@router.get("/system/temp-info")
async def system_temp_info():
    return {"path": str(TEMP_DIR), **(await run_in_threadpool(_directory_stats, TEMP_DIR))}


@router.post("/system/clear-temp")
async def system_clear_temp(req: ClearTempRequest, request: Request):
    _require_localhost(request)
    if not req.confirmed:
        raise HTTPException(status_code=400, detail="يلزم تأكيد حذف الملفات المؤقتة.")
    root = TEMP_DIR.resolve()
    if root == USER_DATA_DIR.resolve() or USER_DATA_DIR.resolve() not in root.parents:
        raise HTTPException(status_code=409, detail="مسار الملفات المؤقتة غير آمن.")
    before = await run_in_threadpool(_directory_stats, root)
    for child in list(root.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child)
    return {"ok": True, "removed_files": before["file_count"], "removed_bytes": before["size_bytes"]}


@router.get("/encoders")
async def list_encoders(force_refresh: bool = False):
    return {"encoders": await run_in_threadpool(detect_encoder_capabilities, force_refresh)}


@router.post("/encoder/test")
async def test_encoder(req: EncoderTestRequest):
    try:
        return await run_in_threadpool(benchmark_encoder, req.encoder)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/browse-video")
async def browse_video(request: Request):
    """Opens native Windows file picker for video selection (127.0.0.1 only)."""
    return await system_select_video(request)


@router.post("/browse-subtitle")
async def browse_subtitle(request: Request):
    """Opens native Windows file picker for subtitle selection (127.0.0.1 only)."""
    return await system_select_subtitle(request)


@router.post("/probe")
async def probe_media(req: ProbeRequest):
    """Probe video file and return complete specifications."""
    try:
        p = Path(req.video_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail="ملف الفيديو غير موجود في المسار المحدد.")
        return await run_in_threadpool(probe_video, p)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/fonts/analyze")
async def analyze_fonts(req: FontsAnalyzeRequest):
    """Audit fonts in ASS file against Windows system, Studio attached fonts, and MKV attachments."""
    try:
        p = Path(req.ass_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail="ملف الترجمة ASS غير موجود.")
        mkv_p = Path(req.video_path) if req.video_path else None
        return await run_in_threadpool(analyze_ass_fonts, p, mkv_p)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/fonts")
async def get_attached_fonts():
    """List all custom attached fonts stored in Studio."""
    return await run_in_threadpool(list_attached_fonts)


@router.get("/font-library")
async def font_library_status():
    return await run_in_threadpool(get_font_library_summary)


@router.post("/font-library/rescan")
async def rescan_font_library():
    return await run_in_threadpool(get_font_library_summary, force_refresh=True)


@router.post("/fonts/upload")
async def upload_font(file: UploadFile = File(...)):
    """Upload a TTF/OTF font file into Studio fonts folder."""
    try:
        content = await file.read()
        res = await run_in_threadpool(attach_font_file, file.filename, content)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/fonts/upload-multiple")
async def upload_multiple_fonts(files: List[UploadFile] = File(...)):
    """Upload multiple TTF/OTF font files into Studio fonts folder."""
    results = []
    for f in files:
        try:
            content = await f.read()
            res = await run_in_threadpool(attach_font_file, f.filename, content)
            results.append(res)
        except Exception as e:
            results.append({"filename": f.filename, "error": str(e)})
    return {"total": len(files), "results": results}


@router.post("/fonts/import-zip")
async def import_zip_fonts(file: UploadFile = File(...)):
    """Safely extracts TTF/OTF fonts from an uploaded ZIP archive."""
    try:
        tmp_zip = Path(TEMP_DIR) / f"upload_{file.filename}"
        content = await file.read()
        tmp_zip.write_bytes(content)
        imported = await run_in_threadpool(import_fonts_from_zip, tmp_zip)
        tmp_zip.unlink(missing_ok=True)
        return {"imported_count": len(imported), "fonts": imported}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"فشل استيراد الخطوط من ملف ZIP: {str(e)}")


class FontsImportFolderRequest(BaseModel):
    folder_path: str


@router.post("/fonts/import-folder")
async def import_folder_fonts(req: FontsImportFolderRequest, request: Request):
    """Safely copies TTF/OTF fonts from a local directory into Studio fonts (127.0.0.1 only)."""
    _require_localhost(request)
    src = Path(req.folder_path)
    if not src.exists() or not src.is_dir():
        raise HTTPException(status_code=404, detail="مجلد الخطوط غير موجود.")

    imported = []
    for ext in ("*.ttf", "*.otf", "*.ttc"):
        for fp in src.glob(ext):
            try:
                data = fp.read_bytes()
                attach_font_file(fp.name, data)
                imported.append(fp.name)
            except Exception:
                pass
    return {"imported_count": len(imported), "fonts": imported}


@router.post("/jobs/start")
async def start_job(req: StartJobRequest):
    """Start a transcoding & upload job."""
    global current_active_job
    cfg = load_studio_config()
    if cfg.get("processing_mode", "local") != "local":
        raise HTTPException(status_code=409, detail="هذه العملية للحزمة المحلية. اختر المعالجة المحلية أو استخدم شاشة الرفع للخادم.")
    get_converted_output_root(require_configured=True)

    server_url = req.server_url or cfg.get("server_url")
    api_key = req.api_key or cfg.get("api_key")

    job = StudioJob(
        video_path=req.video_path,
        title=req.title or Path(req.video_path).stem,
        selected_qualities=req.selected_qualities,
        encoder_choice=req.encoder_choice,
        subtitle_mode=req.subtitle_mode,
        subtitle_path=req.subtitle_path,
        embedded_subtitle_index=req.embedded_subtitle_index,
        hardsub_style=req.hardsub_style,
        subtitle_lang=req.subtitle_lang,
        subtitle_label=req.subtitle_label,
        server_url=server_url,
        api_key=api_key,
        video_id=req.video_id,
        auto_upload=req.auto_upload,
    )

    active_jobs[job.job_id] = job
    current_active_job = job
    job.start()

    return {"job_id": job.job_id, "status": job.state["status"]}


@router.get("/jobs/{job_id}/status")
async def get_job_status(job_id: str):
    """Poll job status."""
    job = active_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="المهمة غير موجودة.")
    return job.state


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel running job."""
    job = active_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="المهمة غير موجودة.")
    job.cancel()
    return {"ok": True, "job_id": job_id}


@router.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket):
    """Streams live hardware telemetry, active single-video job, and season batch telemetry over WebSocket.

    Adaptive polling: 1s when active encode/upload, 3s when idle.
    """
    from studio.core.batch_runner import batch_runner
    from studio.web.local_security import local_request_error
    if local_request_error(websocket.headers, scheme=websocket.url.scheme, mutating=True):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            hw = await run_in_threadpool(get_full_telemetry)
            batch_data = batch_runner.get_live_telemetry_payload()
            active_job_state = current_active_job.state if current_active_job else None
            payload = {
                "hardware": hw["hardware"],
                "active_job": active_job_state,
                "batch": batch_data,
            }
            await websocket.send_json(payload)
            # Adaptive sleep: fast when processing, slow when idle
            is_active = bool(
                (active_job_state and active_job_state.get("status") not in ("completed", "failed", "cancelled", None))
                or (batch_data and batch_data.get("is_running"))
            )
            await asyncio.sleep(1.0 if is_active else 3.0)
    except (WebSocketDisconnect, Exception):
        pass


@router.get("/diagnostics/ffmpeg")
async def get_ffmpeg_diagnostic_info():
    """Returns FFmpeg and hardware encoder diagnostics."""
    from studio.core.ffmpeg_finder import get_ffmpeg_diagnostics
    return await run_in_threadpool(get_ffmpeg_diagnostics)


@router.post("/system/open-logs-folder")
async def open_logs_folder():
    """Opens %LOCALAPPDATA%\\ABOODVIDEO Studio\\logs in Windows Explorer."""
    import os
    from studio.config import LOGS_DIR
    if os.name == "nt":
        try:
            os.startfile(str(LOGS_DIR))
            return {"status": "ok", "path": str(LOGS_DIR)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "unsupported", "path": str(LOGS_DIR)}


@router.post("/system/open-data-folder")
async def open_data_folder():
    """Opens %LOCALAPPDATA%\\ABOODVIDEO Studio in Windows Explorer."""
    import os
    from studio.config import USER_DATA_DIR
    if os.name == "nt":
        try:
            os.startfile(str(USER_DATA_DIR))
            return {"status": "ok", "path": str(USER_DATA_DIR)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "unsupported", "path": str(USER_DATA_DIR)}


@router.post("/system/app-focus")
async def focus_app_window():
    """Focuses the native desktop window if running."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "ABOODVIDEO Studio")
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return {"status": "ok", "focused": True}
    except Exception:
        pass
    return {"status": "ok", "focused": False}


@router.get("/system/disk-info")
async def get_disk_info():
    """Returns disk space statistics for the user data drive."""
    import shutil
    from studio.config import USER_DATA_DIR

    def _get_disk() -> dict:
        try:
            usage = shutil.disk_usage(str(USER_DATA_DIR))

            def fmt(n: int) -> str:
                if n < 1024:
                    return f"{n} B"
                if n < 1024 ** 2:
                    return f"{n / 1024:.1f} KB"
                if n < 1024 ** 3:
                    return f"{n / 1024 ** 2:.1f} MB"
                return f"{n / 1024 ** 3:.2f} GB"

            return {
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_formatted": fmt(usage.free),
                "total_formatted": fmt(usage.total),
                "used_pct": round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0,
            }
        except Exception as ex:
            return {"free_bytes": 0, "total_bytes": 0, "free_formatted": "\u2014", "error": str(ex)}

    return await run_in_threadpool(_get_disk)


class NotifyRequest(BaseModel):
    title: str = "ABOODVIDEO Studio"
    body: str = ""


@router.post("/system/notify")
async def send_windows_notification(req: NotifyRequest):
    """Sends a Windows toast notification using dependency-free PowerShell fallback."""
    import subprocess

    def _notify() -> dict:
        title = req.title.replace("'", "").replace('"', '')
        body = req.body.replace("'", "").replace('"', '')

        # Method 1: win10toast (if available in frozen bundle)
        try:
            from win10toast import ToastNotifier  # type: ignore
            toaster = ToastNotifier()
            toaster.show_toast(req.title, req.body, duration=5, threaded=True)
            return {"ok": True, "method": "win10toast"}
        except ImportError:
            pass
        except Exception:
            pass

        # Method 2: PowerShell BurntToast (Windows 10+)
        try:
            ps_script = (
                f"[Windows.UI.Notifications.ToastNotificationManager, "
                f"Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
                f"$template = [Windows.UI.Notifications.ToastNotificationManager]::"
                f"GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                f"$template.SelectSingleNode('//text[@id=1]').InnerText = '{title}'; "
                f"$template.SelectSingleNode('//text[@id=2]').InnerText = '{body}'; "
                f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
                f"[Windows.UI.Notifications.ToastNotificationManager]::"
                f"CreateToastNotifier('ABOODVIDEO Studio').Show($toast)"
            )
            result = subprocess.run(
                ["powershell", "-NonInteractive", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=5, check=False
            )
            if result.returncode == 0:
                return {"ok": True, "method": "powershell_winrt"}
        except Exception:
            pass

        # Method 3: msg.exe fallback (silent popup)
        try:
            import os
            user = os.environ.get("USERNAME", "*")
            subprocess.run(
                ["msg", user, f"{title}: {body}"],
                capture_output=True, timeout=3, check=False
            )
            return {"ok": True, "method": "msg_exe"}
        except Exception:
            pass

        return {"ok": True, "method": "noop"}

    return await run_in_threadpool(_notify)


@router.get("/batch/recent-jobs")
async def get_recent_jobs():
    """Returns recently processed episodes from the active batch for the home landing screen."""
    from studio.core.batch_runner import batch_runner

    def _get_recent() -> dict:
        try:
            data = batch_runner.get_current_batch_data()
            episodes = data.get("episodes", [])
            jobs = [
                {
                    "filename": ep.get("filename", ""),
                    "title": ep.get("filename", ""),
                    "state": ep.get("state", "PENDING"),
                }
                for ep in episodes
            ]
            jobs.reverse()
            return {"jobs": jobs[:10]}
        except Exception:
            return {"jobs": []}

    return await run_in_threadpool(_get_recent)
