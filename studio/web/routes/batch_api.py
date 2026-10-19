"""REST API routes for Season Batch Queue management in ABOODVIDEO Studio."""
import os
import logging
import re
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from studio.core.batch_runner import batch_runner
from studio.core.batch_store import EpisodeState, _as_dict
from studio.config import get_converted_output_root
from studio.core.picker import select_folder
from studio.core.episode_scanner import natural_sort_key, discover_season_videos
from studio.core.presets import normalize_preset_name, preset_identity, selected_qualities, expected_output_size

router = APIRouter(prefix="/api/batch")
logger = logging.getLogger("studio.batch_api")


class ImportBatchRequest(BaseModel):
    folder_path: str
    destination_label: Optional[str] = ""
    mode: Optional[str] = "SEQUENTIAL"
    encoder_choice: Optional[str] = None
    default_preset: Optional[str] = None


class ImportSeriesRequest(BaseModel):
    folder_path: str
    destination_label: Optional[str] = ""
    encoder_choice: Optional[str] = None
    default_preset: Optional[str] = None


class ImportFileRequest(BaseModel):
    file_path: str
    destination_label: Optional[str] = ""


class UpdateDestinationRequest(BaseModel):
    destination_label: str


class UpdateEncoderRequest(BaseModel):
    encoder: str


class UpdatePresetRequest(BaseModel):
    preset: str
    include_360p: Optional[bool] = None


class DeleteEpisodesRequest(BaseModel):
    episode_ids: List[str]
    expected_count: int


