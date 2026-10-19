from pathlib import Path

import pytest

from studio.core.batch_store import BatchStore
from studio.core.batch_runner import BatchRunner


def _mock_scan_with_progress(scan):
    def wrapper(folder, on_episode_scanned=None, **kwargs):
        rows = scan(folder)
        for row in rows:
            if on_episode_scanned:
                on_episode_scanned(row)
        return rows
    return wrapper


def test_series_import_groups_seasons_and_skips_duplicate_sources(tmp_path, monkeypatch):
    import studio.core.batch_runner as runner_module

    series = tmp_path / "Ling Cage (2019)"
    seasons = [series / "Season 01", series / "Season 02"]
    for season in seasons:
        season.mkdir(parents=True)
        (season / "01.mkv").write_bytes(b"synthetic")

    def fake_scan(path):
        source = Path(path) / "01.mkv"
        return [{
            "order_index": 1, "episode_number": 1, "source_path": str(source),
            "filename": source.name, "display_label": "01", "state": "PENDING",
            "subtitle_path": str(source.with_suffix(".ass")) if source.with_suffix(".ass").exists() else "",
            "settings_json": {}, "source_bytes": source.stat().st_size,
        }]

    monkeypatch.setattr(runner_module, "scan_season_folder", _mock_scan_with_progress(fake_scan))
    monkeypatch.setattr(runner_module, "load_studio_config", lambda: {
        "converted_output_folder": "", "default_encoder": "gpu", "default_preset": "quality",
        "output_organization": "automatic",
    })
    store = BatchStore(tmp_path / "jobs.db")
    runner = BatchRunner(store)

    imported = runner.import_series_folder(series)
    assert imported["series_name"] == "Ling Cage (2019)"
    assert imported["detected_seasons"] == ["Season 01", "Season 02"]
    assert len(store.list_batches()) == 1
    episodes = store.get_episodes(imported["batch"]["id"])
    assert len(episodes) == 2
    assert [ep["settings"]["season_number"] for ep in episodes] == ["01", "02"]
    assert all(ep["settings"]["encoder_choice"] == "gpu" for ep in episodes)
    assert all(ep["state"] == "PENDING" for ep in episodes)

    with pytest.raises(ValueError, match="موجودة بالفعل"):
        runner.import_series_folder(series)
    assert len(store.list_all_episodes()) == 2


def test_imported_series_survives_runner_restart(tmp_path, monkeypatch):
    import studio.core.batch_runner as runner_module

    series = tmp_path / "Sample"
    season = series / "Season 01"
    season.mkdir(parents=True)
    (season / "01.mkv").write_bytes(b"dummy")
    monkeypatch.setattr(runner_module, "scan_season_folder", _mock_scan_with_progress(lambda folder: [{
        "order_index": 1, "episode_number": 1, "source_path": str(Path(folder) / "01.mkv"),
        "filename": "01.mkv", "display_label": "01", "state": "PENDING", "settings_json": {},
    }]))
    monkeypatch.setattr(runner_module, "load_studio_config", lambda: {
        "converted_output_folder": "", "default_encoder": "auto", "default_preset": "balanced",
    })
    db = tmp_path / "jobs.db"
    first = BatchRunner(BatchStore(db))
    first.import_series_folder(series)
    restarted = BatchRunner(BatchStore(db))
    assert len(restarted.get_current_batch_data()["episodes"]) == 1
    assert restarted.store.get_latest_active_batch()["status"] == "PAUSED"


def test_installed_ui_import_api_populates_all_queue_rows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import studio.core.batch_runner as runner_module
    from studio.web.app import app
    from studio.web.routes import batch_api

    root = tmp_path / "Example Series"
    for season_name in ("Season 01", "Season 02"):
        season = root / season_name
        season.mkdir(parents=True)
        (season / "01.mkv").write_bytes(b"test")

    monkeypatch.setattr(runner_module, "scan_season_folder", _mock_scan_with_progress(lambda folder: [{
        "order_index": 1, "episode_number": 1, "source_path": str(Path(folder) / "01.mkv"),
        "filename": "01.mkv", "display_label": "01", "state": "PENDING", "settings_json": {},
    }]))
    monkeypatch.setattr(runner_module, "load_studio_config", lambda: {
        "converted_output_folder": "", "default_encoder": "cpu", "default_preset": "balanced",
    })
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setattr(batch_api.batch_runner, "store", BatchStore(tmp_path / "api-jobs.db"))
    batch_api.batch_runner.reload_state()

    client = TestClient(app)
    preview = client.post("/api/batch/inspect-series", json={"folder_path": str(root)})
    assert preview.status_code == 200
    assert [item["name"] for item in preview.json()["seasons"]] == ["Season 01", "Season 02"]
    added = client.post("/api/batch/import-series", json={"folder_path": str(root), "encoder_choice": "cpu"})
    assert added.status_code == 200
    assert len(added.json()["episodes"]) == 2
    queue = client.get("/api/batch/current")
    assert queue.status_code == 200
    assert len(queue.json()["episodes"]) == 2
    assert {ep["settings"]["season_number"] for ep in queue.json()["episodes"]} == {"01", "02"}
    assert queue.json()["runner"]["is_running"] is False
