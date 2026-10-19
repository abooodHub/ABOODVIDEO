"""Unified Job Runner Pipeline Orchestrator for ABOODVIDEO Studio."""
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

from studio.config import FONTS_DIR, load_studio_config
from studio.core.encoder import transcode_package_renditions
from studio.core.fonts import parse_ass_fonts, package_required_fonts
from studio.core.packager import build_and_verify_package
from studio.core.probe import probe_video
from studio.core.subtitles import process_softsub_file, extract_embedded_subtitle
from studio.core.uploader import upload_and_verify_package
from studio.core.output_paths import single_output_directory
from studio.core.subtitle_metadata import normalize_subtitle


class StudioJob:
    """Manages the full lifecycle of a single video transcoding & upload job."""

    def __init__(
        self,
        video_path: str,
        title: str,
        selected_qualities: List[str],
        encoder_choice: str = "auto",
        subtitle_mode: str = "none",
        subtitle_path: Optional[str] = None,
        embedded_subtitle_index: Optional[int] = None,
        hardsub_style: Optional[Dict[str, Any]] = None,
        subtitle_lang: str = "ar",
        subtitle_label: str = "العربية",
        server_url: str = "http://localhost:8000/api/v1",
        api_key: str = "",
        video_id: Optional[str] = None,
        auto_upload: bool = True,
    ):
        self.job_id = str(uuid.uuid4())
        self.video_path = Path(video_path).resolve()
        self.title = title or self.video_path.stem
        self.selected_qualities = selected_qualities
        self.encoder_choice = encoder_choice
        self.subtitle_mode = subtitle_mode
        self.subtitle_path = Path(subtitle_path).resolve() if subtitle_path else None
        self.embedded_subtitle_index = embedded_subtitle_index
        self.hardsub_style = hardsub_style
        self.subtitle_lang = subtitle_lang
        self.subtitle_label = subtitle_label
        self.server_url = server_url
        self.api_key = api_key
        self.video_id = video_id
        self.auto_upload = auto_upload

        self.cancel_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

        self.state: Dict[str, Any] = {
            "job_id": self.job_id,
            "status": "idle",
            "stage_description": "جاهز للبدء",
            "progress_pct": 0.0,
            "current_rendition": None,
            "fps": 0.0,
            "speed": "0x",
            "eta": "00:00:00",
            "error": None,
            "result": None,
        }

        self.listeners: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self.listeners.append(callback)

    def _broadcast(self, update: Dict[str, Any]) -> None:
        self.state.update(update)
        for listener in self.listeners:
            try:
                listener(self.state)
            except Exception:
                pass

    def cancel(self) -> None:
        self.cancel_event.set()
        self._broadcast({
            "status": "cancelled",
            "stage_description": "تم إلغاء المهمة من قبل المستخدم.",
        })

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            cfg = load_studio_config()
            work_dir = single_output_directory(
                cfg,
                source_path=self.video_path,
                title=self.title,
                job_id=self.job_id,
            )
            work_dir.mkdir(parents=True, exist_ok=True)
            # 1. Probing
            self._broadcast({
                "status": "probing",
                "stage_description": "جاري فحص مواصفات الفيديو...",
                "progress_pct": 2.0,
            })
            probe_data = probe_video(self.video_path)
            duration = probe_data["duration_seconds"]

            # 1b. Extract embedded subtitle if selected
            if self.subtitle_mode != "none" and self.embedded_subtitle_index is not None:
                self._broadcast({
                    "status": "probing",
                    "stage_description": f"جاري استخراج الترجمة المضمنة (مسار {self.embedded_subtitle_index})...",
                    "progress_pct": 4.0,
                })
                extracted_subs_dir = work_dir / "extracted_subs"
                try:
                    ext_path, ext_fmt = extract_embedded_subtitle(
                        video_path=self.video_path,
                        stream_index=self.embedded_subtitle_index,
                        output_dir=extracted_subs_dir,
                        language_code=self.subtitle_lang,
                    )
                    self.subtitle_path = ext_path
                except Exception as extract_err:
                    raise RuntimeError(f"فشل استخراج الترجمة المضمنة: {extract_err}")

            # 2. Transcoding
            self._broadcast({
                "status": "transcoding",
                "stage_description": "جاري معالجة الجودات وتوليد مقاطع HLS...",
                "progress_pct": 5.0,
            })

            def on_transcode_progress(p_data: Dict[str, Any]):
                if p_data.get("stage") == "transcoding":
                    mapped_pct = 5.0 + (p_data["overall_progress_pct"] * 0.70)
                    self._broadcast({
                        "status": "transcoding",
                        "stage_description": f"معالجة جودة {p_data.get('current_rendition')} ({p_data.get('rendition_index')} من {p_data.get('total_renditions')})",
                        "progress_pct": round(mapped_pct, 1),
                        "current_rendition": p_data.get("current_rendition"),
                        "fps": p_data.get("fps", 0.0),
                        "speed": p_data.get("speed", "0x"),
                        "eta": p_data.get("eta_formatted", "00:00:00"),
                        "hardware": p_data.get("hardware"),
                    })

            transcode_res = transcode_package_renditions(
                source_path=self.video_path,
                output_dir=work_dir,
                selected_qualities=self.selected_qualities,
                encoder_choice=self.encoder_choice,
                subtitle_mode=self.subtitle_mode,
                subtitle_path=self.subtitle_path,
                fonts_dir=FONTS_DIR,
                hardsub_style=self.hardsub_style,
                progress_callback=on_transcode_progress,
                cancel_event=self.cancel_event,
                preset=cfg.get("default_preset", "balanced"),
                include_360p=bool(cfg.get("include_360p", False)),
            )

            # 3. Handle True ASS Softsub & Font Packaging
            declared_subtitles = []
            packaged_fonts = []

            if self.subtitle_mode == "softsub" and self.subtitle_path and self.subtitle_path.exists():
                self._broadcast({
                    "status": "packaging",
                    "stage_description": "جاري معالجة الترجمة وتجهيز أصول الخطوط...",
                    "progress_pct": 76.0,
                })

                sub_res = process_softsub_file(
                    subtitle_input_path=self.subtitle_path,
                    target_subs_dir=work_dir / "subtitles",
                    language_code=self.subtitle_lang,
                )

                sub_fonts_rel: List[str] = []
                if sub_res["format"] in ("ass", "ssa"):
                    # Scan required fonts
                    req_fonts, _ = parse_ass_fonts(sub_res["primary_abs_path"])
                    # Package required fonts with SHA-256 deduplication
                    packaged_fonts = package_required_fonts(
                        required_fonts=req_fonts,
                        target_fonts_dir=work_dir / "fonts",
                        mkv_path=self.video_path,
                    )
                    sub_fonts_rel = [f["path"] for f in packaged_fonts]

                subtitle_metadata = normalize_subtitle(
                    language=self.subtitle_lang,
                    fmt=sub_res["format"],
                    title=self.subtitle_label,
                    default=True,
                    path=self.subtitle_path,
                )
                declared_subtitles.append({
                    "language_code": subtitle_metadata["language_code"],
                    "language_name": subtitle_metadata["language_name"],
                    "label": subtitle_metadata["label"],
                    "is_default": True,
                    "path": sub_res["primary_rel_path"],
                    "format": sub_res["format"],
                    "fonts": sub_fonts_rel,
                    "auxiliary_files": sub_res.get("auxiliary_files", []),
                })

            # 4. Packaging and Manifest generation
            self._broadcast({
                "status": "packaging",
                "stage_description": "جاري التحقق من سلامة الحزمة وحساب بصمات SHA-256...",
                "progress_pct": 80.0,
            })

            package_meta = build_and_verify_package(
                package_dir=work_dir,
                title=self.title,
                duration_seconds=duration,
                renditions=transcode_res["renditions"],
                subtitles=declared_subtitles,
                fonts=packaged_fonts,
            )

            manifest = package_meta["manifest"]

            if not self.auto_upload or not self.api_key:
                self._broadcast({
                    "status": "completed",
                    "stage_description": "اكتمل تجهيز الحزمة والتحقق منها محلياً بنجاح.",
                    "progress_pct": 100.0,
                    "result": {
                        "package_dir": str(work_dir),
                        "manifest": manifest,
                        "total_files": package_meta["total_files"],
                        "total_bytes": package_meta["total_bytes"],
                    },
                })
                return

            # 5. Upload & Server Verification
            self._broadcast({
                "status": "uploading",
                "stage_description": "جاري رفع الحزمة إلى خادم ABOODVIDEO...",
                "progress_pct": 82.0,
            })

            def on_upload_progress(u_data: Dict[str, Any]):
                u_stage = u_data.get("stage")
                if u_stage == "uploading_files":
                    u_pct = u_data.get("upload_progress_pct", 0.0)
                    mapped_pct = 82.0 + (u_pct * 0.13)
                    self._broadcast({
                        "status": "uploading",
                        "stage_description": f"رفع الملفات ({u_data.get('file_index')}/{u_data.get('total_files')}): {u_data.get('current_file')}",
                        "progress_pct": round(mapped_pct, 1),
                        "speed": f"{u_data.get('upload_speed_mbps')} Mbps",
                        "eta": u_data.get("eta_formatted", "00:00:00"),
                    })
                elif u_stage == "server_verifying":
                    self._broadcast({
                        "status": "verifying",
                        "stage_description": f"الخادم يتحقق من الحزمة: {u_data.get('message')}",
                        "progress_pct": 96.0,
                        "server_status": u_data.get("server_status"),
                    })

            upload_res = upload_and_verify_package(
                server_url=self.server_url,
                api_key=self.api_key,
                package_dir=work_dir,
                manifest=manifest,
                video_id=self.video_id,
                progress_callback=on_upload_progress,
                cancel_event=self.cancel_event,
            )

            # 6. Complete Success
            self._broadcast({
                "status": "completed",
                "stage_description": "تم التحقق من الحزمة ونشر الفيديو بنجاح على الخادم (READY)!",
                "progress_pct": 100.0,
                "result": upload_res,
            })

        except Exception as e:
            if not self.cancel_event.is_set():
                self._broadcast({
                    "status": "failed",
                    "stage_description": f"حدث خطأ أثناء المعالجة: {str(e)}",
                    "error": str(e),
                })