@router.post("/browse-folder")
async def browse_folder(request: Request, kind: str = "season"):
    """Opens native Windows folder picker for selecting a season directory (127.0.0.1 only)."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Localhost access only")

    if kind not in {"season", "series"}:
        raise HTTPException(status_code=400, detail="نوع المصدر غير صالح.")
    title = "اختر مجلد المسلسل الكامل" if kind == "series" else "اختر مجلد الموسم"
    path = await run_in_threadpool(select_folder, title=title)
    if not path:
        return {"cancelled": True, "path": ""}
    return {"cancelled": False, "path": str(Path(path).resolve())}


@router.post("/import")
async def import_season_batch(req: ImportBatchRequest):
    """Imports a local season folder and generates a persistent queue."""
    try:
        if req.encoder_choice and req.encoder_choice not in {"auto", "gpu", "cpu"}:
            raise ValueError("المشفّر المحدد غير صالح.")
        if req.default_preset and normalize_preset_name(req.default_preset) not in {"economy", "balanced", "quality"}:
            raise ValueError("إعداد الجودة المحدد غير صالح.")
        # Queue entries always target the configured permanent output root.
        # Validation happens before the batch is created, so a bad path cannot
        # leave an empty batch behind.
        if os.environ.get("ENVIRONMENT", "").casefold() != "test":
            from studio.config import load_studio_config
            if load_studio_config().get("processing_mode", "local") != "server":
                get_converted_output_root(require_configured=True)
        logger.info("IMPORT_FOLDER_SELECTED path=%s", req.folder_path)
        data = await run_in_threadpool(
            batch_runner.import_season_folder,
            req.folder_path,
            req.destination_label or "",
            req.mode or "SEQUENTIAL",
            req.encoder_choice,
            req.default_preset,
        )
        if not data.get("episodes"):
            raise ValueError("لم تُضف أي حلقات إلى قائمة الانتظار.")
        logger.info("EPISODES_DETECTED count=%d", len(data["episodes"]))
        logger.info("ADD_QUEUE_CLICKED source=season")
        logger.info("BATCH_CREATED batch=%s", (data.get("batch") or {}).get("id", "unknown"))
        logger.info("EPISODE_JOBS_CREATED count=%d", len(data["episodes"]))
        logger.info("DATABASE_COMMIT jobs=%d", data.get("new_episode_count", len(data["episodes"])))
        logger.info("QUEUE_RELOADED jobs=%d", len(data["episodes"]))
        return {"ok": True, **data}
    except Exception as e:
        logger.exception("ERROR season import failed")
        raise HTTPException(status_code=400, detail=f"تعذر إضافة الحلقات إلى قائمة الانتظار: {e}") from e


async def _inspect_source(path, kind):
    from studio.core.import_discovery import discover_import
    from studio.config import load_studio_config
    try:
        return await run_in_threadpool(discover_import, path, kind, load_studio_config())
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/inspect-file")
async def inspect_file(req: ImportFileRequest):
    return await _inspect_source(req.file_path, "file")


@router.post("/inspect-season")
async def inspect_season(req: ImportSeriesRequest):
    return await _inspect_source(req.folder_path, "season")


@router.post("/inspect-series")
async def inspect_series(req: ImportSeriesRequest):
    return await _inspect_source(req.folder_path, "series")


@router.post("/import-series")
async def import_series(req: ImportSeriesRequest):
    try:
        if req.encoder_choice and req.encoder_choice not in {"auto", "gpu", "cpu"}:
            raise ValueError("المشفّر المحدد غير صالح.")
        if req.default_preset and normalize_preset_name(req.default_preset) not in {"economy", "balanced", "quality"}:
            raise ValueError("إعداد الجودة المحدد غير صالح.")
        if os.environ.get("ENVIRONMENT", "").casefold() != "test":
            from studio.config import load_studio_config
            if load_studio_config().get("processing_mode", "local") != "server":
                get_converted_output_root(require_configured=True)
        logger.info("IMPORT_FOLDER_SELECTED path=%s kind=series", req.folder_path)
        data = await run_in_threadpool(
            batch_runner.import_series_folder, req.folder_path, req.destination_label or "",
            req.encoder_choice, req.default_preset,
        )
        logger.info("ADD_QUEUE_CLICKED source=series")
        logger.info("DATABASE_COMMIT series_jobs=%d", data.get("new_episode_count", len(data.get("episodes", []))))
        logger.info("QUEUE_RELOADED jobs=%d", len(data.get("episodes", [])))
        return data
    except Exception as exc:
        logger.exception("ERROR series import failed")
        raise HTTPException(status_code=400, detail=f"تعذر إضافة حلقات المسلسل إلى قائمة الانتظار: {exc}") from exc


@router.post("/import-file")
async def import_media_file(req: ImportFileRequest):
    try:
        data = await run_in_threadpool(batch_runner.import_media_file, req.file_path, req.destination_label or "")
        return {"ok": True, **data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/current")
async def get_current_batch():
    """Retrieve every durable queued episode, even when the queue spans seasons."""
    data = await run_in_threadpool(batch_runner.get_current_batch_data)
    data["batches"] = await run_in_threadpool(batch_runner.store.list_batches)
    return data


@router.post("/font-audits/refresh")
async def refresh_queued_font_audits():
    """Re-resolve stored ASS findings after the external font library changes."""
    try:
        return {"ok": True, **await run_in_threadpool(batch_runner.refresh_font_library_audits)}
    except Exception as exc:
        logger.exception("ERROR refreshing queued font audits")
        raise HTTPException(status_code=400, detail=f"تعذر إعادة فحص الخطوط: {exc}") from exc


@router.get("/history")
async def get_season_history():
    return await run_in_threadpool(batch_runner.get_season_history)


@router.delete("/episodes/{episode_id}")
async def delete_queue_episode(episode_id: str):
    """Delete a single episode from the queue."""
    try:
        success = await run_in_threadpool(batch_runner.delete_episode, episode_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=404, detail="الحلقة غير موجودة في الطابور.")
    return {"ok": True, "episode_id": episode_id}


@router.post("/episodes/delete-bulk")
async def delete_queue_episodes(req: DeleteEpisodesRequest):
    """Remove selected queue records atomically; converted output folders remain untouched."""
    ids = [str(value).strip() for value in req.episode_ids if str(value).strip()]
    if not ids or len(ids) != req.expected_count or len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="حدد الحلقات المطلوبة ثم أعد المحاولة.")
    try:
        removed = await run_in_threadpool(batch_runner.delete_episodes, ids, req.expected_count)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "removed": removed, "outputs_retained": True}


@router.post("/episodes/{episode_id}/clean-package")
async def clean_episode_package(episode_id: str):
    """Local packages are retained so an episode can be reused or re-encoded later."""
    raise HTTPException(status_code=409, detail="النسخ المحوّلة محفوظة محلياً ولا يمكن حذفها من الاستوديو.")


@router.get("/episodes/{episode_id}/package")
async def inspect_episode_package(episode_id: str):
    return await run_in_threadpool(batch_runner.inspect_local_package, episode_id)


@router.post("/episodes/{episode_id}/reupload")
async def reupload_episode_package(episode_id: str):
    try:
        return await run_in_threadpool(batch_runner.reupload_local_package, episode_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/episodes/{episode_id}/open-package")
async def open_episode_package(episode_id: str, request: Request):
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Localhost access only")
    episode = await run_in_threadpool(batch_runner.store.get_episode, episode_id)
    path = Path(episode.get("work_dir") or "") if episode else Path()
    if not episode or not path.is_dir():
        raise HTTPException(status_code=404, detail="مجلد الحزمة المحلية غير موجود.")
    try:
        os.startfile(str(path.resolve()))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"تعذر فتح مجلد الحزمة: {exc}")
    return {"ok": True}



@router.post("/clear")
@router.delete("/{batch_id}")
async def clear_season_batch(batch_id: Optional[str] = None):
    """Clears the season batch and all its episodes."""
    success = await run_in_threadpool(batch_runner.clear_batch, batch_id)
    return {"ok": success}


@router.post("/{batch_id}/destination")
async def update_batch_destination(batch_id: str, req: UpdateDestinationRequest):
    """Update destination display label."""
    res = await run_in_threadpool(batch_runner.update_destination, batch_id, req.destination_label)
    if not res:
        raise HTTPException(status_code=404, detail="الدفعة غير موجودة.")
    return {"ok": True, "batch": res}


class StartAutoPipelineRequest(BaseModel):
    pipeline_window: Optional[int] = 1


@router.post("/auto-pipeline/start")
async def start_auto_pipeline_endpoint(req: Optional[StartAutoPipelineRequest] = None):
    """Starts automatic pipelined workflow: GPU encodes N+1 while Network uploads N."""
    try:
        if os.environ.get("ENVIRONMENT", "").casefold() != "test":
            from studio.config import load_studio_config
            if load_studio_config().get("processing_mode", "local") != "server":
                get_converted_output_root(require_configured=True)
        window = req.pipeline_window if req and req.pipeline_window else 1
        data = await run_in_threadpool(batch_runner.start_automatic_pipeline, pipeline_window=window)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/start")
async def start_batch():
    """Start or resume sequential processing of the active batch."""
    try:
        if os.environ.get("ENVIRONMENT", "").casefold() != "test":
            from studio.config import load_studio_config
            if load_studio_config().get("processing_mode", "local") != "server":
                get_converted_output_root(require_configured=True)
        data = await run_in_threadpool(batch_runner.start_batch)
        logger.warning("PROCESS_ACTION action=START_BATCH caller=POST /start batch_id=%s", data.get("batch_id", ""))
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pause")
async def pause_batch():
    """Request batch pause after current unit of work finishes."""
    try:
        data = await run_in_threadpool(batch_runner.pause_batch)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/resume")
async def resume_batch():
    """Resume sequential batch processing."""
    try:
        data = await run_in_threadpool(batch_runner.resume_batch)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop-after-current")
async def stop_after_current():
    """Finish current episode to READY_TO_UPLOAD, then stop."""
    try:
        data = await run_in_threadpool(batch_runner.stop_after_current)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/episodes/{episode_id}/retry")
async def retry_episode(episode_id: str):
    """Retry a failed episode."""
    try:
        data = await run_in_threadpool(batch_runner.retry_episode, episode_id)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/episodes/{episode_id}/start")
async def start_single_episode(episode_id: str):
    """Explicitly process only the next eligible episode, then pause the batch."""
    try:
        data = await run_in_threadpool(batch_runner.start_episode, episode_id)
        return {"ok": True, **data}
    except Exception as exc:
        logger.exception("ERROR single episode start rejected or failed")
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/encoder")
async def update_episode_encoder(episode_id: str, req: UpdateEncoderRequest):
    episode = await run_in_threadpool(batch_runner.store.get_episode, episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="الحلقة غير موجودة.")
    if episode.get("state") in {EpisodeState.ENCODING.value, EpisodeState.PACKAGING.value, EpisodeState.UPLOADING.value}:
        raise HTTPException(status_code=409, detail="لا يمكن تغيير المشفر أثناء تشغيل المهمة.")
    settings = _as_dict(episode.get("settings"))
    settings["encoder_choice"] = req.encoder
    updated = await run_in_threadpool(batch_runner.store.update_episode, episode_id, settings_json=settings)
    return {"ok": True, "episode": updated}


@router.post("/episodes/{episode_id}/preset")
async def update_episode_preset(episode_id: str, req: UpdatePresetRequest):
    episode = await run_in_threadpool(batch_runner.store.get_episode, episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="الحلقة غير موجودة.")
    if episode.get("state") not in {EpisodeState.PENDING.value, EpisodeState.NEEDS_REVIEW.value, EpisodeState.READY_TO_ENCODE.value}:
        raise HTTPException(status_code=409, detail="يمكن تغيير الجودة قبل بدء معالجة الحلقة فقط.")
    name = normalize_preset_name(req.preset)
    if name not in {"economy", "balanced", "quality"}:
        raise HTTPException(status_code=400, detail="إعداد الجودة المحدد غير صالح.")
    settings = _as_dict(episode.get("settings"))
    include_360p = bool(settings.get("include_360p", False) if req.include_360p is None else req.include_360p)
    snapshot = preset_identity(name, include_360p)
    qualities = selected_qualities(int(episode.get("source_width") or settings.get("source_width", 0) or 0), int(episode.get("source_height") or settings.get("source_height", 0) or 0), include_360p)
    estimate = expected_output_size(
        float(episode.get("duration_seconds") or settings.get("duration_seconds") or 0), name, include_360p, qualities,
        four_k_copy=settings.get("four_k_video_mode") == "COPY",
        source_file_bytes=int(episode.get("source_bytes") or settings.get("source_bytes") or 0),
        source_video_bitrate_bps=int(settings.get("source_video_bitrate_bps") or 0),
        source_audio_bitrate_bps=int(settings.get("source_audio_bitrate_bps") or 0),
    )
    settings.update(snapshot)
    settings.update({"preset_name": name, "default_preset": name, "selected_qualities": qualities,
                     "expected_output_bytes": estimate["estimated_output_bytes"],
                     "expected_output_by_rendition_bytes": estimate["per_rendition_bytes"],
                     "expected_output_is_estimate": True})
    updated = await run_in_threadpool(batch_runner.store.update_episode, episode_id,
                                      settings_json=settings, output_estimate_bytes=estimate["estimated_output_bytes"])
    return {"ok": True, "episode": updated}


@router.post("/episodes/{episode_id}/cancel")
async def cancel_episode(episode_id: str):
    episode = await run_in_threadpool(batch_runner.store.get_episode, episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="الحلقة غير موجودة.")
    if batch_runner._current_uploading_ep_id == episode_id:
        batch_runner._current_upload_cancel_event.set()
        return {"ok": True}
    if episode_id in batch_runner._active_processing_ids:
        batch_runner._episode_cancel_events[episode_id].set()
        return {"ok": True}
    if batch_runner._current_processing_ep_id == episode_id:
        batch_runner._current_cancel_event.set()
        await run_in_threadpool(batch_runner.pause_batch)
    else:
        await run_in_threadpool(
            batch_runner.store.update_episode,
            episode_id,
            state=EpisodeState.CANCELLED.value,
            current_stage=EpisodeState.CANCELLED.value,
        )
    return {"ok": True}


@router.post("/retry-failed")
async def retry_all_failed():
    episodes = await run_in_threadpool(batch_runner.store.list_all_episodes)
    retried = 0
    for episode in episodes:
        if episode.get("state") in {EpisodeState.FAILED.value, EpisodeState.CANCELLED.value}:
            await run_in_threadpool(batch_runner.retry_episode, episode["id"])
            retried += 1
    return {"ok": True, "retried": retried}


@router.post("/clear-completed")
async def clear_completed_queue_records():
    """Remove completed queue rows while retaining every converted output package."""
    episodes = await run_in_threadpool(batch_runner.store.list_all_episodes)
    ids = [episode["id"] for episode in episodes if episode.get("state") in {
        EpisodeState.READY.value, EpisodeState.READY_TO_UPLOAD.value, EpisodeState.SKIPPED.value
    }]
    removed = await run_in_threadpool(batch_runner.store.delete_episodes, ids, len(ids)) if ids else 0
    return {"ok": True, "removed": removed, "outputs_retained": True}


@router.post("/episodes/{episode_id}/skip")
async def skip_episode(episode_id: str):
    """Skip an episode and proceed with the remaining queue."""
    try:
        data = await run_in_threadpool(batch_runner.skip_episode, episode_id)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/episodes/{episode_id}/font-fallback")
async def approve_ass_font_fallback(episode_id: str):
    """Requires an explicit per-episode decision before proceeding with unresolved ASS fonts."""
    try:
        data = await run_in_threadpool(batch_runner.approve_ass_fallback, episode_id)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class SelectSubtitleRequest(BaseModel):
    source: str  # "external", "embedded", "none"
    path: Optional[str] = None
    stream_index: Optional[int] = None


@router.post("/episodes/{episode_id}/select-subtitle")
async def select_episode_subtitle_endpoint(episode_id: str, req: SelectSubtitleRequest):
    """Select or change the subtitle source for a queue episode."""
    try:
        updated = await run_in_threadpool(
            batch_runner.select_episode_subtitle,
            episode_id,
            req.source,
            req.path,
            req.stream_index,
        )
        return {"ok": True, "episode": updated}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class UpdateAuthoritativeDestinationRequest(BaseModel):
    folder_id: Optional[str] = None
    series_id: Optional[str] = None
    season_id: Optional[str] = None
    destination_label: Optional[str] = None


@router.post("/{batch_id}/destination-authoritative")
async def update_authoritative_destination_endpoint(
    batch_id: str,
    req: UpdateAuthoritativeDestinationRequest,
):
    """Update authoritative destination (folder, series, season IDs)."""
    res = await run_in_threadpool(
        batch_runner.update_authoritative_destination,
        batch_id,
        req.folder_id,
        req.series_id,
        req.season_id,
        req.destination_label,
    )
    if not res:
        raise HTTPException(status_code=404, detail="الدفعة غير موجودة.")
    return {"ok": True, "batch": res}


@router.post("/episodes/{episode_id}/upload")
async def upload_episode_endpoint(episode_id: str):
    """Triggers upload of a single ready episode."""
    try:
        data = await run_in_threadpool(batch_runner.upload_episode, episode_id)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload-ready")
async def upload_ready_batch_endpoint():
    """Starts background sequential upload of all episodes in READY_TO_UPLOAD."""
    try:
        data = await run_in_threadpool(batch_runner.start_batch_upload)
        return {"ok": True, **data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/demo-pipelined-state")
async def seed_demo_pipelined_state():
    """A demo route must never mutate a user's queue or running process."""
    raise HTTPException(status_code=404, detail="Endpoint disabled.")
