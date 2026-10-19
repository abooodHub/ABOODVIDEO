"""Focused regression tests for the Studio 0.3 desktop rebuild."""
from pathlib import Path

import pytest

from studio.core.output_paths import batch_output_directory, single_output_directory
from studio.core.subtitle_metadata import normalize_language, normalize_subtitle


@pytest.mark.parametrize("raw", ["ar", "ara", "arabic", "@ara", "العربية"])
def test_arabic_subtitle_metadata_is_normalized(raw):
    code, name = normalize_language(raw)
    assert code == "ar"
    assert name == "العربية"


def test_subtitle_label_has_clean_structured_fields(tmp_path):
    subtitle = tmp_path / "Episode.01.ara.ass"
    subtitle.write_text("[Script Info]\n", encoding="utf-8")
    metadata = normalize_subtitle(fmt="ass", path=subtitle, default=True)
    assert metadata == {
        "language_code": "ar",
        "language_name": "العربية",
        "format": "ASS",
        "title": "",
        "forced": False,
        "default": True,
        "path": str(subtitle),
        "label": "العربية · ASS · افتراضية",
    }


def test_batch_output_is_outside_temp_and_organized(tmp_path):
    output = tmp_path / "Converted"
    output.mkdir()
    season = tmp_path / "Spirit Cage" / "Season 01"
    season.mkdir(parents=True)
    source = season / "01.mkv"
    config = {"converted_output_folder": str(output), "output_organization": "automatic"}
    result = batch_output_directory(
        config,
        source_path=source,
        season_folder=season,
        episode_number=1,
    )
    assert result == output / "Spirit Cage" / "Season 01" / "Episode 001"


def test_single_output_does_not_overwrite_existing_package(tmp_path):
    output = tmp_path / "Converted"
    output.mkdir()
    existing = output / "Pilot"
    existing.mkdir()
    (existing / "manifest.json").write_text("{}", encoding="utf-8")
    result = single_output_directory(
        {"converted_output_folder": str(output), "output_organization": "automatic"},
        source_path=tmp_path / "pilot.mkv",
        title="Pilot",
        job_id="1234567890",
    )
    assert result.name == "Pilot (12345678)"


def test_new_studio_shell_contains_required_navigation():
    template = Path("studio/web/templates/index_v3.html").read_text(encoding="utf-8")
    for page in ("import", "queue", "history", "library", "settings"):
        assert f'data-page="{page}"' in template
    assert 'data-page="dashboard"' not in template
    assert 'data-page="upload"' not in template
    assert 'id="page-upload"' in template
    assert 'id="cpu-value"' in template and 'id="server-label"' in template
    assert 'data-settings-tab="connection"' in template
    assert 'data-history-filter="completed"' in template
    assert 'data-history-filter="failed"' in template
    assert 'id="output-folder"' in template
    assert 'id="benchmark-encoder"' in template
    assert 'dir="rtl"' in template


def test_config_endpoint_never_returns_plaintext_api_key(monkeypatch):
    from fastapi.testclient import TestClient
    from studio.web.routes import api as api_routes
    from studio.web.app import app

    monkeypatch.setattr(api_routes, "load_studio_config", lambda: {
        "server_url": "https://example.test/api/v1",
        "api_key": "must-not-leak",
        "converted_output_folder": "D:/Converted",
    })
    response = TestClient(app).get("/api/config")
    assert response.status_code == 200
    payload = response.json()
    assert "api_key" not in payload
    assert payload["has_api_key"] is True


def test_queue_renders_distinct_real_rendition_and_episode_progress():
    script = Path("studio/web/static/js/studio_v3.js").read_text(encoding="utf-8")
    template = Path("studio/web/templates/index_v3.html").read_text(encoding="utf-8")
    for metric in ("rendition_progress", "overallLabel", "current_quality", "elapsed_seconds", "eta", "fps", "speed"):
        assert metric in script
    for label in ("المرحلة والجودة", "التقدم الكلي / الحالي", "السرعة · FPS", "الوقت / ETA"):
        assert label in template
    assert "function connectTelemetry()" in script
    assert "setInterval(refreshBatch,1000)" in script
    assert "if(batchRefreshPromise)return batchRefreshPromise" in script


