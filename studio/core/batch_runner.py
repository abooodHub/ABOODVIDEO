"""Season Batch Runner and Orchestration Shell for ABOODVIDEO Studio.

Phase 2A Scope:
- Sequential local season processing
- Reuse existing probe.py, encoder.py, packager.py, subtitles.py, fonts.py, hardware.py
- Exactly ONE active encode at any time (GPU Concurrency = 1)
- Persistent state transitions committed to SQLite jobs.db
- Safe restart recovery (conservative recovery, never re-encode READY_TO_UPLOAD)
- Batch controls: Start, Pause, Resume, Stop-after-current, Retry, Skip
- Live queue telemetry updates with throttled DB persistence
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
from contextlib import nullcontext
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from studio.config import (
    TEMP_DIR,
    load_studio_config,
    get_converted_output_root,
    validate_converted_output_folder,
)
from studio.core.process import run_headless, popen_headless
from studio.core.batch_store import (
    BatchStore,
    EpisodeState,
    EPISODE_STATE_ARABIC,
    format_bytes,
    _utc_now_iso,
    _as_dict,
    _as_list,
)
from studio.core.episode_scanner import (
    scan_season_folder, discover_season_videos, get_season_working_fonts_dir, natural_sort_key,
    audit_subtitle_and_fonts, find_season_subtitles, find_season_fonts_dir,
    parse_episode_number,
)
from studio.core.probe import probe_video
from studio.core.encoder import transcode_package_renditions, DiskSafetyPause, can_copy_2160p_video
from studio.core.packager import build_and_verify_package
from studio.core.package_cleaner import cleanup_final_package_intermediates, build_final_package_inventory
from studio.core.presets import normalize_preset_name, preset_identity, expected_output_size, selected_qualities
from studio.core.subtitles import process_softsub_file
from studio.core.fonts import (
    parse_ass_fonts, package_required_fonts, analyze_ass_fonts,
    apply_font_library_to_audit,
)
from studio.core.uploader import test_connection, upload_and_verify_package
from studio.core.output_paths import batch_output_directory, fresh_batch_output_directory
from studio.core.subtitle_metadata import normalize_subtitle
from studio.core.processing_reporter import LocalProcessingReporter

logger = logging.getLogger("studio.batch_runner")

class EncodingControlStop(RuntimeError):
    """A user pause/shutdown revoked permission before the next FFmpeg launch."""


_QUALITY_HEIGHTS = {"2160p": 2160, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}


def _job_preset_snapshot(episode: Dict[str, Any], preset: Any, include_360p: bool) -> Dict[str, Any]:
    name = normalize_preset_name(preset)
    identity = preset_identity(name, include_360p)
    probe = {
        "width": episode.get("source_width", 0), "height": episode.get("source_height", 0),
        "video_codec": episode.get("source_video_codec", ""),
        "video_profile": episode.get("source_video_profile", ""),
        "video_level": episode.get("source_video_level", 0),
        "pixel_format": episode.get("source_pixel_format", ""), "fps": episode.get("source_fps", 0),
    }
    four_k_copy = can_copy_2160p_video(probe)
    allowed = selected_qualities(int(episode.get("source_width") or 0), int(episode.get("source_height") or 0), include_360p)
    identity.update({key: episode.get(key) for key in (
        "source_width", "source_height", "source_video_codec", "source_video_profile",
        "source_video_level", "source_pixel_format", "source_fps", "source_video_bitrate_bps",
        "source_audio_codec", "source_audio_profile", "source_audio_channels",
        "source_audio_sample_rate", "source_audio_bitrate_bps", "source_bytes", "duration_seconds",
    ) if key in episode})
    estimate = expected_output_size(
        float(episode.get("duration_seconds") or 0), name, include_360p, allowed,
        four_k_copy=four_k_copy, source_file_bytes=int(episode.get("source_bytes") or 0),
        source_video_bitrate_bps=int(episode.get("source_video_bitrate_bps") or 0),
        source_audio_bitrate_bps=int(episode.get("source_audio_bitrate_bps") or 0),
    )
    return {**identity, "selected_qualities": allowed, "four_k_video_mode": "COPY" if four_k_copy else "TRANSCODE",
            "expected_output_bytes": estimate["estimated_output_bytes"],
            "expected_output_by_rendition_bytes": estimate["per_rendition_bytes"],
            "expected_output_is_estimate": True}


def _sha256_if_file(path_value: Any, cancel_event=None, progress_callback=None) -> Optional[str]:
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    hashed = 0
    total = path.stat().st_size
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("تم إلغاء التحقق من المصدر.")
            digest.update(chunk)
            hashed += len(chunk)
            if progress_callback:
                progress_callback(hashed, total)
    return digest.hexdigest()


class BatchRunner:
    """Manages active SeasonBatch queue, background sequential execution, and live telemetry."""

    def __init__(self, store: Optional[BatchStore] = None) -> None:
        self.store = store or BatchStore()
        self._active_batch_id: Optional[str] = None
        self._work_authorized = False
        self._studio_session_id = uuid.uuid4().hex
        self._processing_reporter = LocalProcessingReporter()
        self._processing_report_last: Dict[str, float] = {}
        self._run_authorization: Optional[tuple[str, str, str]] = None
        self._encoder_launch_lock = threading.RLock()

        # Concurrency and thread management
        self._import_lock = threading.RLock()
        self._encode_lock = threading.RLock()
        self._upload_lock = threading.RLock()
        self._upload_worker_start_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._upload_worker_thread: Optional[threading.Thread] = None
        self._worker_stop_event = threading.Event()
        self._worker_stop_event.set()
        self._current_upload_cancel_event = threading.Event()
        self._pause_requested = False
        self._stop_after_current_requested = False
        self._single_episode_target_id: Optional[str] = None
        self._current_cancel_event = threading.Event()

        # Phase 3B Pipelined Execution Coordination
        self.pipeline_window: int = 1
        self._pipeline_condition = threading.Condition()
        self.simulate_low_disk: bool = False
        self._execution_history: List[Dict[str, Any]] = []
        self._active_encodes_count: int = 0
        self._active_uploads_count: int = 0
        self._max_observed_encodes: int = 0
        self._max_observed_uploads: int = 0
        self._disk_io_last_sample: Optional[tuple[float, int]] = None
        self._manual_upload_batches = []
        self._execution_config = {"processing_mode": "local", "cpu_concurrent_jobs": 1, "gpu_concurrent_jobs": 1}
        self._active_processing_ids = set()
        self._episode_cancel_events = {}
        self._state_lock = threading.RLock()
        self._resource_locks = {kind: threading.BoundedSemaphore(1) for kind in ("cpu", "gpu")}
        self._server_url: Optional[str] = None
        self._api_key: Optional[str] = None
        self._http_client: Optional[Any] = None

        # In-memory telemetry cache for live UI streaming (ep_id -> dict)
        self._telemetry: Dict[str, Dict[str, Any]] = {}
        self._measured_rendition_speeds: Dict[str, float] = {}
        self._completed_renditions: set[tuple[str, str]] = set()
        self._initial_transcode_estimate_seconds: Optional[float] = None
        self._current_processing_ep_id: Optional[str] = None
        self._current_uploading_ep_id: Optional[str] = None

        self.reload_state()
        self._report_ready_local_episodes()

    def _report_ready_local_episodes(self) -> None:
        """Publish a bounded startup snapshot so already-converted episodes appear."""
        try:
            ready_states = {EpisodeState.READY_TO_UPLOAD.value, EpisodeState.READY.value}
            episodes = [ep for ep in self.store.list_all_episodes() if ep.get("state") in ready_states]
            for ep in episodes[-200:]:
                self._report_local_processing(str(ep.get("id") or ""), str(ep.get("state") or ""))
        except Exception as exc:
            logger.debug("Could not publish ready episode snapshot (%s)", type(exc).__name__)

    def _report_local_processing(self, episode_id: str, state: Optional[str] = None) -> None:
        """Send path-free local processing telemetry without blocking encoding."""
        now = time.monotonic()
        terminal = state in {"READY_TO_UPLOAD", "READY", "FAILED", "CANCELLED", "NEEDS_REVIEW"}
        if not terminal and now - self._processing_report_last.get(episode_id, 0.0) < 3.0:
            return
        self._processing_report_last[episode_id] = now
        try:
            cfg = load_studio_config()
            server_url = str(cfg.get("server_url") or "").strip()
            api_key = str(cfg.get("api_key") or "").strip()
            if not server_url or not api_key:
                return
            ep = self.store.get_episode(episode_id) or {}
            batch = self.store.get_batch(str(ep.get("batch_id") or "")) or {}
            telemetry = self._telemetry.get(episode_id, {})
            stage = str(telemetry.get("current_stage") or ep.get("current_stage") or state or ep.get("state") or "")[:48]
            season = re.search(r"(?:season|الموسم)\s*0*(\d+)", str(batch.get("destination_label") or batch.get("season_folder_path") or ""), re.I)
            identity = _as_dict(ep.get("settings"))
            detected_season = identity.get("season_number")
            season_value = int(detected_season) if str(detected_season or "").isdigit() else int(season.group(1)) if season else None
            episode_no = ep.get("episode_number") or ep.get("order_index")
            source_name = Path(str(ep.get("filename") or "")).name
            source_title = Path(source_name).stem if source_name else ""
            display_label = str(ep.get("display_label") or "").replace("\\", "/").split("/")[-1].strip()
            title = " - ".join(part for part in (display_label, source_title) if part)[:240]
            title = title or f"Episode {episode_no or ''}"
            payload = {
                "studio_id": self._studio_session_id,
                "episode_id": str(episode_id),
                "title": title,
                "batch_name": Path(str(batch.get("destination_label") or "")).name[:160] or None,
                "season_number": season_value,
                "episode_number": int(episode_no) if str(episode_no or "").isdigit() else None,
                "status": str(state or ep.get("state") or "PROBING").upper(),
                "stage": stage or None,
                "quality": str(telemetry.get("current_quality") or "")[:16] or None,
                "progress": max(0, min(100, float(telemetry.get("progress") or ep.get("progress") or 0))),
                "fps": float(telemetry.get("fps") or ep.get("fps") or 0) or None,
                "speed": str(telemetry.get("speed") or telemetry.get("upload_speed") or ep.get("speed") or "")[:16] or None,
                "eta": str(telemetry.get("eta") or telemetry.get("upload_eta") or ep.get("eta") or "")[:24] or None,
            }
            self._processing_reporter.report(server_url, api_key, cfg.get("server_host_header"), payload)
        except Exception as exc:
            logger.debug("Could not prepare local processing heartbeat (%s)", type(exc).__name__)

    def reload_state(self) -> None:
        """Reload persisted queue state without granting processing authorization.

        Persisted state never grants execution permission. Pause every ACTIVE batch,
        recover all transient episode states (including those in a PAUSED batch),
        and leave every worker stopped until an explicit action in this process.
        """
        self._work_authorized = False
        self._run_authorization = None
        batches = self.store.list_batches()
        for batch in batches:
            if batch.get("status") == "ACTIVE":
                self.store.update_batch(batch["id"], status="PAUSED", pause_requested=0)

        # Recovery is state-based, not conditional on the batch flag. A stale
        # ENCODING/UPLOADING episode inside a PAUSED batch is still interrupted.
        for batch in batches:
            self._recover_transient_states(batch_id=batch["id"])

        latest = self.store.get_latest_active_batch()
        if latest:
            self._active_batch_id = latest["id"]
            logger.info("Reattached to existing persistent batch: %s (%s)", self._active_batch_id, latest.get("season_folder_path"))
        else:
            self._active_batch_id = None
        logger.info("STARTUP_PASSIVE batches=%d authorization=EMPTY workers=STOPPED", len(batches))

    def _recover_transient_states(self, batch_id: Optional[str] = None) -> None:
        """Conservative recovery for episodes interrupted by an unexpected Studio termination.
        - READY_TO_UPLOAD: kept intact, never reprocessed.
        - Transient states (PROBING, ENCODING, PACKAGING, VERIFYING_LOCAL):
          verified against on-disk manifest if available, otherwise moved to recoverable FAILED state.
        """
        target_batch_id = batch_id or self._active_batch_id
        if not target_batch_id:
            return

        episodes = self.store.get_episodes(target_batch_id)
        encoding_states = {
            EpisodeState.PROBING.value,
            EpisodeState.READY_TO_ENCODE.value,
            EpisodeState.ENCODING.value,
            EpisodeState.PACKAGING.value,
            EpisodeState.VERIFYING_LOCAL.value,
        }
        upload_states = {EpisodeState.UPLOADING.value, EpisodeState.SERVER_VERIFYING.value}

        for ep in episodes:
            # Episodes that reached READY or had their packages cleaned are never touched
            if ep.get("state") == EpisodeState.READY.value or ep.get("local_package_cleaned"):
                continue
            if ep["state"] in upload_states:
                work_dir_str = ep.get("work_dir")
                manifest_file = Path(work_dir_str) / "manifest.json" if work_dir_str else None
                try:
                    package_ready = bool(manifest_file and manifest_file.is_file())
                    if package_ready:
                        with manifest_file.open("r", encoding="utf-8") as stream:
                            metadata = json.load(stream)
                        files = metadata.get("files", [])
                        root = Path(work_dir_str).resolve()
                        package_ready = bool(files)
                        for item in files:
                            relative = Path(str(item.get("path") or "").replace("\\", "/"))
                            candidate = (root / relative).resolve()
                            if (relative.is_absolute() or ".." in relative.parts or root not in candidate.parents
                                    or not candidate.is_file() or candidate.stat().st_size != int(item.get("size_bytes") or 0)):
                                package_ready = False
                                break
                except Exception:
                    package_ready = False
                if package_ready:
                    self.store.update_episode(
                        ep["id"],
                        state=EpisodeState.READY_TO_UPLOAD.value,
                        current_stage="UPLOAD_INTERRUPTED_RESUMABLE",
                        progress=100.0,
                        last_upload_error="Upload interrupted by Studio shutdown; resume requires an explicit upload action.",
                        error=None,
                    )
                else:
                    self.store.update_episode(
                        ep["id"],
                        state=EpisodeState.FAILED.value,
                        current_stage="UPLOAD_INTERRUPTED_PACKAGE_INVALID",
                        last_upload_error="Upload interrupted and the local package is missing or invalid; automatic re-encoding is blocked.",
                        error="الرفع السابق انقطع والحزمة المحلية غير مكتملة؛ لن تتم إعادة الترميز تلقائياً.",
                    )
                logger.warning("INTERRUPTED_UPLOAD_RECOVERED episode_id=%s state=%s session_preserved=%s", ep["id"], self.store.get_episode(ep["id"])["state"], bool(ep.get("upload_session_id") or ep.get("session_id")))
                continue

            if ep["state"] in encoding_states:
                # Check if work_dir contains an intact and verified package
                work_dir_str = ep.get("work_dir")
                recovered = False
                if work_dir_str:
                    work_dir = Path(work_dir_str)
                    manifest_file = work_dir / "manifest.json"
                    if manifest_file.exists():
                        try:
                            with open(manifest_file, "r", encoding="utf-8") as f:
                                mdata = json.load(f)
                            files = mdata.get("files", [])
                            renditions = mdata.get("renditions", [])
                            valid_files = bool(files)
                            for fi in files:
                                relative = Path(str(fi.get("path") or "").replace("\\", "/"))
                                candidate = (work_dir.resolve() / relative).resolve()
                                if (relative.is_absolute() or ".." in relative.parts or work_dir.resolve() not in candidate.parents
                                        or not candidate.is_file() or candidate.stat().st_size != int(fi.get("size_bytes") or 0)):
                                    valid_files = False
                                    break
                            valid_playlists = bool(renditions) and all(
                                (work_dir / rendition.get("playlist_path", "")).is_file()
                                and "#EXT-X-ENDLIST" in (work_dir / rendition["playlist_path"]).read_text(encoding="utf-8", errors="ignore")
                                for rendition in renditions
                            )
                            valid_identity = (work_dir / "studio_package_identity.json").is_file()
                            if valid_files and valid_playlists and valid_identity:
                                # Intact package found
                                self.store.update_episode(
                                    ep["id"],
                                    state=EpisodeState.READY_TO_UPLOAD.value,
                                    current_stage="COMPLETED_LOCAL",
                                    progress=100.0,
                                    package_bytes=sum(fi.get("size_bytes", 0) for fi in files),
                                    error=None,
                                )
                                recovered = True
                        except Exception:
                            recovered = False

                if not recovered:
                    # The child encoder cannot survive Studio restart. Retain
                    # measured overall progress but clear volatile metrics;
                    # the existing Retry action can safely restart this job.
                    self.store.update_episode(
                        ep["id"],
                        state=EpisodeState.PENDING.value,
                        current_stage=EpisodeState.PENDING.value,
                        current_quality="",
                        rendition_progress=0.0,
                        fps=0.0,
                        speed="0x",
                        eta="00:00:00",
                        elapsed_seconds=0.0,
                        encoder_used="",
                        progress=min(98.0, max(0.0, float(ep.get("progress") or 0.0))),
                        error=None,
                    )

    @property
    def active_batch_id(self) -> Optional[str]:
        return self._active_batch_id

    def _worker_may_claim(self) -> bool:
        """Require persisted ACTIVE state and an explicit grant from this process session."""
        return self._run_is_authorized(self._active_batch_id)

    def _run_is_authorized(self, batch_id: Optional[str], episode_id: Optional[str] = None) -> bool:
        if not batch_id or not self._work_authorized:
            return False
        grant = self._run_authorization
        if not grant or not grant[2] or grant[0] != self._studio_session_id or grant[1] != batch_id:
            return False
        batch = self.store.get_batch(batch_id)
        allowed = bool(batch and batch.get("status") == "ACTIVE" and self._active_batch_id == batch_id)
        if not allowed:
            logger.warning(
                "Encoder launch denied: persisted batch is not ACTIVE or session grant is invalid (batch=%s, episode=%s).",
                batch_id, episode_id or "unknown",
            )
        return allowed

    def _authorized_encoder_launch(self, batch_id: str, episode_id: str, command: List[str], **kwargs: Any):
        # Pause and process creation share this lock: a launch either precedes the
        # persisted PAUSED write, or observes it and is refused.
        with self._encoder_launch_lock:
            if not self._run_is_authorized(batch_id, episode_id):
                logger.warning("Encoder process not launched: no current-session ACTIVE authorization (episode=%s).", episode_id)
                if self._pause_requested or self._worker_stop_event.is_set():
                    raise EncodingControlStop("Encoding blocked: batch is paused or this Studio session has no explicit run authorization.")
                raise RuntimeError("Encoding blocked: batch is paused or this Studio session has no explicit run authorization.")
            return popen_headless(command, **kwargs)

    def _authorized_encoder_command(self, batch_id: str, episode_id: str, command: List[str], **kwargs: Any):
        with self._encoder_launch_lock:
            if not self._run_is_authorized(batch_id, episode_id):
                logger.warning("Encoder command not run: no current-session ACTIVE authorization (episode=%s).", episode_id)
                if self._pause_requested or self._worker_stop_event.is_set():
                    raise EncodingControlStop("Encoding blocked: batch is paused or this Studio session has no explicit run authorization.")
                raise RuntimeError("Encoding blocked: batch is paused or this Studio session has no explicit run authorization.")
            return run_headless(command, **kwargs)

    @property
    def is_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def is_uploading(self) -> bool:
        return bool(self._current_uploading_ep_id) or (
            self._upload_worker_thread is not None and self._upload_worker_thread.is_alive()
        )

    @property
    def is_auto_pipeline_running(self) -> bool:
        return (
            (self._worker_thread is not None and self._worker_thread.is_alive()) or
            (self._upload_worker_thread is not None and self._upload_worker_thread.is_alive())
        )

    def _record_event(self, event_type: str, episode_id: str, order_index: int) -> None:
        """Records timestamped lifecycle event for concurrency and timeline verification."""
        self._execution_history.append({
            "event": event_type,
            "episode_id": episode_id,
            "order_index": order_index,
            "timestamp": time.time(),
            "active_encodes": self._active_encodes_count,
            "active_uploads": self._active_uploads_count,
        })

    def check_disk_space_guard(self, ep: Dict[str, Any]) -> tuple[bool, str]:
        """Validates that sufficient disk space exists before initiating an encode.
        Checks the pending episode's output estimate with a 20% reserve and 5 GiB temporary margin.
        """
        if self.simulate_low_disk:
            return False, "مساحة القرص غير كافية (محاكاة انخفاض المساحة)"

        source_bytes = ep.get("source_bytes", 0)
        if not source_bytes:
            sp = Path(ep.get("source_path", ""))
            if sp.exists():
                try:
                    source_bytes = sp.stat().st_size
                except Exception:
                    source_bytes = 0

        estimated_output = int(ep.get("output_estimate_bytes") or max(source_bytes * 2, 500 * 1024 * 1024))
        safety_buffer = 5 * 1024 * 1024 * 1024
        needed_bytes = int(estimated_output * 1.20 + safety_buffer)

        try:
            disk_root = get_converted_output_root(require_configured=False)
            usage = shutil.disk_usage(disk_root)
            if usage.free < needed_bytes:
                return (
                    False,
                    f"مساحة القرص غير كافية (المتاح: {format_bytes(usage.free)} - المطلوب: {format_bytes(needed_bytes)})",
                )
        except Exception as e:
            logger.warning("Could not query disk usage for converted output root: %s", e)

        return True, ""

    def import_season_folder(self, folder_path, destination_label="", mode="SEQUENTIAL",
                             encoder_choice=None, default_preset=None):
        return self._import_source(folder_path, "season", destination_label, encoder_choice, default_preset, mode=mode)

    def import_series_folder(self, folder_path, destination_label="", encoder_choice=None, default_preset=None):
        return self._import_source(folder_path, "series", destination_label, encoder_choice, default_preset)

    def import_media_file(self, file_path, destination_label=""):
        return self._import_source(file_path, "file", destination_label)

    def _import_source(self, path, kind, destination_label="", encoder_choice=None, default_preset=None, *, mode="SEQUENTIAL"):
        from studio.core.import_discovery import discover_import, season_number
        from studio.core.episode_scanner import _episode_label
        # Serialize discovery/persistence so two clicks cannot allocate the same output.
        with self._import_lock:
            config = load_studio_config()
            if config.get("converted_output_folder"):
                validate_converted_output_folder(config["converted_output_folder"])
            elif config.get("processing_mode", "local") != "server" and os.environ.get("ENVIRONMENT", "").casefold() != "test":
                get_converted_output_root(require_configured=True)
            discovery = discover_import(path, kind, config)
            if discovery["errors"]:
                raise ValueError("تعذر فحص جميع الملفات بأمان: " + "; ".join(e["path"] + ": " + e["message"] for e in discovery["errors"]))
            if not discovery["episodes"]:
                raise ValueError("لم يتم العثور على ملفات فيديو مدعومة.")
            all_rows = self.store.list_all_episodes()
            known = {str(Path(ep["source_path"]).resolve()).casefold() for ep in all_rows}
            selected = [e for e in discovery["episodes"] if e["source_path"].casefold() not in known]
            if not selected:
                if kind == "series":
                    raise ValueError("كل حلقات هذا المسلسل موجودة بالفعل في قائمة الانتظار.")
                result = self.get_current_batch_data()
                result["new_episode_count"] = 0
                return result
            reserved = {str(_as_dict(e.get("settings_json") or e.get("settings")).get("output_directory") or "").casefold() for e in all_rows}
            source = Path(discovery["source_path"])
            batch_path = str(source.parent if kind == "file" else source)
            existing = self.store.find_batch_by_folder(batch_path)
            order = max((int(e["order_index"]) for e in self.store.get_episodes(existing["id"])), default=0) if existing else 0
            encoder = str(encoder_choice or config.get("default_encoder") or "auto")
            preset = normalize_preset_name(default_preset or config.get("default_preset") or "balanced")
            # Allocate stable order/output identities before scanning, then commit
            # each completed scan. A later probe failure must not lose earlier work.
            planned = {}
            by_parent = {}
            for entry in selected:
                order += 1
                output = entry["output_directory"]
                if output:
                    base, suffix = Path(output), 2
                    while output.casefold() in reserved or Path(output).exists():
                        output = str(base.with_name(f"{base.name} ({suffix})"))
                        suffix += 1
                    reserved.add(output.casefold())
                planned[entry["source_path"].casefold()] = (entry, order, output)
                parent = Path(entry["source_path"]).parent
                scope = next((p for p in (parent, *parent.parents) if p.is_relative_to(Path(discovery["source_root"]))
                              and season_number(p.name) is not None), parent)
                by_parent.setdefault(scope, []).append(entry)

            batch = existing
            persisted = {}

            def persist_scan(scanned_row):
                nonlocal batch
                key = str(Path(scanned_row["source_path"]).resolve()).casefold()
                if key not in planned:
                    raise ValueError("Scanner returned an unselected source")
                entry, order, output = planned[key]
                row = dict(scanned_row)
                snapshot = _job_preset_snapshot(row, preset, True)
                row.update(order_index=order, episode_number=entry["episode_number"] if entry["episode_number"] is not None else "",
                           display_label=_episode_label(entry["episode_number"], order) if entry["episode_number"] is not None else "غير محدد",
                           output_estimate_bytes=snapshot["expected_output_bytes"])
                row["settings_json"] = {**_as_dict(row.get("settings_json")), **snapshot,
                    "import_version": 2, "encoder_choice": encoder, "default_preset": preset,
                    "processing_mode": config.get("processing_mode", "local"),
                    "series_name": discovery["series_name"],
                    "season_number": f"{entry['season_number']:02d}" if entry["season_number"] is not None else "",
                    "season_name": entry["season_name"], "source_root": discovery["source_root"],
                    "original_filename": entry["filename"], "release_tags": re.findall(r"\[([^\]]+)\]", entry["filename"]), "relative_source_path": entry["relative_source_path"],
                    "detected_episode_number": entry["episode_number"], "import_issues": entry["issues"],
                    "season_folder_path": str(next((p for p in (Path(entry["source_path"]).parent, *Path(entry["source_path"]).parents) if p.is_relative_to(Path(discovery["source_root"])) and season_number(p.name) is not None), Path(entry["source_path"]).parent)),
                    "output_root": config.get("converted_output_folder", ""), "output_directory": output,
                    "upload_destination": {"series_name": discovery["series_name"], "season_name": entry["season_name"],
                        "season_number": entry["season_number"], "episode_number": entry["episode_number"],
                        "relative_source_path": entry["relative_source_path"]}}
                if entry["issues"]:
                    row["state"] = EpisodeState.NEEDS_REVIEW.value
                    row["error"] = "; ".join(filter(None, [row.get("error"), *entry["issues"]]))
                if batch is None:
                    batch = self.store.create_batch(batch_path, destination_label=destination_label or discovery["series_name"], mode=mode)
                if key in persisted:
                    # A sibling may supply a previously missing subtitle font.
                    # Refresh its audit without allocating a second episode/output.
                    fields = {name: row[name] for name in (
                        "state", "error", "subtitle_path", "subtitle_source", "subtitle_format",
                        "subtitle_stream_index", "subtitle_label", "font_status", "missing_fonts",
                        "subtitle_candidates", "settings_json",
                    ) if name in row}
                    fields["font_audit_json"] = json.dumps(row.get("font_audit") or {}, ensure_ascii=False)
                    self.store.update_episode(persisted[key], **fields)
                else:
                    added = self.store.add_episodes(batch["id"], [row], expected_insert_count=1)
                    persisted[key] = added[0]["id"]

            try:
                for parent, entries in by_parent.items():
                    rows = scan_season_folder(parent, on_episode_scanned=persist_scan,
                                             video_files=[Path(e["source_path"]) for e in entries])
                    # Also support scanners that return records without callbacks.
                    for row in rows:
                        key = str(Path(row["source_path"]).resolve()).casefold()
                        if key not in persisted:
                            persist_scan(row)
                if len(persisted) != len(selected):
                    raise ValueError("لم يكتمل فحص جميع الملفات المحددة")
            except Exception:
                if batch is not None and not existing and not persisted:
                    self.store.delete_batch(batch["id"], expected_episode_count=0)
                # Successfully saved episodes remain paused and survive restart.
                raise
            self._active_batch_id = batch["id"]
            result = self.get_current_batch_data()
            result.update(new_episode_count=len(persisted), series_name=discovery["series_name"],
                          detected_seasons=[season["name"] for season in discovery["seasons"]],
                          skipped_episode_count=len(discovery["episodes"])-len(persisted))
            return result

    def get_current_batch_data(self) -> Dict[str, Any]:
        """Returns the full view of the current batch, merged with live in-memory telemetry."""
        if not self._active_batch_id:
            latest = self.store.get_latest_active_batch()
            if latest:
                self._active_batch_id = latest["id"]
            else:
                return {
                    "active": False,
                    "batch": None,
                    "episodes": [],
                    "summary": {},
                    "runner": self.get_runner_status(),
                }

        batch = self.store.get_batch(self._active_batch_id)
        if not batch:
            self._active_batch_id = None
            latest = self.store.get_latest_active_batch()
            if latest:
                self._active_batch_id = latest["id"]
                batch = latest
            else:
                return {
                    "active": False,
                    "batch": None,
                    "episodes": [],
                    "summary": {},
                    "runner": self.get_runner_status(),
                }

        episodes = self.store.list_all_episodes()
        def queue_sort_key(episode: Dict[str, Any]) -> tuple[Any, ...]:
            settings = _as_dict(episode.get("settings"))

            def numeric(value: Any) -> float:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return float("inf")

            series = str(settings.get("series_name") or "").casefold()
            season = str(settings.get("season_number") or "").casefold()
            return (
                series,
                numeric(season),
                season,
                numeric(episode.get("episode_number") if episode.get("episode_number") is not None else episode.get("order_index")),
                int(episode.get("order_index") or 0),
                str(episode.get("filename") or "").casefold(),
            )

        episodes.sort(key=queue_sort_key)
        for episode in episodes:
            episode.setdefault("state_arabic", EPISODE_STATE_ARABIC.get(episode.get("state"), episode.get("state", "")))
        summary = self.store.get_batch_summary(self._active_batch_id)

        # Overlay live telemetry onto episode records
        for ep in episodes:
            eid = ep["id"]
            settings = _as_dict(ep.get("settings"))
            if ep.get("encoding_elapsed_seconds") is None and settings.get("encoding_elapsed_seconds") is not None:
                try:
                    ep["encoding_elapsed_seconds"] = float(settings["encoding_elapsed_seconds"])
                except (TypeError, ValueError):
                    ep["encoding_elapsed_seconds"] = None
            if eid in self._telemetry:
                live = self._telemetry[eid]
                ep.update({
                    "progress": live.get("progress", ep.get("progress", 0.0)),
                    "rendition_progress": live.get("rendition_progress", ep.get("rendition_progress", 0.0)),
                    "fps": live.get("fps", ep.get("fps", 0.0)),
                    "speed": live.get("speed", ep.get("speed", "0x")),
                    "eta": live.get("eta", ep.get("eta", "00:00:00")),
                    "elapsed_seconds": live.get("elapsed_seconds", ep.get("elapsed_seconds", 0.0)),
                    "encoder_used": live.get("encoder_used", ep.get("encoder_used", "")),
                    "current_quality": live.get("current_quality", ep.get("current_quality", "")),
                    "current_stage": live.get("current_stage", ep.get("current_stage", "")),
                    "renditions": live.get("renditions", ep.get("renditions", [])),
                    "rendition_index": live.get("rendition_index", ep.get("rendition_index", 0)),
                    "total_renditions": live.get("total_renditions", ep.get("total_renditions", 0)),
                    "package_bytes": live.get("package_bytes", ep.get("package_bytes", 0)),
                    "package_bytes_formatted": format_bytes(live.get("package_bytes", ep.get("package_bytes", 0))),
                    "uploaded_bytes": live.get("uploaded_bytes", ep.get("uploaded_bytes", 0)),
                    "uploaded_bytes_formatted": format_bytes(live.get("uploaded_bytes", ep.get("uploaded_bytes", 0))),
                    "upload_speed": live.get("upload_speed", ep.get("upload_speed", "0 KB/s")),
                    "upload_eta": live.get("upload_eta", ep.get("upload_eta", "00:00:00")),
                    "upload_elapsed_seconds": live.get("upload_elapsed_seconds", ep.get("upload_elapsed_seconds", 0.0)),
                    "server_status": live.get("server_status", ep.get("server_status", "")),
                    "resource_warning": live.get("resource_warning", ""),
                })
                if live.get("encoding_started_at") and ep.get("state") == EpisodeState.ENCODING.value:
                    ep["encoding_elapsed_seconds"] = round(max(0.0, time.time() - float(live["encoding_started_at"])), 1)
                    ep["elapsed_seconds"] = live.get("episode_elapsed_seconds", ep["encoding_elapsed_seconds"])
                if live.get("current_stage"):
                    try:
                        valid_stage = EpisodeState(live["current_stage"]).value
                        ep["state"] = valid_stage
                        ep["state_arabic"] = EPISODE_STATE_ARABIC.get(valid_stage, valid_stage)
                    except ValueError:
                        pass
            ep.setdefault("renditions", list(_as_dict(ep.get("settings")).get("selected_qualities") or []))
            ep.setdefault("encoder_used", ep.get("encoder_used") or "")

        # Compute summary counts & telemetry for Phase 3B UI
        ready_count = sum(1 for ep in episodes if ep["state"] == EpisodeState.READY.value)
        ready_to_upload_count = sum(1 for ep in episodes if ep["state"] == EpisodeState.READY_TO_UPLOAD.value)
        uploading_count = sum(1 for ep in episodes if ep["state"] in (EpisodeState.UPLOADING.value, EpisodeState.SERVER_VERIFYING.value))
        encoding_count = sum(1 for ep in episodes if ep["state"] in (
            EpisodeState.ENCODING.value, EpisodeState.PROBING.value,
            EpisodeState.READY_TO_ENCODE.value, EpisodeState.PACKAGING.value,
            EpisodeState.VERIFYING_LOCAL.value,
        ))
        failed_count = sum(1 for ep in episodes if ep["state"] in (EpisodeState.FAILED.value, EpisodeState.NEEDS_REVIEW.value))
        skipped_count = sum(1 for ep in episodes if ep["state"] == EpisodeState.SKIPPED.value)
        pending_count = sum(1 for ep in episodes if ep["state"] == EpisodeState.PENDING.value)
        total_count = len(episodes)

        preflight = self._build_preflight_summary(episodes)

        summary.update({
            "ready_count": ready_count,
            "ready_to_upload_count": ready_to_upload_count,
            "uploading_count": uploading_count,
            "encoding_count": encoding_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "pending_count": pending_count,
            "overall_progress": round((ready_count / total_count * 100.0), 1) if total_count > 0 else 0.0,
            "preflight": preflight,
        })

        return {
            "active": True,
            "batch": batch,
            "episodes": episodes,
            "summary": summary,
            "runner": self.get_runner_status(),
        }

    def _build_preflight_summary(self, episodes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build duration-based estimates; upload/package remain explicitly unmeasured."""
        total_count = len(episodes)
        try:
            profile_path = TEMP_DIR / "nvenc_benchmark_profile.json"
            benchmark = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
        except Exception:
            benchmark = {}
        benchmark = _as_dict(benchmark)
        try:
            packaging_path = TEMP_DIR / "packaging_benchmark_profile.json"
            packaging = json.loads(packaging_path.read_text(encoding="utf-8")) if packaging_path.exists() else {}
        except Exception:
            packaging = {}
        packaging = _as_dict(packaging)
        default_include_360p = bool(load_studio_config().get("include_360p", False))
        qualities = ["2160p", "1080p", "720p", "480p", "360p"]
        speeds = {
            q: float(self._measured_rendition_speeds.get(q) or _as_dict(benchmark.get(q)).get("speed_multiplier", 0) or 0)
            for q in qualities
        }
        historical_episodes = self.store.list_all_episodes()
        try:
            from studio.core.hardware import get_full_telemetry
            current_gpu = (get_full_telemetry().get("hardware") or {}).get("gpu", {}).get("gpu_name", "unknown")
        except Exception:
            current_gpu = "unknown"
        current_encoder_choice = str(load_studio_config().get("default_encoder", "auto"))

        def historical_speed_for(episode: Dict[str, Any], quality: str) -> tuple[float, int]:
            source_resolution = f"{int(episode.get('source_width') or 0)}x{int(episode.get('source_height') or 0)}"
            samples = []
            for historical in historical_episodes:
                settings = _as_dict(historical.get("settings"))
                history = _as_dict(settings.get("processing_history"))
                successful = _as_dict(history.get("successful"))
                rendition = _as_dict(_as_dict(successful.get("renditions")).get(quality))
                profile = _as_dict(rendition.get("profile"))
                speed = float(rendition.get("speed_multiplier") or 0)
                if not successful.get("success") or speed <= 0:
                    continue
                if profile.get("gpu", "unknown") != current_gpu:
                    continue
                if profile.get("encoder_choice", successful.get("encoder_choice", "auto")) != current_encoder_choice:
                    continue
                if source_resolution != "0x0" and profile.get("source_resolution") != source_resolution:
                    continue
                if profile.get("codec", "h264") != "h264" or profile.get("subtitle_mode", "softsub") != "softsub":
                    continue
                samples.append((historical.get("updated_at", ""), speed))
            samples.sort(key=lambda sample: sample[0], reverse=True)
            if not samples:
                return 0.0, 0
            weights = [0.85 ** index for index in range(len(samples))]
            speed = sum(item[1] * weight for item, weight in zip(samples, weights)) / sum(weights)
            return speed, len(samples)

        history_sample_count = 0
        for quality in qualities:
            matching_speeds = [historical_speed_for(episode, quality)[0] for episode in episodes]
            measured = next((speed for speed in matching_speeds if speed > 0), 0.0)
            if measured > 0:
                speeds[quality] = measured
                history_sample_count += sum(1 for speed in matching_speeds if speed > 0)
        packaging_samples = []
        upload_samples = []
        for historical in historical_episodes:
            settings = _as_dict(historical.get("settings"))
            history = _as_dict(settings.get("processing_history"))
            successful = _as_dict(history.get("successful"))
            if successful.get("success"):
                measured_package = float(successful.get("packaging_duration_seconds") or 0) + float(successful.get("verification_duration_seconds") or 0)
                if measured_package > 0:
                    packaging_samples.append(measured_package)
            upload = _as_dict(history.get("successful_upload"))
            if upload.get("success") and float(upload.get("upload_duration_seconds") or 0) > 0:
                upload_samples.append(float(upload["upload_duration_seconds"]))
        per_episode: List[Dict[str, Any]] = []
        total_duration = 0.0
        initial_total = 0.0
        remaining_total = 0.0
        output_bytes = 0
        ready = review = 0
        font_pool: Dict[str, Dict[str, Any]] = {}
        for ep in episodes:
            duration = float(ep.get("duration_seconds") or 0)
            total_duration += duration
            width, height = int(ep.get("source_width") or 0), int(ep.get("source_height") or 0)
            settings = _as_dict(ep.get("settings"))
            outputs = _as_list(settings.get("selected_qualities")) or selected_qualities(
                width, height, bool(settings.get("include_360p", default_include_360p))
            )
            profile = _as_dict(settings.get("video_bitrate_bps_per_rendition"))
            episode_estimates = {}
            for q in outputs:
                speed = float(speeds.get(q) or _as_dict(benchmark.get(q)).get("speed_multiplier", 0) or 0)
                seconds = duration / speed if speed > 0 else 0
                episode_estimates[q] = seconds
                initial_total += seconds
                if (ep["id"], q) not in self._completed_renditions:
                    remaining_total += seconds
            if ep.get("state") == EpisodeState.NEEDS_REVIEW.value:
                review += 1
            elif ep.get("state") in (EpisodeState.PENDING.value, EpisodeState.READY_TO_ENCODE.value):
                ready += 1
            audit = ep.get("font_audit") or {}
            for item in audit.get("fonts", []) if isinstance(audit, dict) else []:
                family = str(item.get("name") or "").strip()
                if not family:
                    continue
                entry = font_pool.setdefault(family.casefold(), {
                    "family_name": family,
                    "episodes": [],
                    "sources": set(),
                    "sha256": set(),
                    "available": False,
                })
                entry["episodes"].append(ep.get("display_label") or str(ep.get("order_index", "")))
                if item.get("source"):
                    entry["sources"].add(item["source"])
                if item.get("sha256"):
                    entry["sha256"].add(item["sha256"])
                if item.get("status") != "missing":
                    entry["available"] = True
            audio_profile = _as_dict(settings.get("audio_bitrate_bps_per_rendition"))
            estimate = expected_output_size(
                duration, settings.get("preset_name") or settings.get("default_preset") or load_studio_config().get("default_preset", "balanced"),
                bool(settings.get("include_360p", default_include_360p)), outputs,
                four_k_copy=settings.get("four_k_video_mode") == "COPY",
                source_file_bytes=int(ep.get("source_bytes") or 0),
                source_video_bitrate_bps=int(settings.get("source_video_bitrate_bps") or 0),
                source_audio_bitrate_bps=int(settings.get("source_audio_bitrate_bps") or 0),
            )
            if profile and audio_profile:
                estimate["per_rendition_bytes"] = {
                    q: int(duration * (int(profile.get(q) or 0) + int(audio_profile.get(q) or 0)) / 8)
                    for q in outputs if q in profile and q != "2160p"
                } | ({"2160p": estimate["per_rendition_bytes"]["2160p"]} if "2160p" in outputs and "2160p" in estimate["per_rendition_bytes"] else {})
                estimate["estimated_output_bytes"] = sum(estimate["per_rendition_bytes"].values())
            est_bytes = int(estimate["estimated_output_bytes"] or ep.get("output_estimate_bytes") or 0)
            output_bytes += est_bytes
            per_episode.append({
                "episode": ep.get("display_label") or str(ep.get("order_index", "")),
                "duration_seconds": round(duration, 2),
                "outputs": outputs,
                "output_estimate_bytes": est_bytes,
                "output_estimate_by_rendition_bytes": estimate["per_rendition_bytes"],
                "output_estimate_is_estimate": True,
                "encoding_seconds": round(sum(episode_estimates.values()), 1),
                "rendition_seconds": {q: round(v, 1) for q, v in episode_estimates.items()},
                "actual_transcode_seconds": float(_as_dict(_as_dict(ep.get("processing_history")).get("successful")).get("total_transcode_duration_seconds") or ep.get("encoding_elapsed_seconds") or 0),
                "actual_packaging_seconds": float(_as_dict(_as_dict(ep.get("processing_history")).get("successful")).get("packaging_duration_seconds") or 0),
                "actual_upload_seconds": float(_as_dict(_as_dict(ep.get("processing_history")).get("successful_upload")).get("upload_duration_seconds") or 0),
            })
        if self._initial_transcode_estimate_seconds is None and initial_total > 0:
            self._initial_transcode_estimate_seconds = initial_total
        actual_started = bool(self._completed_renditions)
        pool_rows = []
        for entry in font_pool.values():
            pool_rows.append({
                "family_name": entry["family_name"],
                "episodes": sorted(set(entry["episodes"]), key=lambda label: natural_sort_key(str(label))),
                "sources": sorted(entry["sources"]),
                "sha256": sorted(entry["sha256"]),
                "available": entry["available"],
            })
        try:
            disk_root = get_converted_output_root(require_configured=False)
            disk_free_bytes = int(shutil.disk_usage(disk_root).free)
        except Exception:
            disk_root = TEMP_DIR
            disk_free_bytes = 0
        disk_temporary_overhead_bytes = 5 * 1024 * 1024 * 1024
        disk_safety_reserve_bytes = int(output_bytes * 0.20)
        disk_required_bytes = output_bytes + disk_temporary_overhead_bytes + disk_safety_reserve_bytes
        packaging_seconds = (sum(packaging_samples) / len(packaging_samples) * max(0, total_count - ready - review)) if packaging_samples else (float(packaging.get("season_projection_seconds") or 0) or None)
        upload_seconds = (sum(upload_samples) / len(upload_samples) * max(0, total_count - ready - review)) if upload_samples else None
        safe_transcode_seconds = initial_total * 1.25 if initial_total else 0
        safe_packaging_seconds = float(packaging.get("season_projection_safe_seconds") or 0) or None
        return {
            "total_duration_seconds": round(total_duration, 1),
            "output_estimate_bytes": output_bytes,
            "disk_preflight": {
                "work_drive": disk_root.drive,
                "free_bytes": disk_free_bytes,
                "estimated_output_bytes": output_bytes,
                "temporary_overhead_bytes": disk_temporary_overhead_bytes,
                "safety_reserve_bytes": disk_safety_reserve_bytes,
                "required_bytes": disk_required_bytes,
                "safe": disk_free_bytes >= disk_required_bytes if disk_free_bytes else False,
            },
            "per_episode": per_episode,
            "ready_count": ready,
            "needs_review_count": review,
            "font_pool": {
                "required_families": len(pool_rows),
                "available_families": sum(1 for row in pool_rows if row["available"]),
                "missing_families": sum(1 for row in pool_rows if not row["available"]),
                "fonts": pool_rows,
                "unique_binary_hashes": len({sha for row in pool_rows for sha in row["sha256"]}),
            },
            "benchmark_available": bool(benchmark),
            "history_available": history_sample_count > 0,
            "history_sample_count": history_sample_count,
            "initial_transcode_seconds": round(self._initial_transcode_estimate_seconds or initial_total, 1),
            "safe_transcode_seconds": round(safe_transcode_seconds, 1),
            "remaining_transcode_seconds": round(remaining_total, 1),
            "eta_basis": "Studio processing history" if history_sample_count else ("measured throughput" if actual_started else ("local benchmark" if benchmark else "unavailable")),
            "finish_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + remaining_total)) if remaining_total else None,
            "packaging_seconds": packaging_seconds,
            "safe_packaging_seconds": safe_packaging_seconds,
            "packaging_basis": packaging.get("basis"),
            "upload_basis": f"Studio history ({len(upload_samples)} uploads)" if upload_seconds else "not measured",
            "upload_seconds": upload_seconds,
        }

    def get_season_history(self) -> Dict[str, Any]:
        """Summarize completed seasons and measured successful quality performance."""
        episodes_by_batch: Dict[str, List[Dict[str, Any]]] = {}
        for episode in self.store.list_all_episodes():
            episodes_by_batch.setdefault(episode["batch_id"], []).append(episode)
        seasons = []
        performance_samples: Dict[str, List[tuple[str, float]]] = {}
        for batch in self.store.list_batches():
            episodes = episodes_by_batch.get(batch["id"], [])
            if not episodes:
                continue
            source_duration = sum(float(ep.get("duration_seconds") or 0) for ep in episodes)
            transcode_duration = upload_duration = 0.0
            completion_dates = []
            for episode in episodes:
                settings = _as_dict(episode.get("settings"))
                history = _as_dict(settings.get("processing_history"))
                successful = _as_dict(history.get("successful"))
                if successful.get("success"):
                    transcode_duration += float(successful.get("total_transcode_duration_seconds") or 0)
                    if successful.get("ready_at"):
                        completion_dates.append(successful["ready_at"])
                    for quality, result in _as_dict(successful.get("renditions")).items():
                        result = _as_dict(result)
                        speed = float(result.get("speed_multiplier") or 0)
                        if speed > 0:
                            performance_samples.setdefault(quality, []).append((episode.get("updated_at", ""), speed))
                upload = _as_dict(history.get("successful_upload"))
                if upload.get("success"):
                    upload_duration += float(upload.get("upload_duration_seconds") or 0)
            if not all(ep.get("state") == EpisodeState.READY.value for ep in episodes):
                continue
            seasons.append({
                "batch_id": batch["id"],
                "name": Path(batch.get("season_folder_path") or batch.get("destination_label") or "الموسم").name,
                "episodes": len(episodes),
                "source_duration_seconds": round(source_duration, 1),
                "transcode_duration_seconds": round(transcode_duration, 1),
                "upload_duration_seconds": round(upload_duration, 1),
                "output_bytes": sum(int(ep.get("package_bytes") or 0) for ep in episodes),
                "completed_at": max(completion_dates) if completion_dates else batch.get("updated_at"),
            })
        performance = {}
        for quality, samples in performance_samples.items():
            samples.sort(key=lambda sample: sample[0], reverse=True)
            weights = [0.85 ** index for index in range(len(samples))]
            performance[quality] = {
                "average_speed_multiplier": round(sum(speed * weight for (_, speed), weight in zip(samples, weights)) / sum(weights), 2),
                "samples": len(samples),
            }
        return {"seasons": seasons, "performance": performance}

    def get_runner_status(self) -> Dict[str, Any]:
        """Returns current operational status of the sequential and pipelined workers."""
        disk_write_mbps = None
        try:
            import psutil
            now = time.monotonic()
            write_bytes = int(psutil.disk_io_counters().write_bytes)
            if self._disk_io_last_sample:
                prior_time, prior_bytes = self._disk_io_last_sample
                elapsed = now - prior_time
                if elapsed > 0 and write_bytes >= prior_bytes:
                    disk_write_mbps = round((write_bytes - prior_bytes) / elapsed / (1024 * 1024), 2)
            self._disk_io_last_sample = (now, write_bytes)
        except Exception:
            pass
        active_ep = self._current_processing_ep_id or self._current_uploading_ep_id
        episodes = self.store.get_episodes(self._active_batch_id) if self._active_batch_id else []

        enc_id = self._current_processing_ep_id
        if not enc_id:
            enc_cand = next((ep for ep in episodes if ep["state"] in (
                EpisodeState.ENCODING.value, EpisodeState.PROBING.value,
                EpisodeState.READY_TO_ENCODE.value, EpisodeState.PACKAGING.value,
                EpisodeState.VERIFYING_LOCAL.value,
            )), None)
            if enc_cand:
                enc_id = enc_cand["id"]

        gpu_task = None
        gpu_task_text = "-"
        if enc_id:
            enc_ep = next((ep for ep in episodes if ep["id"] == enc_id), None)
            if enc_ep:
                telem = self._telemetry.get(enc_id, {})
                q_str = f" {enc_ep.get('current_quality') or telem.get('current_quality', '')}".strip()
                fps_val = telem.get("fps", enc_ep.get("fps", 0.0))
                spd_val = telem.get("speed", enc_ep.get("speed", "1x"))
                pct_val = round(telem.get("progress", enc_ep.get("progress", 0.0)))
                gpu_task_text = f"{enc_ep['filename']} — {enc_ep['state']}{' ' + q_str if q_str else ''} — {pct_val}% — {fps_val} FPS — {spd_val}"
                gpu_task = {
                    "episode_id": enc_ep["id"],
                    "filename": enc_ep["filename"],
                    "state": enc_ep["state"],
                    "quality": q_str,
                    "progress": pct_val,
                    "fps": fps_val,
                    "speed": spd_val,
                    "eta": telem.get("eta", enc_ep.get("eta", "00:00:00")),
                    "task_text": gpu_task_text,
                    "hardware": telem.get("hardware", {}),
                    "resource_warning": telem.get("resource_warning", ""),
                }

        up_id = self._current_uploading_ep_id
        if not up_id:
            up_cand = next((ep for ep in episodes if ep["state"] in (
                EpisodeState.UPLOADING.value, EpisodeState.SERVER_VERIFYING.value,
            )), None)
            if up_cand:
                up_id = up_cand["id"]

        network_task = None
        network_task_text = "-"
        if up_id:
            up_ep = next((ep for ep in episodes if ep["id"] == up_id), None)
            if up_ep:
                telem = self._telemetry.get(up_id, {})
                pct_val = round(telem.get("progress", up_ep.get("progress", 0.0)))
                spd_str = telem.get("upload_speed", up_ep.get("upload_speed", "0 KB/s"))
                eta_str = telem.get("upload_eta", up_ep.get("upload_eta", "00:00:00"))
                network_task_text = f"{up_ep['filename']} — {up_ep['state']} {pct_val}% — {spd_str} — ETA {eta_str}"
                network_task = {
                    "episode_id": up_ep["id"],
                    "filename": up_ep["filename"],
                    "state": up_ep["state"],
                    "progress": pct_val,
                    "upload_speed": spd_str,
                    "upload_eta": eta_str,
                    "uploaded_bytes": telem.get("uploaded_bytes", up_ep.get("uploaded_bytes", 0)),
                    "package_bytes": telem.get("package_bytes", up_ep.get("package_bytes", 0)),
                    "task_text": network_task_text,
                }

        is_running = self.is_running or (gpu_task is not None)
        is_uploading = self.is_uploading or (network_task is not None)

        # Overall descriptive status line
        status_parts = []
        if gpu_task:
            status_parts.append(f"جاري تحويل {gpu_task['filename']}")
        if network_task:
            status_parts.append(f"جاري رفع {network_task['filename']}")
        ready_count = sum(1 for ep in episodes if ep["state"] == EpisodeState.READY.value)
        if ready_count > 0:
            status_parts.append(f"{ready_count} جاهزة")
        overall_status_line = " | ".join(status_parts) if status_parts else "خامل"

        return {
            "is_running": is_running,
            "is_uploading": is_uploading,
            "is_auto_pipeline_running": self.is_auto_pipeline_running,
            "current_episode_id": active_ep,
            "current_processing_episode_id": self._current_processing_ep_id,
            "current_upload_episode_id": self._current_uploading_ep_id,
            "active_encodes_count": self._active_encodes_count,
            "active_uploads_count": self._active_uploads_count,
            "max_observed_encodes": self._max_observed_encodes,
            "max_observed_uploads": self._max_observed_uploads,
            "pipeline_window": self.pipeline_window,
            "disk_write_mbps": disk_write_mbps,
            "pause_requested": self._pause_requested,
            "stop_after_current": self._stop_after_current_requested,
            "gpu_task": gpu_task,
            "gpu_task_text": gpu_task_text,
            "network_task": network_task,
            "network_task_text": network_task_text,
            "overall_status_line": overall_status_line,
        }

    def get_live_telemetry_payload(self) -> Optional[Dict[str, Any]]:
        """Provides lightweight payload for WebSocket broadcast."""
        is_active = bool(self._active_batch_id and self._current_processing_ep_id)
        if not is_active:
            # Completion can fall between WebSocket ticks. Send the final
            # runner state too, so clients cannot retain is_running=True.
            return {"is_running": self.is_running, "runner": self.get_runner_status()}
        ep_telem = self._telemetry.get(self._current_processing_ep_id, {})
        live_episode = next((ep for ep in self.store.get_episodes(self._active_batch_id)
                             if ep.get("id") == self._current_processing_ep_id), None)
        return {
            "batch_id": self._active_batch_id,
            "episode_id": self._current_processing_ep_id,
            "telemetry": ep_telem,
            "episode": live_episode,
            "runner": self.get_runner_status(),
            "is_running": self.is_running,
        }

    # =========================================================================
    # BATCH CONTROLS
    # =========================================================================

    def start_batch(self, batch_id: Optional[str] = None, *, single_episode_id: Optional[str] = None) -> Dict[str, Any]:
        """Starts sequential processing of unfinished episodes in order."""
        bid = batch_id or self._active_batch_id
        if not bid:
            raise ValueError("لا توجد دفعة موسم محددة للبدء.")

        batch = self.store.get_batch(bid)
        if not batch or batch.get("status") in ("FAILED", "COMPLETED", "COMPLETED_LOCAL", "DELETED"):
            raise ValueError("لا يمكن بدء دفعة فاشلة أو مكتملة.")
        if os.environ.get("ENVIRONMENT", "").casefold() != "test":
            get_converted_output_root(require_configured=True)

        if self.is_running:
            return self.get_current_batch_data()
        self._configure_execution()
        with self._encoder_launch_lock:
            self._active_batch_id = bid
            self._single_episode_target_id = single_episode_id
            self._work_authorized = False
            self._run_authorization = None
            self.store.update_batch(bid, status="ACTIVE")
            self._run_authorization = (self._studio_session_id, bid, uuid.uuid4().hex)
            self._work_authorized = True
            self._pause_requested = False
            self._worker_stop_event.clear()
            self._current_cancel_event.clear()
            logger.warning("SESSION_AUTHORIZATION_GRANTED action=START_BATCH batch_id=%s queued=%d", bid, len(self.store.get_episodes(bid)))
            logger.info("Explicit sequential run authorization granted for active batch %s in this Studio session.", bid)

        if not self.is_running:
            self._worker_thread = threading.Thread(
                target=self._sequential_worker_loop,
                daemon=True,
                name="SeasonBatchWorker",
            )
            self._worker_thread.start()

        return self.get_current_batch_data()

    def start_episode(self, episode_id: str) -> Dict[str, Any]:
        """Explicitly start only the selected pending episode, then pause its batch."""
        if self.is_running:
            raise ValueError("توجد مهمة قيد التشغيل بالفعل؛ أوقفها مؤقتاً قبل بدء حلقة منفردة.")
        episode = self.store.get_episode(episode_id)
        if not episode:
            raise ValueError("الحلقة المحددة غير موجودة في قائمة الانتظار.")
        if episode.get("state") != EpisodeState.PENDING.value:
            raise ValueError("لا يمكن بدء الحلقة قبل معالجة حالة المراجعة أو إعادتها إلى الانتظار.")
        batch_id = episode["batch_id"]
        # A row-level start is an explicit request for this exact episode.
        # The worker receives a target ID and must never claim neighboring rows.
        self._stop_after_current_requested = True
        try:
            result = self.start_batch(batch_id, single_episode_id=episode_id)
        except Exception:
            self._stop_after_current_requested = False
            raise
        logger.info("EXPLICIT_SINGLE_EPISODE_START episode=%s batch=%s", episode_id, batch_id)
        return result

    def refresh_font_library_audits(self) -> Dict[str, int]:
        """Re-resolve persisted ASS audit results against the configured font library."""
        updated_count = ready_count = review_count = 0
        for episode in self.store.list_all_episodes():
            if episode.get("state") not in (EpisodeState.PENDING.value, EpisodeState.NEEDS_REVIEW.value):
                continue
            audit = episode.get("font_audit") or {}
            if not isinstance(audit, dict) or not isinstance(audit.get("fonts"), list):
                continue
            refreshed = apply_font_library_to_audit(audit)
            if refreshed == audit:
                continue
            missing = list(refreshed.get("missing_font_names") or [])
            total = int(refreshed.get("total_fonts") or 0)
            available = int(refreshed.get("available_count") or 0)
            fields: Dict[str, Any] = {
                "font_audit_json": json.dumps(refreshed, ensure_ascii=False),
                "missing_fonts": missing,
                "font_status": f"{len(missing)}/{total} مفقود" if missing else (f"{available}/{total} ✓" if total else "—"),
            }
            error = str(episode.get("error") or "")
            if missing:
                review_count += 1
                if episode.get("state") == EpisodeState.PENDING.value:
                    fields["state"] = EpisodeState.NEEDS_REVIEW.value
                if error.startswith("خطوط مفقودة"):
                    fields["error"] = f"خطوط مفقودة ({fields['font_status']}): {', '.join(missing[:5])}"
            else:
                ready_count += 1
                if episode.get("state") == EpisodeState.NEEDS_REVIEW.value and (not error or error == "None" or error.startswith("خطوط مفقودة")):
                    fields["state"] = EpisodeState.PENDING.value
                    fields["error"] = None
                    fields["current_stage"] = ""
            import_issues = _as_dict(episode.get("settings")).get("import_issues")
            if import_issues:
                fields["state"] = EpisodeState.NEEDS_REVIEW.value
                fields["error"] = "; ".join(import_issues)
            self.store.update_episode(episode["id"], **fields)
            updated_count += 1
        logger.info("FONT_AUDITS_REFRESHED episodes=%d ready=%d needs_review=%d", updated_count, ready_count, review_count)
        return {"updated": updated_count, "ready": ready_count, "needs_review": review_count}

    def pause_batch(self) -> Dict[str, Any]:
        """Requests pausing after the current safe unit of work finishes."""
        with self._encoder_launch_lock:
            self._pause_requested = True
            self._work_authorized = False
            self._run_authorization = None
            if self._active_batch_id:
                self.store.update_batch(self._active_batch_id, status="PAUSED")
        with self._pipeline_condition:
            self._pipeline_condition.notify_all()
        return self.get_current_batch_data()

    def resume_batch(self) -> Dict[str, Any]:
        """Resumes processing from the first unfinished episode."""
        self._stop_after_current_requested = False
        self._pause_requested = False
        if self._manual_upload_batches:
            return self.start_batch_upload(batch_ids=list(self._manual_upload_batches))
        batch = self.store.get_batch(self._active_batch_id) if self._active_batch_id else None
        if batch and batch.get("mode") == "AUTOMATIC_PIPELINED":
            return self.start_automatic_pipeline(self._active_batch_id)
        return self.start_batch(self._active_batch_id)

    def stop_after_current(self) -> Dict[str, Any]:
        """Signals worker to complete current episode to READY_TO_UPLOAD (or READY for uploader), then pause."""
        self._stop_after_current_requested = True
        return self.get_current_batch_data()

    def retry_episode(self, episode_id: str) -> Dict[str, Any]:
        """Resets a failed or cancelled episode so it can be reprocessed or re-uploaded."""
        ep = self.store.get_episode(episode_id)
        if ep and ep.get("state") == EpisodeState.NEEDS_REVIEW.value:
            raise ValueError("الحلقة تحتاج قرار مراجعة: توفير الخطوط أو الموافقة الصريحة على ASS fallback أو التخطي.")
        target_state = EpisodeState.PENDING.value
        current_stage = ""
        prog = 0.0

        if ep and ep.get("work_dir"):
            package_check = self.inspect_local_package(episode_id)
            if package_check.get("compatible"):
                target_state = EpisodeState.READY_TO_UPLOAD.value
                current_stage = "READY_TO_UPLOAD"
                prog = 100.0

        update_fields: Dict[str, Any] = {
            "state": target_state,
            "current_stage": current_stage,
            "current_quality": "",
            "progress": prog,
            "fps": 0.0,
            "speed": "0x",
            "eta": "00:00:00",
            # Failure detail is preserved in the last attempt's history above;
            # the retried queue item itself returns to a clean pending state.
            "error": None,
            "last_upload_error": "",
        }
        if ep:
            settings = _as_dict(ep.get("settings"))
            history = _as_dict(settings.get("processing_history"))
            attempts = [_as_dict(item) for item in _as_list(history.get("attempts"))]
            if attempts:
                last_attempt = attempts[-1]
                if not last_attempt.get("success") and ep.get("error"):
                    last_attempt.setdefault("failure_reason", ep["error"])
                history["attempts"] = attempts
                try:
                    history["attempt_count"] = max(int(history.get("attempt_count") or 0), len(attempts))
                except (TypeError, ValueError):
                    history["attempt_count"] = len(attempts)
                try:
                    history["retry_count"] = max(int(history.get("retry_count") or 0), len(attempts) - 1)
                except (TypeError, ValueError):
                    history["retry_count"] = max(0, len(attempts) - 1)
                settings["processing_history"] = history
                update_fields["settings_json"] = settings

        self.store.update_episode(episode_id, **update_fields)
        self._telemetry.pop(episode_id, None)
        with self._pipeline_condition:
            self._pipeline_condition.notify_all()
        return self.get_current_batch_data()

    def inspect_local_package(self, episode_id: str, *, work_dir: Optional[str] = None, verify_files: bool = True, cancel_event=None, progress_callback=None) -> Dict[str, Any]:
        """Verify local package files and the saved source/config/font identity before reuse."""
        episode = self.store.get_episode(episode_id)
        package_dir = work_dir or (episode.get("work_dir") if episode else None)
        if not episode or not package_dir:
            return {"compatible": False, "reason": "لا توجد حزمة محلية مسجلة."}
        work_dir = Path(package_dir).resolve()
        manifest_path = work_dir / "manifest.json"
        identity_path = work_dir / "studio_package_identity.json"
        if not manifest_path.is_file():
            return {"compatible": False, "reason": "ملف manifest.json غير موجود."}
        if not identity_path.is_file():
            return {"compatible": False, "reason": "الحزمة قديمة — يلزم التحقق أو إعادة التحويل."}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"compatible": False, "reason": f"تعذر قراءة هوية الحزمة: {exc}"}
        manifest = _as_dict(manifest)
        identity = _as_dict(identity)
        if not identity:
            return {"compatible": False, "reason": "الحزمة قديمة — يلزم التحقق أو إعادة التحويل."}
        files = _as_list(manifest.get("files"))
        for raw_entry in files:
            entry = _as_dict(raw_entry)
            if not entry:
                return {"compatible": False, "reason": "بيانات ملفات الحزمة غير صالحة."}
            relative = str(entry.get("path") or "").replace("/", os.sep)
            candidate = (work_dir / relative).resolve()
            if work_dir not in candidate.parents or not candidate.is_file():
                return {"compatible": False, "reason": f"ملف من الحزمة مفقود: {relative}"}
            expected_hash = entry.get("sha256")
            if verify_files and expected_hash and _sha256_if_file(candidate, cancel_event) != expected_hash:
                return {"compatible": False, "reason": f"بصمة ملف الحزمة غير مطابقة: {relative}"}
        source = Path(episode["source_path"])
        if not source.is_file() or _sha256_if_file(source, cancel_event, progress_callback) != identity.get("source_sha256"):
            return {"compatible": False, "reason": "بصمة ملف المصدر مختلفة أو تعذر قراءته."}
        if int(source.stat().st_size) != int(identity.get("source_bytes") or -1):
            return {"compatible": False, "reason": "حجم المصدر مختلف عن الحزمة."}
        current_resolution = f"{int(episode.get('source_width') or 0)}x{int(episode.get('source_height') or 0)}"
        if current_resolution != "0x0" and identity.get("source_resolution") != current_resolution:
            return {"compatible": False, "reason": "دقة المصدر مختلفة عن الحزمة."}
        config = load_studio_config()
        job_settings = _as_dict(episode.get("settings"))
        expected_qualities = _as_list(job_settings.get("selected_qualities")) or selected_qualities(
            int(episode.get("source_width") or 0), int(episode.get("source_height") or 0),
            bool(job_settings.get("include_360p", config.get("include_360p", False))),
        )
        if expected_qualities and set(_as_list(identity.get("qualities"))) != set(expected_qualities):
            return {"compatible": False, "reason": "سلم الجودات مختلف عن الحزمة."}
        episode_settings = _as_dict(episode.get("settings"))
        configured_choice = episode_settings.get("encoder_choice") or config.get("default_encoder", "auto")
        if str(configured_choice) != str(identity.get("encoder_choice", "auto")):
            return {"compatible": False, "reason": "إعداد المشفر تغير عن إعداد إنشاء الحزمة."}
        try:
            from studio.core.hardware import get_full_telemetry, resolve_encoder_choice
            current_gpu = (get_full_telemetry().get("hardware") or {}).get("gpu", {}).get("gpu_name", "unknown")
            if identity.get("gpu", "unknown") not in ("unknown", current_gpu):
                return {"compatible": False, "reason": "بطاقة GPU مختلفة عن بطاقة إنشاء الحزمة."}
            expected_encoder = resolve_encoder_choice(configured_choice)
            if identity.get("encoder_used") != expected_encoder:
                return {"compatible": False, "reason": "المشفر الفعلي مختلف عن مشفر إنشاء الحزمة."}
        except Exception:
            return {"compatible": False, "reason": "تعذر التحقق من بطاقة GPU أو المشفر."}
        if int(config.get("hls_segment_duration", 6)) != int(identity.get("hls_segment_duration") or 6):
            return {"compatible": False, "reason": "إعداد مقاطع HLS تغير عن الحزمة."}
        current_subtitle_hash = _sha256_if_file(episode.get("subtitle_path"), cancel_event)
        if current_subtitle_hash != identity.get("subtitle_sha256"):
            return {"compatible": False, "reason": "ملف الترجمة مختلف عن الحزمة."}
        if episode.get("subtitle_source", "none") != identity.get("subtitle_source", "none"):
            return {"compatible": False, "reason": "اختيار مصدر الترجمة مختلف عن الحزمة."}
        if episode.get("subtitle_stream_index") != identity.get("subtitle_stream_index"):
            return {"compatible": False, "reason": "مسار الترجمة المضمن مختلف عن الحزمة."}
        return {"compatible": True, "reason": "الحزمة متوافقة مع المصدر والجودات والمشفر والترجمة والخطوط المضمنة.", "work_dir": str(work_dir), "package_bytes": sum(int(item.get("size_bytes") or 0) for item in files)}

    def reupload_local_package(self, episode_id: str) -> Dict[str, Any]:
        check = self.inspect_local_package(episode_id)
        if not check.get("compatible"):
            raise ValueError(check.get("reason") or "الحزمة المحلية غير متوافقة.")
        if self.is_uploading:
            raise ValueError("يوجد رفع آخر قيد التنفيذ.")
        config = load_studio_config()
        server_url = (config.get("server_url") or "http://localhost:8000/api/v1").strip()
        api_key = (config.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("مفتاح API الخاص بالاستوديو غير مضبوط. يرجى إدخال مفتاح API في الإعدادات.")
        connection = test_connection(
            server_url, api_key, require_package_upload=True,
            host_header=config.get("server_host_header"),
        )
        if not connection.get("ok"):
            raise ValueError(connection.get("message") or "تعذر التحقق من الاتصال وصلاحيات الرفع.")
        scopes = set(connection.get("scopes") or [])
        required_scopes = {"packages:create", "packages:write", "packages:publish"}
        if scopes and not required_scopes.issubset(scopes):
            missing = ", ".join(sorted(required_scopes - scopes))
            raise ValueError(f"مفتاح API لا يملك صلاحيات رفع الحزم المطلوبة: {missing}.")
        self.store.update_episode(
            episode_id, state=EpisodeState.READY_TO_UPLOAD.value, current_stage="READY_TO_UPLOAD",
            progress=100.0, completed_at="", last_upload_error="", error=None,
        )
        threading.Thread(target=self.upload_episode, args=(episode_id,), daemon=True, name=f"PackageReupload-{episode_id[:8]}").start()
        return {"ok": True, "message": "بدأ رفع الحزمة المحلية دون إعادة تحويل المصدر."}

    def skip_episode(self, episode_id: str) -> Dict[str, Any]:
        """Marks an episode as SKIPPED to bypass it and proceed with the remaining queue."""
        self.store.update_episode(
            episode_id,
            state=EpisodeState.SKIPPED.value,
            current_stage="SKIPPED",
            progress=0.0,
            fps=0.0,
            speed="0x",
            eta="00:00:00",
            error=None,
        )
        self._telemetry.pop(episode_id, None)
        with self._pipeline_condition:
            self._pipeline_condition.notify_all()
        return self.get_current_batch_data()

    def approve_ass_fallback(self, episode_id: str) -> Dict[str, Any]:
        """Explicitly allow ASS playback with platform fallback, preserving source styling."""
        ep = self.store.get_episode(episode_id)
        if not ep or ep.get("state") != EpisodeState.NEEDS_REVIEW.value:
            raise ValueError("الحلقة ليست بانتظار قرار خطوط.")
        missing = ep.get("missing_fonts_list") or []
        if not missing:
            raise ValueError("لا توجد خطوط مفقودة لتجاوزها بخيار ASS fallback.")
        settings = ep.get("settings_json") or "{}"
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except Exception:
                settings = {}
        settings["ass_font_fallback"] = "explicit_user_approval"
        settings["ass_font_fallback_approved_at"] = _utc_now_iso()
        self.store.update_episode(
            episode_id,
            state=EpisodeState.PENDING.value,
            current_stage="",
            font_status="ASS fallback approved",
            settings_json=json.dumps(settings, ensure_ascii=False),
            error=None,
        )
        self._telemetry.pop(episode_id, None)
        with self._pipeline_condition:
            self._pipeline_condition.notify_all()
        return self.get_current_batch_data()

    def delete_episode(self, episode_id: str) -> bool:
        """Deletes a single episode from the queue and reorders remaining episodes."""
        if episode_id in self._active_processing_ids or (self._current_processing_ep_id == episode_id and self.is_running) or self._current_uploading_ep_id == episode_id:
            raise RuntimeError("لا يمكن حذف الحلقة أثناء معالجتها حالياً.")
        return self.store.delete_episode(episode_id)

    def delete_episodes(self, episode_ids: List[str], expected_deleted_count: int) -> int:
        """Delete selected queue rows only after guarding the current worker targets."""
        protected = self._active_processing_ids | {self._current_processing_ep_id, self._current_uploading_ep_id}
        if protected.intersection(str(episode_id) for episode_id in episode_ids) and (self.is_running or self.is_uploading):
            raise RuntimeError("أوقف معالجة أو رفع الحلقات المحددة قبل حذفها.")
        return self.store.delete_episodes(episode_ids, expected_deleted_count)

    def clear_batch(self, batch_id: Optional[str] = None) -> bool:
        """Clears all episodes in the batch and deletes the batch."""
        bid = batch_id or self._active_batch_id
        if not bid:
            return False

        if self.is_running or self.is_uploading:
            raise RuntimeError("أوقف المعالجة والرفع مؤقتاً قبل حذف دفعة من قائمة الانتظار.")

        batch = self.store.get_batch(bid)
        if not batch:
            return False
        expected_episode_count = len(self.store.get_episodes(bid))
        res = self.store.delete_batch(bid, expected_episode_count=expected_episode_count)
        if bid == self._active_batch_id:
            self._active_batch_id = None
            self._telemetry.clear()
            self._current_processing_ep_id = None
            self.reload_state()
        return res

    def shutdown(self, timeout_seconds: float = 10.0) -> None:
        """Stop processing and uploading before the application server exits."""
        with self._encoder_launch_lock:
            self._work_authorized = False
            self._run_authorization = None
            self._pause_requested = True
            self._worker_stop_event.set()
            self._current_cancel_event.set()
            with self._state_lock:
                for event in self._episode_cancel_events.values():
                    event.set()
            self._current_upload_cancel_event.set()
            for batch in self.store.list_batches():
                if batch.get("status") == "ACTIVE":
                    self.store.update_batch(batch["id"], status="PAUSED", pause_requested=0)
        with self._pipeline_condition:
            self._pipeline_condition.notify_all()
        workers = (self._worker_thread, self._upload_worker_thread)
        for worker in workers:
            if worker and worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=max(0.0, timeout_seconds))
        logger.info("STUDIO_SHUTDOWN work_authorized=false batch_states=paused upload_resume_state=preserved")

    def set_episode_state(
        self,
        episode_id: str,
        state: Union[str, EpisodeState],
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Sets the state of an episode in the database."""
        state_val = state.value if isinstance(state, EpisodeState) else str(state)
        return self.store.update_episode(episode_id, state=state_val, error=error)

    def update_destination(self, batch_id: str, destination_label: str) -> Optional[Dict[str, Any]]:
        """Updates manual destination label."""
        return self.store.update_batch(batch_id, destination_label=destination_label)

    def select_episode_subtitle(
        self,
        episode_id: str,
        source: str,
        path: Optional[str] = None,
        stream_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Allows user to select external subtitle, embedded subtitle, or no subtitle for an episode.
        Re-audits ASS fonts and updates NEEDS_REVIEW state.
        """
        ep = self.store.get_episode(episode_id)
        if not ep:
            raise ValueError(f"الحلقة غير موجودة: {episode_id}")

        batch_id = ep["batch_id"]
        batch = self.store.get_batch(batch_id)
        season_folder = Path(_as_dict(ep.get("settings")).get("season_folder_path") or (batch["season_folder_path"] if batch else Path(ep["source_path"]).parent))
        shared_fonts = get_season_working_fonts_dir(season_folder)
        season_fonts_dir = shared_fonts if shared_fonts.is_dir() else (season_folder / "fonts" if (season_folder / "fonts").is_dir() else None)
        source_path = Path(ep["source_path"])

        if source == "external":
            if not path or not Path(path).exists():
                raise ValueError("مسار ملف الترجمة الخارجي غير صالح أو غير موجود.")
            sub_p = Path(path).resolve()
            fmt = sub_p.suffix.lower().lstrip(".")
            metadata = normalize_subtitle(fmt=fmt, path=sub_p, default=True)
            font_status = "—"
            missing_fonts: List[str] = []
            state = EpisodeState.PENDING.value
            error = None

            if fmt in ("ass", "ssa"):
                font_audit = analyze_ass_fonts(sub_p, mkv_path=source_path, season_fonts_dir=season_fonts_dir)
                total = font_audit.get("total_fonts", 0)
                avail = font_audit.get("available_count", 0)
                missing = font_audit.get("missing_count", 0)
                missing_fonts = font_audit.get("missing_font_names", [])
                if total == 0:
                    font_status = "0/0 ✓"
                elif missing == 0:
                    font_status = f"{avail}/{total} ✓"
                else:
                    font_status = f"{missing}/{total} مفقود"
                    state = EpisodeState.NEEDS_REVIEW.value
                    error = f"خطوط مفقودة ({font_status}): {', '.join(missing_fonts[:5])}"

            updated = self.store.update_episode(
                episode_id,
                subtitle_source="external",
                subtitle_path=str(sub_p),
                subtitle_format=fmt,
                subtitle_stream_index=None,
                subtitle_label=metadata["label"],
                font_status=font_status,
                missing_fonts=missing_fonts,
                state=state,
                error=error,
                settings_json={**_as_dict(ep.get("settings")), "subtitle_metadata": metadata},
            )
            return updated or {}

        elif source == "embedded":
            if stream_index is None:
                raise ValueError("يجب تحديد مؤشر المسار المضمن (stream_index).")
            from studio.core.episode_scanner import probe_embedded_subtitles
            streams = probe_embedded_subtitles(source_path)
            matched_s = next((s for s in streams if s["index"] == stream_index), None)
            fmt = matched_s["codec"] if matched_s else "unknown"
            metadata = normalize_subtitle(
                language=(matched_s or {}).get("language"),
                fmt=fmt,
                title=(matched_s or {}).get("title"),
                forced=(matched_s or {}).get("forced"),
                default=(matched_s or {}).get("default"),
            )

            updated = self.store.update_episode(
                episode_id,
                subtitle_source="embedded",
                subtitle_path="",
                subtitle_format=fmt,
                subtitle_stream_index=stream_index,
                subtitle_label=metadata["label"],
                font_status="—",
                missing_fonts=[],
                state=EpisodeState.PENDING.value,
                error=None,
                settings_json={**_as_dict(ep.get("settings")), "subtitle_metadata": metadata},
            )
            return updated or {}

        elif source == "none":
            metadata = normalize_subtitle(fmt="unknown")
            metadata["label"] = "بدون ترجمة"
            updated = self.store.update_episode(
                episode_id,
                subtitle_source="none",
                subtitle_path="",
                subtitle_format="",
                subtitle_stream_index=None,
                subtitle_label="بدون ترجمة",
                font_status="—",
                missing_fonts=[],
                state=EpisodeState.PENDING.value,
                error=None,
                settings_json={**_as_dict(ep.get("settings")), "subtitle_metadata": metadata},
            )
            return updated or {}
        else:
            raise ValueError(f"نوع الترجمة غير معروف: {source}")

    # =========================================================================
    # SEQUENTIAL WORKER PIPELINE
    # =========================================================================

    def _sequential_worker_loop(self) -> None:
        """Sequential orchestrator executing episodes in strict natural order."""
        if self._execution_config["processing_mode"] == "server" or max(self._execution_config["cpu_concurrent_jobs"], self._execution_config["gpu_concurrent_jobs"]) > 1:
            return self._configured_worker_loop(pipelined=False)
        logger.info("Starting sequential batch processing loop for batch: %s", self._active_batch_id)

        while not self._worker_stop_event.is_set():
            if not self._active_batch_id:
                break
            if not self._worker_may_claim():
                logger.info("Sequential worker has no active user authorization; leaving queued episodes untouched.")
                break

            if self._pause_requested:
                logger.info("Batch pause requested. Halting sequential loop.")
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                self._pause_requested = False
                break

            # Fetch fresh list of episodes ordered by order_index ASC
            episodes = self.store.get_episodes(self._active_batch_id)
            if not episodes:
                break

            # A row-level start is strictly scoped to its selected pending job.
            # All other rows remain untouched, regardless of their queue order.
            next_ep: Optional[Dict[str, Any]] = None
            target_id = self._single_episode_target_id
            if target_id:
                target = next((ep for ep in episodes if ep.get("id") == target_id), None)
                if target and target.get("state") == EpisodeState.PENDING.value:
                    next_ep = target
                else:
                    logger.warning("Explicit single-episode target is unavailable or not pending (%s); pausing without claiming another row.", target_id)
                    self.store.update_batch(self._active_batch_id, status="PAUSED")
                    self._single_episode_target_id = None
                    self._stop_after_current_requested = False
                    break
            else:
                for ep in episodes:
                    st = ep["state"]
                    if st in (
                        EpisodeState.READY_TO_UPLOAD.value,
                        EpisodeState.UPLOADING.value,
                        EpisodeState.SERVER_VERIFYING.value,
                        EpisodeState.READY.value,
                        EpisodeState.SKIPPED.value,
                        EpisodeState.CANCELLED.value,
                    ):
                        continue
                    if st == EpisodeState.FAILED.value:
                        logger.warning("Encountered failed episode %s (%s). Pausing batch.", ep["order_index"], ep["filename"])
                        self.store.update_batch(self._active_batch_id, status="PAUSED")
                        return
                    if st == EpisodeState.NEEDS_REVIEW.value:
                        logger.warning("Encountered episode requiring review %s (%s). Pausing batch.", ep["order_index"], ep["filename"])
                        self.store.update_batch(self._active_batch_id, status="PAUSED")
                        return
                    next_ep = ep
                    break

            if not next_ep:
                # All episodes in batch are either READY_TO_UPLOAD or SKIPPED!
                logger.info("All episodes in batch %s have completed local preparation.", self._active_batch_id)
                self.store.update_batch(self._active_batch_id, status="COMPLETED_LOCAL")
                break

            # Process the episode under single-encoder lock (GPU Concurrency = 1)
            success = self._process_single_episode(next_ep)
            if not success:
                # Episode failed; halt batch loop for user inspection
                logger.warning("Episode %s failed. Stopping sequential worker.", next_ep["filename"])
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                break

            # Check Stop-after-current control
            if self._stop_after_current_requested:
                logger.info("Stop-after-current satisfied after episode %s.", next_ep["filename"])
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                self._stop_after_current_requested = False
                self._single_episode_target_id = None
                break

            # Brief yield to check cancellation
            time.sleep(0.1)

        self._current_processing_ep_id = None
        logger.info("Sequential batch worker exited.")

    def _configure_execution(self):
        cfg = load_studio_config()
        self._execution_config = {"processing_mode": cfg.get("processing_mode", "local"),
            **{k: max(1, min(4, int(cfg.get(k, 1)))) for k in ("cpu_concurrent_jobs", "gpu_concurrent_jobs")}}
        self._resource_locks = {kind: threading.BoundedSemaphore(self._execution_config[kind + "_concurrent_jobs"]) for kind in ("cpu", "gpu")}

    def _resource_kind(self, ep):
        if _as_dict(ep.get("settings")).get("processing_mode", self._execution_config["processing_mode"]) == "server":
            return "cpu"
        choice = _as_dict(ep.get("settings")).get("encoder_choice", "auto")
        if choice in {"cpu", "libx264"}:
            return "cpu"
        if choice == "auto":
            from studio.core.hardware import resolve_encoder_choice
            return "cpu" if resolve_encoder_choice(choice) == "libx264" else "gpu"
        return "gpu"

    def _configured_worker_loop(self, pipelined=False):
        from studio.core.scheduler import run_resource_scheduler
        limits = {kind: self._execution_config[kind + "_concurrent_jobs"] for kind in ("cpu", "gpu")}
        def pending():
            episodes = self.store.get_episodes(self._active_batch_id)
            candidates = [ep for ep in episodes if ep["state"] in {EpisodeState.PENDING.value, EpisodeState.READY_TO_ENCODE.value}]
            if self._single_episode_target_id:
                candidates = [ep for ep in candidates if ep["id"] == self._single_episode_target_id]
            if pipelined and self._execution_config["processing_mode"] != "server":
                ahead = sum(ep["state"] == EpisodeState.READY_TO_UPLOAD.value for ep in episodes)
                candidates = candidates[:max(0, self.pipeline_window - ahead)]
            return candidates
        while self._worker_may_claim() and not self._worker_stop_event.is_set():
            results = run_resource_scheduler(pending, self._process_single_episode, self._resource_kind, limits,
                lambda: self._pause_requested or self._worker_stop_event.is_set() or (self._stop_after_current_requested and not self._single_episode_target_id))
            episodes = self.store.get_episodes(self._active_batch_id)
            if any(x is False for x in results.values()) or self._pause_requested or self._stop_after_current_requested:
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                break
            if not any(ep["state"] in {EpisodeState.PENDING.value, EpisodeState.READY_TO_ENCODE.value} for ep in episodes):
                status = "COMPLETED" if all(ep["state"] in {EpisodeState.READY.value, EpisodeState.SKIPPED.value} for ep in episodes) else "COMPLETED_LOCAL"
                self.store.update_batch(self._active_batch_id, status=status)
                break
            if not pipelined:
                break
            self._worker_stop_event.wait(0.1)

    def _process_single_episode(self, ep):
        import_issues = _as_dict(ep.get("settings")).get("import_issues")
        if import_issues:
            self.store.update_episode(ep["id"], state=EpisodeState.NEEDS_REVIEW.value, error="; ".join(import_issues))
            return False
        kind = self._resource_kind(ep)
        with self._resource_locks[kind]:
            with self._state_lock:
                self._active_processing_ids.add(ep["id"])
                self._episode_cancel_events[ep["id"]] = threading.Event()
                self._active_encodes_count += 1
                self._max_observed_encodes = max(self._max_observed_encodes, self._active_encodes_count)
            try:
                if _as_dict(ep.get("settings")).get("processing_mode", self._execution_config["processing_mode"]) == "server":
                    return self._process_server_episode(ep)
                return self._process_episode_impl(ep)
            except Exception as exc:
                logger.exception("Episode execution failed: %s", ep["id"])
                self.store.update_episode(ep["id"], state=EpisodeState.FAILED.value, error=str(exc))
                return False
            finally:
                with self._state_lock:
                    self._active_processing_ids.discard(ep["id"])
                    self._episode_cancel_events.pop(ep["id"], None)
                    self._active_encodes_count -= 1
                    self._current_processing_ep_id = next(iter(self._active_processing_ids), None)

    def _process_server_episode(self, ep):
        from studio.core.server_processing import process_on_server
        cfg = load_studio_config()
        job_settings = _as_dict(ep.get("settings"))
        def persist(value):
            job_settings["server_source"] = value
            self.store.update_episode(ep["id"], settings_json=job_settings)
        def progress(state, pct):
            stage = EpisodeState.READY.value if state == "READY" else EpisodeState.ENCODING.value if state in {"PROBING", "PROCESSING", "VERIFYING", "QUEUED"} else EpisodeState.UPLOADING.value
            self.store.update_episode(ep["id"], state=stage, current_stage=state, progress=pct)
            self._telemetry[ep["id"]] = {"progress": pct, "current_stage": state, "encoder_used": "SERVER"}
        try:
            if not cfg.get("api_key"):
                raise ValueError("مفتاح Studio API مطلوب للمعالجة على الخادم.")
            metadata = {"title": Path(ep["filename"]).stem, "series_name": job_settings.get("series_name"),
                "season_number": int(job_settings.get("season_number") if job_settings.get("season_number") != "" and job_settings.get("season_number") is not None else 1), "episode_number": int(ep.get("episode_number") or ep["order_index"])}
            batch = self.store.get_batch(ep["batch_id"])
            if batch.get("target_folder_id"):
                metadata["folder_id"] = batch["target_folder_id"]
            for source_key, target_key in (("target_series_id", "series_id"), ("target_season_id", "season_id")):
                if batch.get(source_key):
                    metadata[target_key] = batch[source_key]
            vid = process_on_server(ep["source_path"], cfg["server_url"], cfg["api_key"], cfg.get("server_host_header"),
                metadata, job_settings.get("server_source", {}), progress, self._episode_cancel_events[ep["id"]], persist)
            self.store.update_episode(ep["id"], state=EpisodeState.READY.value, current_stage="READY", server_status="READY", server_video_id=vid, progress=100)
            return True
        except Exception as exc:
            self.store.update_episode(ep["id"], state=EpisodeState.FAILED.value, error=str(exc))
            return False

    def _process_episode_impl(self, ep: Dict[str, Any]) -> bool:
        """Executes the full local pipeline for a single episode:
        PROBING → READY_TO_ENCODE → ENCODING → PACKAGING → VERIFYING_LOCAL → READY_TO_UPLOAD
        Resource limits are enforced by the episode scheduler and semaphores.
        """
        ep_id = ep["id"]
        source_path = Path(ep["source_path"]).resolve()
        order_index = ep["order_index"]
        batch_id = ep["batch_id"]

        if not self._run_is_authorized(batch_id, ep_id):
            logger.warning("Episode processing denied before state changes (episode=%s).", ep_id)
            return False

        with nullcontext():
            self._current_processing_ep_id = ep_id
            pipeline_started_at = _utc_now_iso()
            pipeline_started_monotonic = time.monotonic()
            settings = _as_dict(ep.get("settings"))
            history = _as_dict(settings.get("processing_history"))
            history.setdefault("schema_version", 1)
            attempts = _as_list(history.get("attempts"))
            history["attempts"] = attempts
            attempt = {
                "attempt_number": len(attempts) + 1,
                "started_at": pipeline_started_at,
                "renditions": {},
                "success": False,
            }
            attempts.append(attempt)
            history["attempt_count"] = len(attempts)
            history["retry_count"] = max(0, len(attempts) - 1)
            settings["processing_history"] = history
            ep["settings"] = settings

            def save_history() -> None:
                settings["processing_history"] = history
                self.store.update_episode(ep_id, settings_json=settings)

            def finish_attempt(success: bool = False) -> None:
                attempt["attempt_finished_at"] = _utc_now_iso()
                attempt["attempt_duration_seconds"] = round(max(0.0, time.monotonic() - pipeline_started_monotonic), 2)
                attempt["success"] = bool(success)
                if success:
                    history["successful"] = attempt
                save_history()

            save_history()

            self._telemetry[ep_id] = {
                "progress": 0.0,
                "rendition_progress": 0.0,
                "fps": 0.0,
                "speed": "0x",
                "eta": "00:00:00",
                "elapsed_seconds": 0.0,
                "encoder_used": "",
                "current_quality": "",
                "current_stage": EpisodeState.PROBING.value,
                "package_bytes": 0,
            }
            self._report_local_processing(ep_id, EpisodeState.PROBING.value)
            encoding_started_at: Optional[float] = None
            rendition_elapsed_times: Dict[str, float] = {}

            def persist_encoding_measurement() -> Optional[float]:
                if encoding_started_at is None:
                    return None
                elapsed = round(max(0.0, time.monotonic() - encoding_started_at), 2)
                try:
                    finished_at = _utc_now_iso()
                    attempt["transcode_started_at"] = attempt.get("transcode_started_at") or finished_at
                    attempt["transcode_finished_at"] = finished_at
                    attempt["total_transcode_duration_seconds"] = elapsed
                    attempt["encoding_elapsed_seconds"] = elapsed
                    attempt["rendition_elapsed_seconds"] = dict(rendition_elapsed_times)
                    history["source_duration_seconds"] = float(ep.get("duration_seconds") or 0)
                    settings["encoding_elapsed_seconds"] = elapsed
                    settings["rendition_elapsed_seconds"] = dict(rendition_elapsed_times)
                    settings["encoding_recorded_at"] = finished_at
                    save_history()
                except Exception as save_err:
                    logger.warning("Unable to persist encoding time for %s: %s", ep_id, save_err)
                self._telemetry.setdefault(ep_id, {})["encoding_elapsed_seconds"] = elapsed
                return elapsed

            cfg = load_studio_config()
            batch = self.store.get_batch(batch_id) or {}
            season_folder = source_path.parent
            work_dir = batch_output_directory(
                cfg,
                source_path=source_path,
                season_folder=season_folder,
                episode_number=ep.get("episode_number") if ep.get("episode_number") is not None else order_index,
                existing_work_dir=ep.get("work_dir"),
                preferred_work_dir=_as_dict(ep.get("settings")).get("output_directory"),
                allow_test_fallback=os.environ.get("ENVIRONMENT", "").casefold() == "test",
            )
            # A retry must never encode into a non-empty legacy directory. Reuse
            # it only when its complete package matches the current source and
            # settings; otherwise retain it and allocate a fresh sibling path.
            if ep.get("state") == EpisodeState.PENDING.value and work_dir.exists():
                try:
                    has_existing_output = not work_dir.is_dir() or any(work_dir.iterdir())
                except OSError:
                    has_existing_output = True
                if has_existing_output:
                    package_check = self.inspect_local_package(ep_id, work_dir=str(work_dir))
                    if package_check.get("compatible"):
                        self.store.update_episode(
                            ep_id,
                            state=EpisodeState.READY_TO_UPLOAD.value,
                            current_stage="COMPLETED_LOCAL",
                            progress=100.0,
                            package_bytes=int(package_check.get("package_bytes") or 0),
                            error=None,
                            work_dir=str(work_dir),
                        )
                        logger.info("EXISTING_PACKAGE_REUSED episode=%s package_bytes=%s", ep_id, package_check.get("package_bytes", 0))
                        return True
                    work_dir = fresh_batch_output_directory(
                        cfg,
                        source_path=source_path,
                        season_folder=season_folder,
                        episode_number=ep.get("episode_number") if ep.get("episode_number") is not None else order_index,
                        preferred_work_dir=str(work_dir),
                    )
                    settings = _as_dict(ep.get("settings"))
                    settings["output_directory"] = str(work_dir)
                    self.store.update_episode(ep_id, settings_json=settings)
                    logger.warning("LEGACY_OUTPUT_PRESERVED episode=%s new_work_dir=%s", ep_id, work_dir)
            from studio.core.output_paths import claim_output_directory
            claim_output_directory(work_dir, ep_id, source_path)

            try:
                ok_disk, disk_err = self.check_disk_space_guard(ep)
                if not ok_disk:
                    raise RuntimeError(disk_err)

                # -------------------------------------------------------------
                # 1. PROBING
                # -------------------------------------------------------------
                logger.info("[Episode %02d] Step 1: Probing %s", order_index, source_path.name)
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.PROBING.value,
                    current_stage=EpisodeState.PROBING.value,
                    progress=2.0,
                    work_dir=str(work_dir),
                )
                self._telemetry[ep_id]["progress"] = 2.0
                self._telemetry[ep_id]["current_stage"] = EpisodeState.PROBING.value

                probe_info = probe_video(source_path)
                duration = probe_info["duration_seconds"]
                settings = _as_dict(ep.get("settings"))
                preset_name = normalize_preset_name(settings.get("preset_name") or settings.get("default_preset") or cfg.get("default_preset", "balanced"))
                include_360p = bool(settings.get("include_360p", cfg.get("include_360p", False)))
                preset_snapshot = preset_identity(preset_name, include_360p)
                # Stored values are the queued job's immutable encoding contract.
                for key in ("video_bitrate_bps_per_rendition", "audio_bitrate_bps_per_rendition", "nvenc_preset", "rate_control_mode", "cq", "four_k_strategy"):
                    if key in settings:
                        preset_snapshot[key] = settings[key]

                # Determine quality ladder: Downscale only, never upscale
                allowed_qualities = _as_list(settings.get("selected_qualities")) or [q["key"] for q in probe_info["qualities"] if q["allowed"]]
                if not include_360p:
                    allowed_qualities = [quality for quality in allowed_qualities if quality != "360p"]
                if int(probe_info.get("width") or 0) >= 3840 and "2160p" not in allowed_qualities:
                    allowed_qualities.append("2160p")
                if not allowed_qualities and include_360p and int(probe_info.get("height") or 0) >= 360:
                    allowed_qualities = ["360p"]
                if not allowed_qualities:
                    raise RuntimeError("لا توجد جودة إخراج مدعومة لهذا المصدر؛ تحقق من إعدادات الجودة والدقة.")

                encoder_choice = settings.get("encoder_choice") or cfg.get("default_encoder", "auto")
                hls_segment_duration = cfg.get("hls_segment_duration", 6)

                # -------------------------------------------------------------
                # 2. READY_TO_ENCODE
                # -------------------------------------------------------------
                logger.info("[Episode %02d] Step 2: Ready to encode (qualities: %s, encoder: %s)", order_index, allowed_qualities, encoder_choice)
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.READY_TO_ENCODE.value,
                    current_stage=EpisodeState.READY_TO_ENCODE.value,
                    progress=8.0,
                )
                self._telemetry[ep_id]["progress"] = 8.0
                self._telemetry[ep_id]["current_stage"] = EpisodeState.READY_TO_ENCODE.value

                if self._worker_stop_event.is_set() or self._pause_requested:
                    finish_attempt(False)
                    return False
                if not self._run_is_authorized(batch_id, ep_id):
                    finish_attempt(False)
                    logger.warning("Episode processing stopped before encoding because session authorization is no longer valid (episode=%s).", ep_id)
                    return False

                # -------------------------------------------------------------
                # 3. ENCODING
                # -------------------------------------------------------------
                logger.info("[Episode %02d] Step 3: Encoding HLS renditions...", order_index)
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.ENCODING.value,
                    current_stage=EpisodeState.ENCODING.value,
                    progress=10.0,
                )
                self._telemetry[ep_id]["progress"] = 10.0
                self._telemetry[ep_id]["current_stage"] = EpisodeState.ENCODING.value

                last_db_persist = 0.0

                def on_transcode_progress(p_data: Dict[str, Any]):
                    nonlocal last_db_persist
                    if p_data.get("stage") == "before_rendition":
                        free_bytes = shutil.disk_usage(work_dir).free
                        remaining_bytes = int(p_data.get("estimated_remaining_output_bytes") or 0)
                        required_bytes = int(remaining_bytes * 1.20 + 5 * 1024 * 1024 * 1024)
                        if free_bytes < required_bytes:
                            self._telemetry[ep_id]["resource_warning"] = (
                                f"مساحة التخزين منخفضة؛ يلزم {format_bytes(required_bytes)} ويتوفر {format_bytes(free_bytes)}"
                            )
                            return False
                        return True
                    if p_data.get("stage") == "transcoding_rendition_completed":
                        q = p_data.get("current_rendition", "")
                        elapsed = float(p_data.get("rendition_elapsed_seconds") or 0)
                        source_duration = float(p_data.get("source_duration_seconds") or 0)
                        if q and elapsed > 0 and source_duration > 0:
                            rendition_elapsed_times[q] = round(elapsed, 2)
                            measured_speed = source_duration / elapsed
                            attempt.setdefault("renditions", {})[q] = {
                                "duration_seconds": round(elapsed, 2),
                                "fps": float(p_data.get("fps") or 0),
                                "speed_multiplier": round(float(p_data.get("speed_multiplier") or measured_speed), 4),
                                "source_duration_seconds": round(source_duration, 2),
                                "profile": {
                                    "gpu": (p_data.get("hardware") or {}).get("gpu", {}).get("gpu_name", "unknown"),
                                    "encoder": p_data.get("encoder_used", "unknown"),
                                    "encoder_choice": encoder_choice,
                                    "source_resolution": p_data.get("source_resolution", "unknown"),
                                    "output_resolution": p_data.get("output_resolution", "unknown"),
                                    "codec": p_data.get("codec", "h264"),
                                    "subtitle_mode": p_data.get("subtitle_mode", "softsub"),
                                },
                            }
                            save_history()
                            self._measured_rendition_speeds[q] = measured_speed
                            self._completed_renditions.add((ep_id, q))
                            gpu = (p_data.get("hardware") or {}).get("gpu", {})
                            profile = {
                                "gpu": gpu.get("gpu_name", "unknown"),
                                "encoder": p_data.get("encoder_used", "unknown"),
                                "source_resolution": p_data.get("source_resolution", "unknown"),
                                "output_resolution": p_data.get("output_resolution", "unknown"),
                                "codec": p_data.get("codec", "h264"),
                                "subtitle_mode": p_data.get("subtitle_mode", "softsub"),
                                "quality": q,
                                "fps": p_data.get("fps"),
                                "speed_multiplier": round(measured_speed, 4),
                                "measured_elapsed_seconds": elapsed,
                                "sample_duration_seconds": source_duration,
                                "measured_at": _utc_now_iso(),
                            }
                            profile_key = "|".join(str(profile[k]) for k in (
                                "gpu", "encoder", "source_resolution", "output_resolution", "codec", "subtitle_mode", "quality"
                            ))
                            profile_path = TEMP_DIR / "encoder_history.json"
                            try:
                                history = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
                            except Exception:
                                history = {}
                            history = _as_dict(history)
                            prior = _as_dict(history.get(profile_key))
                            prior_samples = int(prior.get("samples", 0))
                            prior_mean = float(prior.get("mean_speed_multiplier", 0) or 0)
                            profile["samples"] = prior_samples + 1
                            profile["mean_speed_multiplier"] = round(
                                ((prior_mean * prior_samples) + measured_speed) / (prior_samples + 1), 4
                            )
                            history[profile_key] = profile
                            temp_path = profile_path.with_suffix(".tmp")
                            temp_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
                            temp_path.replace(profile_path)
                        return
                    if p_data.get("stage") == "transcoding":
                        mapped_pct = 10.0 + (p_data.get("overall_progress_pct", 0.0) * 0.70)
                        fps_val = p_data.get("fps", 0.0)
                        speed_val = p_data.get("speed", "0x")
                        eta_val = p_data.get("eta_formatted", "00:00:00")
                        current_q = p_data.get("current_rendition", "")
                        rendition_pct = float(p_data.get("rendition_progress_pct", 0.0) or 0.0)
                        elapsed_seconds = float(p_data.get("elapsed_seconds", 0.0) or 0.0)
                        encoder_used = str(p_data.get("encoder_used") or "")
                        hardware = p_data.get("hardware") or {}
                        gpu_temp = (hardware.get("gpu") or {}).get("gpu_temp_c", 0)
                        warnings = []
                        if gpu_temp and gpu_temp > 85:
                            warnings.append(f"حرارة GPU مرتفعة: {gpu_temp}°C")
                        resource_warning = " • ".join(warnings)

                        self._telemetry[ep_id] = {
                            "progress": round(mapped_pct, 1),
                            "fps": fps_val,
                            "speed": speed_val,
                            "eta": eta_val,
                            "current_quality": current_q,
                            "rendition_progress": rendition_pct,
                            "elapsed_seconds": elapsed_seconds,
                            "encoder_used": encoder_used,
                            "rendition_index": p_data.get("rendition_index", 0),
                            "total_renditions": p_data.get("total_renditions", 0),
                            "renditions": list(allowed_qualities),
                            "completed_renditions": list(p_data.get("completed_renditions") or []),
                            "current_stage": EpisodeState.ENCODING.value,
                            "package_bytes": 0,
                            "hardware": hardware,
                            "resource_warning": resource_warning,
                            "encoding_started_at": self._telemetry.get(ep_id, {}).get("encoding_started_at"),
                        }

                        now = time.time()
                        # Throttle SQLite transactions to at most once per 1.0s
                        if now - last_db_persist >= 1.0:
                            self.store.update_episode(
                                ep_id,
                                progress=round(mapped_pct, 1),
                                rendition_progress=round(rendition_pct, 1),
                                fps=fps_val,
                                speed=speed_val,
                                eta=eta_val,
                                elapsed_seconds=round(elapsed_seconds, 1),
                                encoder_used=encoder_used,
                                current_quality=current_q,
                                current_stage=EpisodeState.ENCODING.value,
                            )
                            last_db_persist = now
                        if self._current_processing_ep_id == ep_id:
                            self._telemetry[ep_id]["episode_elapsed_seconds"] = round(
                                max(0.0, time.monotonic() - pipeline_started_monotonic), 1
                            )
                        self._report_local_processing(ep_id, EpisodeState.ENCODING.value)

                encoding_started_at = time.monotonic()
                attempt["transcode_started_at"] = _utc_now_iso()
                attempt["encoder_choice"] = encoder_choice
                attempt["source_resolution"] = f"{probe_info.get('width', 0)}x{probe_info.get('height', 0)}"
                attempt["source_duration_seconds"] = float(duration or 0)
                history["source_duration_seconds"] = float(duration or 0)
                save_history()
                self._telemetry[ep_id]["encoding_started_at"] = time.time()
                transcode_res = transcode_package_renditions(
                    source_path=source_path,
                    output_dir=work_dir,
                    selected_qualities=allowed_qualities,
                    encoder_choice=encoder_choice,
                    hls_segment_duration=hls_segment_duration,
                    progress_callback=on_transcode_progress,
                    cancel_event=self._episode_cancel_events.get(ep_id, self._current_cancel_event),
                    process_launcher=lambda command, **kwargs: self._authorized_encoder_launch(batch_id, ep_id, command, **kwargs),
                    process_runner=lambda command, **kwargs: self._authorized_encoder_command(batch_id, ep_id, command, **kwargs),
                    preset=preset_name,
                    include_360p=include_360p,
                    rate_profile=preset_snapshot,
                )
                persist_encoding_measurement()
                attempt["transcode_finished_at"] = _utc_now_iso()
                attempt["total_transcode_duration_seconds"] = round(max(0.0, time.monotonic() - encoding_started_at), 2)

                if self._episode_cancel_events.get(ep_id, self._current_cancel_event).is_set():
                    finish_attempt(False)
                    self.store.update_episode(ep_id, state=EpisodeState.CANCELLED.value, current_stage="CANCELLED")
                    return False

                # -------------------------------------------------------------
                # 4. PACKAGING
                # -------------------------------------------------------------
                logger.info("[Episode %02d] Step 4: Packaging and Subtitles...", order_index)
                packaging_started_monotonic = time.monotonic()
                attempt["packaging_started_at"] = _utc_now_iso()
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.PACKAGING.value,
                    current_stage=EpisodeState.PACKAGING.value,
                    progress=85.0,
                )
                self._telemetry[ep_id]["progress"] = 85.0
                self._telemetry[ep_id]["current_stage"] = EpisodeState.PACKAGING.value
                self._report_local_processing(ep_id, EpisodeState.PACKAGING.value)

                declared_subtitles: List[Dict[str, Any]] = []
                packaged_fonts: List[Dict[str, Any]] = []

                # Find season fonts directory
                batch_rec = self.store.get_batch(batch_id)
                season_folder = Path(_as_dict(ep.get("settings")).get("season_folder_path") or source_path.parent)
                shared_fonts = get_season_working_fonts_dir(season_folder)
                season_fonts_dir = shared_fonts if shared_fonts.is_dir() else (season_folder / "fonts" if (season_folder / "fonts").is_dir() else None)

                sub_src = ep.get("subtitle_source", "none")
                sub_path_str = ep.get("subtitle_path", "")
                sub_stream_idx = ep.get("subtitle_stream_index")
                sub_fmt = (ep.get("subtitle_format") or "").lower()

                # Fallback to direct sidecar check if subtitle_source wasn't explicitly configured
                if sub_src == "none" and not ep.get("subtitle_label"):
                    if source_path.with_suffix(".ass").exists():
                        sub_src = "external"
                        sub_path_str = str(source_path.with_suffix(".ass"))
                        sub_fmt = "ass"
                    elif source_path.with_suffix(".srt").exists():
                        sub_src = "external"
                        sub_path_str = str(source_path.with_suffix(".srt"))
                        sub_fmt = "srt"

                if sub_src == "external" and sub_path_str:
                    sub_target = Path(sub_path_str).resolve()
                    if sub_target.exists():
                        sub_res = process_softsub_file(
                            subtitle_input_path=sub_target,
                            target_subs_dir=work_dir / "subtitles",
                            language_code="ar",
                        )
                        sub_fonts_rel: List[str] = []
                        if sub_res["format"] in ("ass", "ssa"):
                            req_fonts, _ = parse_ass_fonts(sub_res["primary_abs_path"])
                            packaged_fonts = package_required_fonts(
                                required_fonts=req_fonts,
                                target_fonts_dir=work_dir / "fonts",
                                mkv_path=source_path,
                                season_fonts_dir=season_fonts_dir,
                            )
                            sub_fonts_rel = [f["path"] for f in packaged_fonts]

                        declared_subtitles.append({
                            "language_code": "ar",
                            "language_name": "العربية",
                            "label": f"العربية ({sub_res['format'].upper()})",
                            "is_default": True,
                            "path": sub_res["primary_rel_path"],
                            "format": sub_res["format"],
                            "fonts": sub_fonts_rel,
                            "auxiliary_files": sub_res.get("auxiliary_files", []),
                        })

                elif sub_src == "embedded" and sub_stream_idx is not None:
                    from studio.core.ffmpeg_finder import get_ffmpeg_path
                    ffmpeg_bin = get_ffmpeg_path()
                    if ffmpeg_bin:
                        out_sub_dir = work_dir / "subtitles"
                        out_sub_dir.mkdir(parents=True, exist_ok=True)
                        ext = "ass" if "ass" in sub_fmt else ("srt" if "subrip" in sub_fmt or "srt" in sub_fmt else "vtt")
                        raw_extracted = out_sub_dir / f"embedded_raw.{ext}"
                        extract_cmd = [
                            ffmpeg_bin, "-y",
                            "-i", str(source_path),
                            "-map", f"0:{sub_stream_idx}",
                            "-c:s", "ass" if ext == "ass" else "srt" if ext == "srt" else "webvtt",
                            str(raw_extracted),
                        ]
                        run_headless(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                        if raw_extracted.exists() and raw_extracted.stat().st_size > 0:
                            sub_res = process_softsub_file(
                                subtitle_input_path=raw_extracted,
                                target_subs_dir=out_sub_dir,
                                language_code="ar",
                            )
                            sub_fonts_rel: List[str] = []
                            if sub_res["format"] in ("ass", "ssa"):
                                req_fonts, _ = parse_ass_fonts(sub_res["primary_abs_path"])
                                packaged_fonts = package_required_fonts(
                                    required_fonts=req_fonts,
                                    target_fonts_dir=work_dir / "fonts",
                                    mkv_path=source_path,
                                    season_fonts_dir=season_fonts_dir,
                                )
                                sub_fonts_rel = [f["path"] for f in packaged_fonts]

                            declared_subtitles.append({
                                "language_code": "ar",
                                "language_name": "العربية",
                                "label": f"العربية (مضمنة {sub_res['format'].upper()})",
                                "is_default": True,
                                "path": sub_res["primary_rel_path"],
                                "format": sub_res["format"],
                                "fonts": sub_fonts_rel,
                                "auxiliary_files": sub_res.get("auxiliary_files", []),
                            })

                # -------------------------------------------------------------
                # 5. VERIFYING_LOCAL
                # -------------------------------------------------------------
                logger.info("[Episode %02d] Step 5: Verifying package locally...", order_index)
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.VERIFYING_LOCAL.value,
                    current_stage=EpisodeState.VERIFYING_LOCAL.value,
                    progress=92.0,
                )
                self._telemetry[ep_id]["progress"] = 92.0
                self._telemetry[ep_id]["current_stage"] = EpisodeState.VERIFYING_LOCAL.value
                self._report_local_processing(ep_id, EpisodeState.VERIFYING_LOCAL.value)

                title = f"{ep.get('display_label', '')} - {source_path.stem}".strip(" -")
                attempt["packaging_duration_seconds"] = round(max(0.0, time.monotonic() - packaging_started_monotonic), 2)
                attempt["verification_started_at"] = _utc_now_iso()
                verification_started_monotonic = time.monotonic()
                package_meta = build_and_verify_package(
                    package_dir=work_dir,
                    title=title,
                    description=json.dumps(_as_dict(ep.get("settings")).get("upload_destination", {}), ensure_ascii=False),
                    duration_seconds=duration,
                    renditions=transcode_res["renditions"],
                    subtitles=declared_subtitles,
                    fonts=packaged_fonts,
                )

                attempt["verification_finished_at"] = _utc_now_iso()
                attempt["verification_duration_seconds"] = round(max(0.0, time.monotonic() - verification_started_monotonic), 2)
                attempt["package_finished_at"] = attempt["verification_finished_at"]

                pkg_bytes = package_meta["total_bytes"]
                # Persist a cryptographic identity beside every verified local package.
                # Reuse checks compare this record before skipping an encode.
                try:
                    source_hash = hashlib.sha256()
                    with source_path.open("rb") as source_file:
                        for chunk in iter(lambda: source_file.read(8 * 1024 * 1024), b""):
                            source_hash.update(chunk)
                    identity = {
                        "schema_version": 1,
                        **preset_snapshot,
                        "default_preset": preset_name,
                        "selected_qualities": list(allowed_qualities),
                        "four_k_video_mode": _as_dict(transcode_res.get("rendition_methods")).get("2160p", {}).get("four_k_video_mode", "NOT_SELECTED"),
                        "four_k_audio_mode": _as_dict(transcode_res.get("rendition_methods")).get("2160p", {}).get("four_k_audio_mode", "NOT_SELECTED"),
                        "source_sha256": source_hash.hexdigest(),
                        "source_bytes": source_path.stat().st_size,
                        "source_mtime_ns": source_path.stat().st_mtime_ns,
                        "qualities": allowed_qualities,
                        "source_resolution": f"{int(probe_info.get('width') or 0)}x{int(probe_info.get('height') or 0)}",
                        "encoder_choice": encoder_choice,
                        "encoder_used": transcode_res.get("encoder", "unknown"),
                        "rendition_methods": transcode_res.get("rendition_methods", {}),
                        "gpu": _as_dict(_as_dict(next(iter(_as_dict(attempt.get("renditions")).values()), {})).get("profile")).get("gpu", "unknown"),
                        "subtitle_source": sub_src,
                        "subtitle_stream_index": sub_stream_idx,
                        "codec": "h264",
                        "hls_segment_duration": hls_segment_duration,
                        "subtitle_mode": "softsub",
                        "subtitle_path": str(sub_path_str or ""),
                        "subtitle_sha256": _sha256_if_file(sub_path_str),
                        "fonts": [{"path": item.get("path"), "sha256": item.get("sha256")} for item in packaged_fonts],
                        "created_at": _utc_now_iso(),
                    }
                    identity_path = work_dir / "studio_package_identity.json"
                    identity_tmp = identity_path.with_suffix(".tmp")
                    identity_tmp.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
                    identity_tmp.replace(identity_path)
                    attempt["package_identity"] = identity
                except Exception as identity_error:
                    logger.warning("Could not record package identity for %s: %s", source_path.name, identity_error)

                cleanup_result = cleanup_final_package_intermediates(work_dir, apply=True)
                # Re-run the full HLS/package validator after deleting only the
                # classifier's allowlisted intermediates. Any failure blocks READY.
                package_meta = build_and_verify_package(
                    package_dir=work_dir,
                    title=title,
                    description=json.dumps(_as_dict(ep.get("settings")).get("upload_destination", {}), ensure_ascii=False),
                    duration_seconds=duration,
                    renditions=transcode_res["renditions"],
                    subtitles=declared_subtitles,
                    fonts=packaged_fonts,
                )
                final_inventory = build_final_package_inventory(work_dir, verify_hashes=True)
                pkg_bytes = final_inventory["declared_bytes"]
                logger.info(
                    "[Episode %02d] Final package inventory: %d declared files, %d bytes; removed %d known intermediates (%d bytes); preserved %d unknown files",
                    order_index,
                    final_inventory["file_count"],
                    final_inventory["declared_bytes"],
                    len(cleanup_result["deleted"]),
                    cleanup_result["freed_bytes"],
                    len(final_inventory["unknown_preserve"]),
                )

                # -------------------------------------------------------------
                # 6. READY_TO_UPLOAD (Phase 2A terminal state)
                # -------------------------------------------------------------
                logger.info("[Episode %02d] Step 6: READY_TO_UPLOAD achieved! (%s bytes)", order_index, pkg_bytes)
                local_ready_at = _utc_now_iso()
                attempt["local_ready_at"] = local_ready_at
                attempt["ready_at"] = local_ready_at
                attempt["total_pipeline_duration_seconds"] = round(max(0.0, time.monotonic() - pipeline_started_monotonic), 2)
                finish_attempt(True)
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.READY_TO_UPLOAD.value,
                    current_stage=EpisodeState.READY_TO_UPLOAD.value,
                    progress=100.0,
                    fps=0.0,
                    speed="0x",
                    eta="00:00:00",
                    package_bytes=pkg_bytes,
                    error=None,
                )

                self._telemetry[ep_id] = {
                    "progress": 100.0,
                    "rendition_progress": 100.0,
                    "fps": 0.0,
                    "speed": "0x",
                    "eta": "00:00:00",
                    "elapsed_seconds": round(max(0.0, time.monotonic() - pipeline_started_monotonic), 1),
                    "encoder_used": transcode_res.get("encoder", ""),
                    "renditions": list(allowed_qualities),
                    "current_quality": "",
                    "current_stage": EpisodeState.READY_TO_UPLOAD.value,
                    "package_bytes": pkg_bytes,
                }
                self._report_local_processing(ep_id, EpisodeState.READY_TO_UPLOAD.value)

                return True

            except EncodingControlStop:
                persist_encoding_measurement()
                finish_attempt(False)
                cancelled = self._worker_stop_event.is_set()
                state = EpisodeState.CANCELLED.value if cancelled else EpisodeState.READY_TO_ENCODE.value
                self.store.update_episode(ep_id, state=state, current_stage="CANCELLED" if cancelled else "PAUSED", error=None, fps=0.0, speed="0x", eta="00:00:00")
                self._telemetry[ep_id].update(current_stage="CANCELLED" if cancelled else "PAUSED", fps=0.0, speed="0x", eta="00:00:00")
                return False
            except DiskSafetyPause as e:
                persist_encoding_measurement()
                finish_attempt(False)
                logger.warning("[Episode %02d] Safe disk pause: %s", order_index, str(e))
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.READY_TO_ENCODE.value,
                    current_stage="PAUSED_DISK",
                    error=str(e),
                )
                self._telemetry[ep_id] = {
                    "progress": 8.0,
                    "current_stage": "PAUSED_DISK",
                    "resource_warning": str(e),
                }
                return False
            except Exception as e:
                persist_encoding_measurement()
                finish_attempt(False)
                logger.error("[Episode %02d] Error during processing: %s", order_index, str(e), exc_info=True)
                self.store.update_episode(
                    ep_id,
                    state=EpisodeState.FAILED.value,
                    current_stage="FAILED",
                    fps=0.0,
                    speed="0x",
                    eta="00:00:00",
                    error=str(e),
                )
                self._telemetry[ep_id] = {
                    "progress": 0.0,
                    "rendition_progress": 0.0,
                    "fps": 0.0,
                    "speed": "0x",
                    "eta": "00:00:00",
                    "current_quality": "",
                    "current_stage": "FAILED",
                    "package_bytes": 0,
                }
                self._report_local_processing(ep_id, EpisodeState.FAILED.value)
                return False

    # =========================================================================
    # PHASE 3A: AUTHORITATIVE DESTINATION & RESUMABLE UPLOAD
    # =========================================================================

    def update_authoritative_destination(
        self,
        batch_id: str,
        folder_id: Optional[str] = None,
        series_id: Optional[str] = None,
        season_id: Optional[str] = None,
        destination_label: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Sets or updates authoritative destination fields on a persistent batch."""
        up_fields: Dict[str, Any] = {}
        if folder_id is not None:
            up_fields["target_folder_id"] = folder_id.strip()
        if series_id is not None:
            up_fields["target_series_id"] = series_id.strip()
        if season_id is not None:
            up_fields["target_season_id"] = season_id.strip()
        if destination_label is not None:
            up_fields["destination_label"] = destination_label.strip()

        return self.store.update_batch(batch_id, **up_fields)

    def upload_episode(
        self,
        episode_id: str,
        server_url: Optional[str] = None,
        api_key: Optional[str] = None,
        http_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Uploads a verified local package for an episode in READY_TO_UPLOAD to ABOODVIDEO server.
        Transitions: READY_TO_UPLOAD -> UPLOADING -> SERVER_VERIFYING -> READY
        """
        try:
            with self._upload_lock:
                ep = self.store.get_episode(episode_id)
                if not ep:
                    raise ValueError(f"الحلقة غير موجودة: {episode_id}")

                allowed_upload_states = {
                    EpisodeState.READY_TO_UPLOAD.value,
                    EpisodeState.UPLOADING.value,
                    EpisodeState.SERVER_VERIFYING.value,
                    EpisodeState.FAILED.value,
                }
                if ep["state"] not in allowed_upload_states:
                    raise ValueError(f"الحلقة ليست جاهزة للرفع (حالتها الحالية: {ep['state']})")

                # Never trust READY_TO_UPLOAD alone. A legacy output-path collision
                # can leave a valid package that belongs to a different episode.
                # Verify its signed-by-hash local identity against this source row
                # before any upload state or server session is created.
                self._current_uploading_ep_id = episode_id
                self._telemetry[episode_id] = {"progress": 0, "uploaded_bytes": ep.get("uploaded_bytes", 0), "current_stage": "hashing_source"}
                try:
                    def source_progress(done, total):
                        self._telemetry[episode_id].update(preparation_progress=done * 100 / max(1, total), current_stage="hashing_source")
                    package_check = self.inspect_local_package(episode_id, verify_files=False, cancel_event=self._current_upload_cancel_event, progress_callback=source_progress)
                except Exception:
                    self._current_uploading_ep_id = None
                    raise
                if not package_check.get("compatible"):
                    self._current_uploading_ep_id = None
                    reason = package_check.get("reason") or "تعذر إثبات تطابق الحزمة مع ملف المصدر."
                    raise ValueError(f"تم إيقاف الرفع لحماية المحتوى: {reason}")

                # Load configuration
                cfg = load_studio_config()
                active_server_url = (server_url or cfg.get("server_url") or "http://localhost:8000/api/v1").strip()
                active_api_key = (api_key or cfg.get("api_key") or "").strip()

                if not active_api_key:
                    raise ValueError("مفتاح API الخاص بالاستوديو غير مضبوط. يرجى إدخال مفتاح API في الإعدادات.")

                # Ensure local verified package exists
                work_dir_str = ep.get("work_dir")
                if not work_dir_str:
                    raise FileNotFoundError("مجلد الحزمة غير مسجل في بيانات الحلقة.")
                work_dir = Path(work_dir_str)
                manifest_file = work_dir / "manifest.json"
                if not manifest_file.exists():
                    raise FileNotFoundError(f"ملف manifest.json مفقود في مجلد الحزمة: {work_dir}")

                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)

                settings = _as_dict(ep.get("settings"))
                history = _as_dict(settings.get("processing_history"))
                history.setdefault("schema_version", 1)
                history["attempts"] = _as_list(history.get("attempts"))
                upload_attempts = _as_list(history.get("upload_attempts"))
                history["upload_attempts"] = upload_attempts
                upload_attempt = {"attempt_number": len(upload_attempts) + 1, "upload_started_at": _utc_now_iso(), "success": False}
                upload_attempts.append(upload_attempt)
                history["upload_attempt_count"] = len(upload_attempts)
                history["upload_retry_count"] = max(0, len(upload_attempts) - 1)

                def save_upload_history() -> None:
                    settings["processing_history"] = history
                    self.store.update_episode(episode_id, settings_json=settings)

                save_upload_history()

                # Assemble authoritative destination
                batch = self.store.get_batch(ep["batch_id"])
                destination: Dict[str, Any] = {}
                if batch:
                    if batch.get("target_folder_id"):
                        destination["folder_id"] = batch["target_folder_id"]
                    if batch.get("target_series_id"):
                        destination["series_id"] = batch["target_series_id"]
                    if batch.get("target_season_id"):
                        destination["season_id"] = batch["target_season_id"]
                destination["episode_number"] = ep.get("episode_number") if ep.get("episode_number") is not None else ep.get("order_index", 1)

                # Existing session & video IDs for resumption
                sess_id = ep.get("upload_session_id") or ep.get("session_id") or None
                v_id = ep.get("server_video_id") or ep.get("video_id") or None

                self._current_uploading_ep_id = episode_id
                self.store.update_episode(
                    episode_id,
                    state=EpisodeState.UPLOADING.value,
                    current_stage=EpisodeState.UPLOADING.value,
                    upload_started_at=upload_attempt["upload_started_at"],
                    server_status="UPLOADING",
                    last_upload_error="",
                )
                self._report_local_processing(episode_id, EpisodeState.UPLOADING.value)

                last_db_persist = 0.0
                upload_started_monotonic = time.monotonic()
                server_verification_started_monotonic: Optional[float] = None
                server_verification_started_at: Optional[str] = None

                def on_upload_progress(p_data: Dict[str, Any]):
                    nonlocal last_db_persist, server_verification_started_monotonic, server_verification_started_at
                    stage = p_data.get("stage")
                    if stage == "server_verifying" and server_verification_started_monotonic is None:
                        server_verification_started_monotonic = time.monotonic()
                        server_verification_started_at = _utc_now_iso()
                        upload_attempt["verification_started_at"] = server_verification_started_at
                    curr_session_id = p_data.get("session_id")
                    curr_video_id = p_data.get("video_id")
                    s_status = p_data.get("server_status", "")
                    previous = self._telemetry.get(episode_id, {})
                    pct = p_data.get("upload_progress_pct", previous.get("progress", 0.0))
                    up_bytes = p_data.get("uploaded_bytes", previous.get("uploaded_bytes", ep.get("uploaded_bytes", 0)))
                    total_b = p_data.get("total_bytes", previous.get("package_bytes", ep.get("package_bytes", 0)))
                    speed_str = p_data.get("upload_speed_formatted") or f"{p_data.get('upload_speed_mbps', 0.0):.2f} MB/s"
                    eta_str = p_data.get("eta_formatted") or "00:00:00"
                    upload_elapsed = round(max(0.0, time.monotonic() - upload_started_monotonic), 1)

                    ep_state = EpisodeState.UPLOADING.value
                    if stage in ("finalizing", "server_verifying"):
                        ep_state = EpisodeState.SERVER_VERIFYING.value
                    elif stage == "completed":
                        ep_state = EpisodeState.READY.value

                    self._telemetry[episode_id] = {
                        "progress": pct,
                        "uploaded_bytes": up_bytes,
                        "package_bytes": total_b or ep.get("package_bytes", 0),
                        "upload_speed": speed_str,
                        "upload_eta": eta_str,
                        "upload_elapsed_seconds": upload_elapsed,
                        "server_status": s_status,
                        "current_stage": stage or ep_state,
                        "transfer_progress": pct,
                        "preparation_progress": p_data.get("preparation_progress", previous.get("preparation_progress")),
                        "verification_progress": 100 if stage == "completed" else None,
                        "session_id": curr_session_id,
                        "video_id": curr_video_id,
                    }
                    self._report_local_processing(episode_id, ep_state)

                    now = time.time()
                    if now - last_db_persist >= 1.0 or stage in ("finalizing", "server_verifying", "completed"):
                        up_fields: Dict[str, Any] = {
                            "progress": pct,
                            "uploaded_bytes": up_bytes,
                            "upload_speed": speed_str,
                            "upload_eta": eta_str,
                            "server_status": s_status,
                            "state": ep_state,
                            "current_stage": stage or ep_state,
                        }
                        if curr_session_id:
                            up_fields["upload_session_id"] = str(curr_session_id)
                        if curr_video_id:
                            up_fields["server_video_id"] = str(curr_video_id)
                        if stage == "completed":
                            up_fields["completed_at"] = _utc_now_iso()
                            up_fields["last_upload_error"] = ""
                        self.store.update_episode(episode_id, **up_fields)
                        last_db_persist = now

                try:
                    res = upload_and_verify_package(
                        server_url=active_server_url,
                        api_key=active_api_key,
                        package_dir=work_dir,
                        manifest=manifest,
                        video_id=v_id,
                        session_id=sess_id,
                        destination=destination,
                        progress_callback=on_upload_progress,
                        cancel_event=self._current_upload_cancel_event,
                        http_client=http_client,
                        host_header=cfg.get("server_host_header"),
                    )

                    upload_finished_at = _utc_now_iso()
                    upload_elapsed = round(max(0.0, time.monotonic() - upload_started_monotonic), 2)
                    verification_elapsed = round(max(0.0, time.monotonic() - server_verification_started_monotonic), 2) if server_verification_started_monotonic is not None else 0.0
                    upload_attempt.update({
                        "upload_finished_at": upload_finished_at,
                        "upload_duration_seconds": upload_elapsed,
                        "verification_started_at": server_verification_started_at,
                        "verification_duration_seconds": verification_elapsed,
                        "ready_at": upload_finished_at,
                        "success": True,
                    })
                    history["successful_upload"] = upload_attempt
                    successful = _as_dict(history.get("successful"))
                    attempts_history = [_as_dict(item) for item in _as_list(history.get("attempts"))]
                    if successful:
                        successful_attempts = [
                            item for item in attempts_history if item.get("success")
                            and item.get("attempt_number") == successful.get("attempt_number")
                        ]
                        records = [successful] + successful_attempts
                        for record in records:
                            record["upload_started_at"] = upload_attempt["upload_started_at"]
                            record["upload_finished_at"] = upload_finished_at
                            record["upload_duration_seconds"] = upload_elapsed
                            record["verification_duration_seconds"] = float(record.get("verification_duration_seconds") or 0) + verification_elapsed
                            record["ready_at"] = upload_finished_at
                        try:
                            started_epoch = datetime.fromisoformat(successful["started_at"]).timestamp()
                            elapsed_pipeline = round(max(0.0, time.time() - started_epoch), 2)
                            for record in records:
                                record["total_pipeline_duration_seconds"] = elapsed_pipeline
                        except (KeyError, TypeError, ValueError):
                            pass
                    save_upload_history()

                    self.store.update_episode(
                        episode_id,
                        state=EpisodeState.READY.value,
                        current_stage="COMPLETED",
                        progress=100.0,
                        server_status="READY",
                        completed_at=_utc_now_iso(),
                        last_upload_error="",
                    )
                    self._telemetry[episode_id] = {
                        "progress": 100.0,
                        "uploaded_bytes": ep.get("package_bytes", 0),
                        "package_bytes": ep.get("package_bytes", 0),
                        "upload_speed": "0 KB/s",
                        "upload_eta": "00:00:00",
                        "server_status": "READY",
                        "current_stage": EpisodeState.READY.value,
                    }
                    self._report_local_processing(episode_id, EpisodeState.READY.value)

                    return res

                except Exception as e:
                    logger.error("Upload failed for episode %s: %s", episode_id, e, exc_info=True)
                    upload_attempt.update({
                        "upload_finished_at": _utc_now_iso(),
                        "upload_duration_seconds": round(max(0.0, time.monotonic() - upload_started_monotonic), 2),
                        "success": False,
                        "error": str(e),
                    })
                    save_upload_history()
                    self.store.update_episode(
                        episode_id,
                        state=EpisodeState.CANCELLED.value if self._current_upload_cancel_event.is_set() else EpisodeState.FAILED.value,
                        current_stage="CANCELLED" if self._current_upload_cancel_event.is_set() else "FAILED",
                        server_status="CANCELLED" if self._current_upload_cancel_event.is_set() else "FAILED",
                        last_upload_error=str(e),
                    )
                    self._report_local_processing(episode_id, EpisodeState.FAILED.value)
                    raise
                finally:
                    self._current_uploading_ep_id = None
        finally:
            self._current_uploading_ep_id = None

    def clean_episode_local_package(
        self, episode_id: str, is_auto: bool = False
    ) -> Dict[str, Any]:
        """Keep generated packages; local outputs are retained for later re-encoding."""
        raise ValueError("سياسة الاستوديو تحفظ الحزم المحلية دائماً ولا تحذف النسخ المحوّلة.")

    def start_batch_upload(
        self,
        batch_id: Optional[str] = None,
        server_url: Optional[str] = None,
        api_key: Optional[str] = None,
        batch_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Starts background sequential upload of all episodes in READY_TO_UPLOAD (Concurrency = 1)."""
        bid = batch_id or self._active_batch_id
        if not bid:
            raise ValueError("لا توجد دفعة موسم محددة للرفع.")

        with self._upload_worker_start_lock:
            if batch_ids:
                batches = [self.store.get_batch(x) for x in batch_ids]
            elif batch_id:
                batches = [self.store.get_batch(batch_id)]
            else:
                # The Studio upload page shows ready items from every imported batch.
                # Its bulk action must therefore drain all eligible batches, not only
                # whichever batch happens to be selected for encoding.
                batches = sorted(self.store.list_batches(), key=lambda item: item.get("created_at") or "")
            eligible_batch_ids: List[str] = []
            ready_count = 0
            for batch in batches:
                if not batch or batch.get("status") not in {"ACTIVE", "PAUSED", "COMPLETED_LOCAL"}:
                    continue
                batch_episodes = self.store.get_episodes(batch["id"])
                count = sum(ep["state"] in {EpisodeState.READY_TO_UPLOAD.value, EpisodeState.UPLOADING.value, EpisodeState.SERVER_VERIFYING.value} for ep in batch_episodes)
                if count:
                    eligible_batch_ids.append(batch["id"])
                    ready_count += count
            if not ready_count:
                raise ValueError("لا توجد حلقات جاهزة للرفع في الدفعات الحالية.")
            if self.is_uploading:
                return {"status": "ALREADY_RUNNING", "batch_id": bid, "message": "عملية الرفع جارية بالفعل."}

            self._current_upload_cancel_event.clear()
            self._pause_requested = False
            self._manual_upload_batches = list(eligible_batch_ids)
            logger.warning("SESSION_AUTHORIZATION_GRANTED action=UPLOAD_BATCH batches=%d ready=%d", len(eligible_batch_ids), ready_count)
            self._upload_worker_thread = threading.Thread(
                target=self._run_batch_upload,
                args=(eligible_batch_ids, server_url, api_key),
                daemon=True,
                name="SeasonManualUploader",
            )
            self._upload_worker_thread.start()
        return {"status": "STARTED", "batch_id": bid, "batch_ids": eligible_batch_ids, "ready_count": ready_count, "message": "تم بدء الرفع التتابعي للحلقات الجاهزة."}

    def _run_batch_upload(
        self,
        batch_ids: Union[str, List[str]],
        server_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        """Sequential batch upload runner (Concurrency = 1)."""
        selected_batch_ids = [batch_ids] if isinstance(batch_ids, str) else list(batch_ids)
        logger.info("Starting sequential upload runner for %d batches...", len(selected_batch_ids))
        for batch_id in selected_batch_ids:
            episodes = self.store.get_episodes(batch_id)
            ready_episodes = [
                ep for ep in episodes
                if ep["state"] in (EpisodeState.READY_TO_UPLOAD.value, EpisodeState.UPLOADING.value, EpisodeState.SERVER_VERIFYING.value)
            ]
            for ep in ready_episodes:
                if self._pause_requested or self._stop_after_current_requested:
                    self.store.update_batch(batch_id, status="PAUSED")
                    self._stop_after_current_requested = False
                    return
                if self._current_upload_cancel_event.is_set():
                    logger.info("Upload batch runner cancelled or stopped.")
                    return
                try:
                    self._active_uploads_count += 1
                    try:
                        self.upload_episode(ep["id"], server_url=server_url, api_key=api_key)
                    finally:
                        self._active_uploads_count -= 1
                except Exception as e:
                    logger.error("Sequential upload halted due to failure on episode %s: %s", ep["filename"], e)
                    return
        for batch_id in selected_batch_ids:
            if all(ep["state"] in {EpisodeState.READY.value, EpisodeState.SKIPPED.value} for ep in self.store.get_episodes(batch_id)):
                self.store.update_batch(batch_id, status="COMPLETED")
        self._manual_upload_batches = []
        logger.info("Sequential upload runner finished for %d batches.", len(selected_batch_ids))

    # =========================================================================
    # PHASE 3B: PIPELINED SEASON PROCESSING + UPLOAD
    # =========================================================================

    def start_automatic_pipeline(
        self,
        batch_id: Optional[str] = None,
        server_url: Optional[str] = None,
        api_key: Optional[str] = None,
        http_client: Optional[Any] = None,
        pipeline_window: int = 1,
    ) -> Dict[str, Any]:
        """Starts automatic pipelined workflow:
        GPU: Episode N+1 encodes locally WHILE
        NETWORK: Episode N uploads to ABOODVIDEO.
        Strict limits:
        - Max 1 simultaneous encode (NVENC / FFmpeg concurrency = 1)
        - Max 1 simultaneous upload (Network concurrency = 1)
        - Strict upload ordering: 01 -> 02 -> 03...
        - Bounded pipeline window: max 1 pre-encoded episode ahead
        """
        bid = batch_id or self._active_batch_id
        if not bid:
            raise ValueError("لا توجد دفعة موسم محددة للتشغيل التلقائي.")

        batch = self.store.get_batch(bid)
        if not batch or batch.get("status") in ("FAILED", "COMPLETED", "COMPLETED_LOCAL", "DELETED"):
            raise ValueError("لا يمكن تشغيل دفعة فاشلة أو مكتملة.")
        if self.is_uploading or self.is_running:
            raise ValueError("أوقف التحويل أو الرفع الجاري مؤقتاً قبل بدء خط التحويل والرفع التلقائي.")

        self._configure_execution()
        with self._encoder_launch_lock:
            self._active_batch_id = bid
            self._work_authorized = False
            self._run_authorization = None
            self.store.update_batch(bid, status="ACTIVE", mode="AUTOMATIC_PIPELINED")
            self._run_authorization = (self._studio_session_id, bid, uuid.uuid4().hex)
            self._work_authorized = True
            self.pipeline_window = max(pipeline_window,self._execution_config["cpu_concurrent_jobs"],self._execution_config["gpu_concurrent_jobs"])
            self._pause_requested = False
            self._stop_after_current_requested = False
            self._worker_stop_event.clear()
            self._current_upload_cancel_event.clear()
            self._current_cancel_event.clear()
            logger.warning("SESSION_AUTHORIZATION_GRANTED action=START_AUTOMATIC_PIPELINE batch_id=%s queued=%d", bid, len(self.store.get_episodes(bid)))
            logger.info("Explicit automatic-pipeline authorization granted for active batch %s in this Studio session.", bid)

        # Update saved connection credentials
        if server_url:
            self._server_url = server_url
        if api_key:
            self._api_key = api_key
        if http_client:
            self._http_client = http_client

        # Launch Encoder Thread if not running
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(
                target=self._pipelined_encoder_loop,
                daemon=True,
                name="SeasonPipelinedEncoder",
            )
            self._worker_thread.start()

        # Launch Uploader Thread if not running
        if self._upload_worker_thread is None or not self._upload_worker_thread.is_alive():
            if self._execution_config["processing_mode"] != "server":
                self._upload_worker_thread = threading.Thread(
                    target=self._pipelined_upload_loop,
                    daemon=True,
                    name="SeasonPipelinedUploader",
                )
                self._upload_worker_thread.start()


        return self.get_current_batch_data()

    def _pipelined_encoder_loop(self) -> None:
        """Background encoder worker loop for pipelined processing.
        Encodes eligible episodes up to the pipeline_window limit.
        Guarantees max 1 active encode at any time.
        """
        if self._execution_config["processing_mode"] == "server" or max(self._execution_config["cpu_concurrent_jobs"], self._execution_config["gpu_concurrent_jobs"]) > 1:
            return self._configured_worker_loop(pipelined=True)
        logger.info("Starting pipelined encoder loop for batch: %s", self._active_batch_id)
        while not self._worker_stop_event.is_set():
            if not self._active_batch_id:
                break
            if not self._worker_may_claim():
                logger.info("Pipelined encoder has no active user authorization; leaving queued episodes untouched.")
                break
            if self._pause_requested:
                logger.info("Encoder loop halting due to pause request.")
                break

            episodes = self.store.get_episodes(self._active_batch_id)
            if not episodes:
                break

            # Check if any episode failed or needs review
            failed_ep = next(
                (ep for ep in episodes if ep["state"] in (EpisodeState.FAILED.value, EpisodeState.NEEDS_REVIEW.value)),
                None,
            )
            if failed_ep:
                logger.warning("Encountered failed/review episode %s. Halting encoder loop.", failed_ep["order_index"])
                self._pause_requested = True
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                with self._pipeline_condition:
                    self._pipeline_condition.notify_all()
                break

            # Check if all episodes have completed local encoding or are already skipped/ready
            all_encoded = all(
                ep["state"] in (
                    EpisodeState.READY_TO_UPLOAD.value,
                    EpisodeState.UPLOADING.value,
                    EpisodeState.SERVER_VERIFYING.value,
                    EpisodeState.READY.value,
                    EpisodeState.SKIPPED.value,
                )
                for ep in episodes
            )
            if all_encoded:
                logger.info("All episodes in batch %s have finished local encoding.", self._active_batch_id)
                with self._pipeline_condition:
                    self._pipeline_condition.notify_all()
                break

            # Check Pipeline Window:
            # How many pre-encoded episodes are currently waiting in READY_TO_UPLOAD?
            ready_to_upload_eps = [
                ep for ep in episodes if ep["state"] == EpisodeState.READY_TO_UPLOAD.value
            ]
            if len(ready_to_upload_eps) >= self.pipeline_window:
                # Window is full! Wait until uploader promotes one of these to UPLOADING
                with self._pipeline_condition:
                    self._pipeline_condition.wait(timeout=0.5)
                continue

            # Find next episode to encode (lowest order_index in PENDING or READY_TO_ENCODE)
            next_to_encode = next(
                (ep for ep in episodes if ep["state"] in (EpisodeState.PENDING.value, EpisodeState.READY_TO_ENCODE.value)),
                None,
            )
            if not next_to_encode:
                with self._pipeline_condition:
                    self._pipeline_condition.wait(timeout=0.5)
                continue

            # Check Disk Space Guard before starting encode
            ok, err_msg = self.check_disk_space_guard(next_to_encode)
            if not ok:
                logger.error("Disk space guard failed for episode %s: %s", next_to_encode["order_index"], err_msg)
                self.store.update_episode(
                    next_to_encode["id"],
                    state=EpisodeState.FAILED.value,
                    error=err_msg,
                )
                self._pause_requested = True
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                with self._pipeline_condition:
                    self._pipeline_condition.notify_all()
                break

            # Execute single episode encode under _encode_lock
            with self._encode_lock:
                self._max_observed_encodes = max(self._max_observed_encodes, self._active_encodes_count)
                self._record_event("encode_start", next_to_encode["id"], next_to_encode["order_index"])
                try:
                    success = self._process_single_episode(next_to_encode)
                finally:
                    self._record_event("encode_end", next_to_encode["id"], next_to_encode["order_index"])
                    with self._pipeline_condition:
                        self._pipeline_condition.notify_all()

            if not success:
                logger.warning("Episode %s encode failed. Halting pipeline.", next_to_encode["order_index"])
                self._pause_requested = True
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                with self._pipeline_condition:
                    self._pipeline_condition.notify_all()
                break

            if self._stop_after_current_requested:
                logger.info("Stop-after-current satisfied for encoder.")
                self._pause_requested = True
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                self._stop_after_current_requested = False
                with self._pipeline_condition:
                    self._pipeline_condition.notify_all()
                break

            time.sleep(0.05)

        logger.info("Pipelined encoder loop exited.")

    def _pipelined_upload_loop(self) -> None:
        """Background uploader worker loop for pipelined processing.
        Enforces STRICT upload order (01 -> 02 -> 03...).
        Episode N+1 CANNOT begin uploading before Episode N reaches server READY.
        Guarantees max 1 active upload at any time.
        """
        logger.info("Starting pipelined upload loop for batch: %s", self._active_batch_id)
        while not self._worker_stop_event.is_set() and not self._current_upload_cancel_event.is_set():
            if not self._active_batch_id:
                break
            if not self._worker_may_claim():
                logger.info("Pipelined uploader has no active user authorization; leaving queued episodes untouched.")
                break
            if self._pause_requested:
                logger.info("Upload loop halting due to pause request.")
                break

            episodes = self.store.get_episodes(self._active_batch_id)
            if not episodes:
                break

            # STRICT ORDER RULE: Find the lowest order_index episode that is not READY and not SKIPPED
            unfinished = [
                ep for ep in episodes 
                if ep["state"] not in (EpisodeState.READY.value, EpisodeState.SKIPPED.value)
            ]
            if not unfinished:
                # All episodes have reached READY or SKIPPED!
                logger.info("All episodes in batch %s have reached server READY!", self._active_batch_id)
                self.store.update_batch(self._active_batch_id, status="COMPLETED")
                break

            target = unfinished[0]  # MUST be the lowest unfinished episode!

            # Check target state
            if target["state"] == EpisodeState.FAILED.value:
                # Cannot proceed past a failed episode!
                logger.warning("Target episode %s (%s) is FAILED. Halting upload pipeline.", target["order_index"], target["filename"])
                self._pause_requested = True
                self.store.update_batch(self._active_batch_id, status="PAUSED")
                break

            if target["state"] in (
                EpisodeState.READY_TO_UPLOAD.value,
                EpisodeState.UPLOADING.value,
                EpisodeState.SERVER_VERIFYING.value,
            ):
                # Target episode is ready for upload!
                with self._upload_lock:
                    self._active_uploads_count += 1
                    self._max_observed_uploads = max(self._max_observed_uploads, self._active_uploads_count)
                    self._record_event("upload_start", target["id"], target["order_index"])
                    self.store.update_episode(
                        target["id"],
                        state=EpisodeState.UPLOADING.value,
                        current_stage=EpisodeState.UPLOADING.value,
                    )
                    with self._pipeline_condition:
                        # Wakes encoder because target transitioned to UPLOADING / ready_to_upload decreased
                        self._pipeline_condition.notify_all()

                    try:
                        self.upload_episode(
                            target["id"],
                            server_url=self._server_url,
                            api_key=self._api_key,
                            http_client=self._http_client,
                        )
                    except Exception as e:
                        logger.error("Upload error for episode %s: %s", target["order_index"], e)
                        self._pause_requested = True
                        self.store.update_batch(self._active_batch_id, status="PAUSED")
                        break
                    finally:
                        self._active_uploads_count -= 1
                        self._record_event("upload_end", target["id"], target["order_index"])
                        with self._pipeline_condition:
                            self._pipeline_condition.notify_all()

                if self._stop_after_current_requested:
                    logger.info("Stop-after-current satisfied for uploader.")
                    self._pause_requested = True
                    self.store.update_batch(self._active_batch_id, status="PAUSED")
                    self._stop_after_current_requested = False
                    with self._pipeline_condition:
                        self._pipeline_condition.notify_all()
                    break

            else:
                # Target episode is still being processed locally (PENDING, ENCODING, PACKAGING, etc.)
                # Uploader MUST wait!
                with self._pipeline_condition:
                    self._pipeline_condition.wait(timeout=0.5)

            time.sleep(0.05)

        logger.info("Pipelined upload loop exited.")


# Default singleton batch runner instance for studio web service
batch_runner = BatchRunner()