def test_idle_telemetry_clears_stale_running_state(tmp_path):
    from studio.core.batch_store import BatchStore
    from studio.core.batch_runner import BatchRunner

    runner = BatchRunner(BatchStore(tmp_path / "telemetry.db"))
    payload = runner.get_live_telemetry_payload()
    assert payload["is_running"] is False
    assert payload["runner"]["is_running"] is False


def test_episode_store_migrates_progress_metrics_and_recovers_interrupted_state(tmp_path):
    from studio.core.batch_store import BatchStore, EpisodeState

    store = BatchStore(tmp_path / "progress.db")
    batch = store.create_batch(str(tmp_path / "Season 01"))
    store.add_episodes(batch["id"], [{
        "order_index": 1,
        "source_path": str(tmp_path / "episode.mkv"),
        "filename": "episode.mkv",
        "display_label": "01",
    }])
    episode = store.get_episodes(batch["id"])[0]
    with store._get_connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()}
    assert {"rendition_progress", "elapsed_seconds", "encoder_used"} <= columns
    store.update_episode(
        episode["id"], state=EpisodeState.ENCODING.value, current_stage=EpisodeState.ENCODING.value,
        current_quality="1080p", progress=35, rendition_progress=50, fps=54, speed="1.8x", eta="00:10:00",
        elapsed_seconds=800, encoder_used="h264_nvenc",
    )

    from studio.core.batch_runner import BatchRunner
    restarted = BatchRunner(BatchStore(tmp_path / "progress.db"))
    saved = restarted.store.get_episode(episode["id"])
    assert saved["state"] == EpisodeState.PENDING.value
    assert saved["current_quality"] == ""
    assert saved["progress"] == 35
    assert saved["rendition_progress"] == 0
    assert saved["fps"] == 0 and saved["speed"] == "0x"
    assert saved["elapsed_seconds"] == 0 and saved["encoder_used"] == ""


def test_startup_does_not_recover_manifest_without_complete_hls_package(tmp_path):
    from studio.core.batch_store import BatchStore, EpisodeState
    from studio.core.batch_runner import BatchRunner
    import json

    db = tmp_path / "recovery.db"
    store = BatchStore(db)
    batch = store.create_batch(str(tmp_path / "Season 01"))
    work = tmp_path / "output" / "Episode 001"
    work.mkdir(parents=True)
    # A bare manifest (or its generic files list) is insufficient evidence:
    # every completed HLS rendition and identity file must validate.
    (work / "manifest.json").write_text(json.dumps({"files": [{"path": "poster.webp", "size_bytes": 1}], "renditions": []}), encoding="utf-8")
    (work / "poster.webp").write_bytes(b"x")
    store.add_episodes(batch["id"], [{
        "order_index": 1, "source_path": str(tmp_path / "source.mkv"), "filename": "source.mkv",
        "display_label": "01", "work_dir": str(work), "state": EpisodeState.ENCODING.value,
        "current_stage": EpisodeState.ENCODING.value, "current_quality": "1080p", "progress": 44,
        "rendition_progress": 63, "fps": 54, "speed": "1.8x", "eta": "00:10:00",
        "elapsed_seconds": 800, "encoder_used": "h264_nvenc",
    }])

    runner = BatchRunner(BatchStore(db))
    recovered = runner.store.get_episodes(batch["id"])[0]
    assert recovered["state"] == EpisodeState.PENDING.value
    assert recovered["progress"] == 44
    assert recovered["current_quality"] == "" and recovered["rendition_progress"] == 0
    assert recovered["fps"] == 0 and recovered["speed"] == "0x"
