"""Comprehensive Phase 1 Tests for ABOODVIDEO Studio Persistent Season Queue.

Tests:
1. 24 files in one folder
2. Natural ordering (1.mkv, 2.mkv, 10.mkv, Episode 01.mkv, S01E03.mkv)
3. Unsupported files ignored (.txt, .nfo, .srt, .ass, .jpg, .part)
4. Duplicate prevention (re-importing same folder / files does not duplicate)
5. SQLite persistence & WAL mode verification
6. Restart/reload state preservation (brand-new BatchRunner instance retains exact order)
7. Deleting one queue item & reordering
8. Clearing a batch
9. Full FastAPI endpoint integration
"""
import os
import sqlite3
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from studio.core.batch_store import (
    BatchStore,
    EpisodeState,
    EPISODE_STATE_ARABIC,
)
from studio.core.episode_scanner import (
    scan_season_folder,
    natural_sort_key,
    parse_episode_number,
    SUPPORTED_VIDEO_EXTENSIONS,
)
from studio.core.batch_runner import BatchRunner
from studio.web.app import app


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Provides a fresh isolated SQLite database path."""
    return tmp_path / "jobs.db"


@pytest.fixture
def temp_season_folder(tmp_path: Path) -> Path:
    """Provides an isolated folder for season videos."""
    season_dir = tmp_path / "Season 1"
    season_dir.mkdir(parents=True, exist_ok=True)
    return season_dir


# ==============================================================================
# 1. 24 files in one folder test
# ==============================================================================
def test_24_files_in_one_folder(temp_season_folder: Path, temp_db_path: Path):
    """Test importing 24 video files in a single flat directory."""
    for i in range(1, 25):
        f = temp_season_folder / f"{i:02d}.mkv"
        f.write_bytes(b"dummy video content " + str(i).encode())

    store = BatchStore(db_path=temp_db_path)
    runner = BatchRunner(store=store)

    result = runner.import_season_folder(temp_season_folder, destination_label="Spirit Cage / الموسم 1")
    assert result["active"] is True
    assert result["summary"]["total_episodes"] == 24
    assert len(result["episodes"]) == 24

    for idx, ep in enumerate(result["episodes"], start=1):
        assert ep["order_index"] == idx
        assert ep["filename"] == f"{idx:02d}.mkv"
        assert ep["display_label"] == f"{idx:02d}"
        assert ep["state"] == EpisodeState.PENDING.value
        assert ep["state_arabic"] == "في الانتظار"
        assert ep["source_bytes"] > 0


# ==============================================================================
# 2. Natural ordering test
# ==============================================================================
def test_natural_ordering(temp_season_folder: Path):
    """Test natural sorting: 1.mkv < 2.mkv < 10.mkv and episode naming variants."""
    # Create filenames out of order
    names = [
        "10.mkv",
        "1.mkv",
        "2.mkv",
        "Episode 03.mkv",
        "Episode 12.mkv",
        "S01E04.mkv",
    ]
    for name in names:
        (temp_season_folder / name).write_bytes(b"content")

    episodes = scan_season_folder(temp_season_folder)
    sorted_filenames = [ep["filename"] for ep in episodes]

    # Expected order:
    # 1.mkv, 2.mkv, 10.mkv, Episode 03.mkv, Episode 12.mkv, S01E04.mkv
    assert sorted_filenames[0] == "1.mkv"
    assert sorted_filenames[1] == "2.mkv"
    assert sorted_filenames[2] == "10.mkv"
    assert sorted_filenames[3] == "Episode 03.mkv"
    assert sorted_filenames[4] == "Episode 12.mkv"
    assert sorted_filenames[5] == "S01E04.mkv"


def test_natural_sort_key_pure():
    """Unit test the natural sort key with standard edge cases."""
    items = ["10.mkv", "1.mkv", "2.mkv"]
    items.sort(key=natural_sort_key)
    assert items == ["1.mkv", "2.mkv", "10.mkv"]

    items_ep = ["Episode 10.mkv", "Episode 2.mkv", "Episode 01.mkv"]
    items_ep.sort(key=natural_sort_key)
    assert items_ep == ["Episode 01.mkv", "Episode 2.mkv", "Episode 10.mkv"]


def test_decimal_episode_sort_and_parse():
    """Decimal/special episode tokens sort numerically and remain parseable."""
    names = [
        "[SILVER] Spirit_Cage_Incarnation_S1_07[4K].mkv",
        "[SILVER] Spirit_Cage_Incarnation_S1_06.5_MiddleChapter[4K].mkv",
        "[SILVER] Spirit_Cage_Incarnation_S1_06[4K].mkv",
    ]
    names.sort(key=natural_sort_key)
    assert names == [
        "[SILVER] Spirit_Cage_Incarnation_S1_06[4K].mkv",
        "[SILVER] Spirit_Cage_Incarnation_S1_06.5_MiddleChapter[4K].mkv",
        "[SILVER] Spirit_Cage_Incarnation_S1_07[4K].mkv",
    ]
    assert parse_episode_number("6.mkv") == 6
    assert parse_episode_number("6.5.mkv") == 6.5
    assert parse_episode_number("S01E06.5.mkv") == 6.5
    assert parse_episode_number("Episode 6.5 Special.mkv") == 6.5
    assert parse_episode_number("SP1.mkv") == "SP1"
    assert parse_episode_number("OVA1.mkv") == "OVA1"


def test_scan_decimal_episode_display_and_order(temp_season_folder: Path):
    for filename in ("7.mkv", "6.5.mkv", "6.mkv"):
        (temp_season_folder / filename).write_bytes(b"video")
    episodes = scan_season_folder(temp_season_folder)
    assert [ep["filename"] for ep in episodes] == ["6.mkv", "6.5.mkv", "7.mkv"]
    assert [ep["episode_number"] for ep in episodes] == [6, 6.5, 7]
    assert [ep["display_label"] for ep in episodes] == ["06", "06.5", "07"]


# ==============================================================================
# 3. Unsupported files ignored
# ==============================================================================
def test_unsupported_files_ignored(temp_season_folder: Path):
    """Verify non-video files (.txt, .nfo, .srt, .ass, .jpg, .part) are ignored."""
    # Video files
    (temp_season_folder / "01.mkv").write_bytes(b"video")
    (temp_season_folder / "02.mp4").write_bytes(b"video")
    (temp_season_folder / "03.mov").write_bytes(b"video")
    (temp_season_folder / "04.ts").write_bytes(b"video")
    (temp_season_folder / "05.webm").write_bytes(b"video")
    (temp_season_folder / "06.avi").write_bytes(b"video")

    # Unrelated files to ignore
    (temp_season_folder / "01.ar.ass").write_bytes(b"subs")
    (temp_season_folder / "02.en.srt").write_bytes(b"subs")
    (temp_season_folder / "info.nfo").write_bytes(b"nfo")
    (temp_season_folder / "cover.jpg").write_bytes(b"image")
    (temp_season_folder / "notes.txt").write_bytes(b"text")
    (temp_season_folder / "partial.part").write_bytes(b"incomplete")
    (temp_season_folder / "Thumbs.db").write_bytes(b"windows")

    episodes = scan_season_folder(temp_season_folder)
    assert len(episodes) == 6
    for ep in episodes:
        suffix = Path(ep["filename"]).suffix.lower()
        assert suffix in SUPPORTED_VIDEO_EXTENSIONS


# ==============================================================================
# 4. Duplicate prevention
# ==============================================================================
def test_duplicate_prevention(temp_season_folder: Path, temp_db_path: Path):
    """Verify that importing the same folder again does not duplicate batches or episodes."""
    for i in range(1, 4):
        (temp_season_folder / f"{i:02d}.mkv").write_bytes(b"video")

    store = BatchStore(db_path=temp_db_path)
    runner = BatchRunner(store=store)

    # First import
    res1 = runner.import_season_folder(temp_season_folder, destination_label="Season 1")
    assert res1["summary"]["total_episodes"] == 3
    assert res1["new_episode_count"] == 3
    batch_id_1 = res1["batch"]["id"]

    # Second import of exact same folder
    res2 = runner.import_season_folder(temp_season_folder, destination_label="Season 1 Updated")
    assert res2["batch"]["id"] == batch_id_1
    assert res2["summary"]["total_episodes"] == 3
    assert res2["new_episode_count"] == 0
    assert len(store.list_batches()) == 1

    # Verify database has exactly 3 episode rows, not 6
    episodes = store.get_episodes(batch_id_1)
    assert len(episodes) == 3


# ==============================================================================
# 5. SQLite persistence & WAL mode
# ==============================================================================
def test_sqlite_persistence_and_wal_mode(temp_season_folder: Path, temp_db_path: Path):
    """Verify SQLite database schema, WAL mode, foreign keys, and fields."""
    (temp_season_folder / "01.mkv").write_bytes(b"abc")
    store = BatchStore(db_path=temp_db_path)

    # Check WAL mode
    with store._get_connection() as conn:
        cursor = conn.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal"

        cursor2 = conn.execute("PRAGMA foreign_keys;")
        fk = cursor2.fetchone()[0]
        assert fk == 1

    # Create batch and episodes
    runner = BatchRunner(store=store)
    res = runner.import_season_folder(temp_season_folder, destination_label="Test / S1")
    ep = res["episodes"][0]

    # Verify all required episode fields exist in the record
    required_episode_fields = [
        "id", "batch_id", "order_index", "source_path", "filename", "display_label",
        "state", "settings_json", "work_dir", "video_id", "session_id",
        "source_bytes", "package_bytes", "uploaded_bytes", "error", "updated_at",
    ]
    for field in required_episode_fields:
        assert field in ep, f"Field '{field}' missing from episode record!"


# ==============================================================================
# 6. Restart/reload state preservation
# ==============================================================================
def test_restart_reload_preserves_queue(temp_season_folder: Path, temp_db_path: Path):
    """Critical acceptance: Create batch, terminate runner, start brand-new runner, verify exact queue."""
    filenames = ["01.mkv", "02.mkv", "03.mkv", "10.mkv"]
    for fn in filenames:
        (temp_season_folder / fn).write_bytes(b"video data for " + fn.encode())

    # Studio Session 1
    store1 = BatchStore(db_path=temp_db_path)
    runner1 = BatchRunner(store=store1)
    res1 = runner1.import_season_folder(temp_season_folder, destination_label="Donghua / Spirit Cage / الموسم 1")
    batch_id = res1["batch"]["id"]
    order_before = [ep["filename"] for ep in res1["episodes"]]
    assert order_before == ["01.mkv", "02.mkv", "03.mkv", "10.mkv"]

    # Simulate Studio complete shutdown: close connections, destroy runner1
    del runner1
    del store1

    # Studio Session 2 (Restart)
    store2 = BatchStore(db_path=temp_db_path)
    runner2 = BatchRunner(store=store2)
    # Runner automatically reloads on initialization
    res2 = runner2.get_current_batch_data()

    assert res2["active"] is True
    assert res2["batch"]["id"] == batch_id
    assert res2["batch"]["destination_label"] == "Donghua / Spirit Cage / الموسم 1"
    assert res2["summary"]["total_episodes"] == 4

    order_after = [ep["filename"] for ep in res2["episodes"]]
    assert order_after == ["01.mkv", "02.mkv", "03.mkv", "10.mkv"]

    display_labels = [ep["display_label"] for ep in res2["episodes"]]
    assert display_labels == ["01", "02", "03", "10"]


# ==============================================================================
# 7. Deleting one queue item
# ==============================================================================
def test_delete_one_queue_item(temp_season_folder: Path, temp_db_path: Path):
    """Verify deleting one queue item removes it and preserves contiguous ordering."""
    for fn in ["01.mkv", "02.mkv", "03.mkv"]:
        (temp_season_folder / fn).write_bytes(b"content")

    store = BatchStore(db_path=temp_db_path)
    runner = BatchRunner(store=store)
    res = runner.import_season_folder(temp_season_folder)

    # Delete episode 02
    ep_to_delete = res["episodes"][1]  # '02.mkv'
    assert ep_to_delete["filename"] == "02.mkv"
    deleted = runner.delete_episode(ep_to_delete["id"])
    assert deleted is True

    # Re-fetch batch data
    after_data = runner.get_current_batch_data()
    assert after_data["summary"]["total_episodes"] == 2

    remaining_filenames = [ep["filename"] for ep in after_data["episodes"]]
    assert remaining_filenames == ["01.mkv", "03.mkv"]

    # Remaining order indices reindexed to 1, 2
    order_indices = [ep["order_index"] for ep in after_data["episodes"]]
    assert order_indices == [1, 2]


# ==============================================================================
# 8. Clearing a batch
# ==============================================================================
def test_clear_batch(temp_season_folder: Path, temp_db_path: Path):
    """Verify clearing a batch removes all episodes and resets state."""
    for fn in ["01.mkv", "02.mkv"]:
        (temp_season_folder / fn).write_bytes(b"content")

    store = BatchStore(db_path=temp_db_path)
    runner = BatchRunner(store=store)
    runner.import_season_folder(temp_season_folder)

    cleared = runner.clear_batch()
    assert cleared is True

    data = runner.get_current_batch_data()
    assert data["active"] is False
    assert len(data["episodes"]) == 0
    assert len(store.list_batches()) == 0


# ==============================================================================
# 9. FastAPI Endpoints Integration
# ==============================================================================
def test_fastapi_batch_api_endpoints(temp_season_folder: Path, temp_db_path: Path, monkeypatch):
    """Test REST API routes for batch queue management."""
    for fn in ["01.mkv", "02.mkv", "10.mkv"]:
        (temp_season_folder / fn).write_bytes(b"sample video bytes")

    # Point batch_runner's store to temp DB
    test_store = BatchStore(db_path=temp_db_path)
    from studio.core.batch_runner import batch_runner
    monkeypatch.setattr(batch_runner, "store", test_store)
    batch_runner.reload_state()

    client = TestClient(app)

    # 1. Import
    resp = client.post(
        "/api/batch/import",
        json={
            "folder_path": str(temp_season_folder),
            "destination_label": "Donghua / Spirit Cage / الموسم 1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["summary"]["total_episodes"] == 3

    # 2. GET /current
    resp2 = client.get("/api/batch/current")
    assert resp2.status_code == 200
    cur = resp2.json()
    assert cur["active"] is True
    episodes = cur["episodes"]
    assert len(episodes) == 3
    assert [ep["filename"] for ep in episodes] == ["01.mkv", "02.mkv", "10.mkv"]

    # 3. DELETE one episode
    ep_id = episodes[0]["id"]
    resp3 = client.delete(f"/api/batch/episodes/{ep_id}")
    assert resp3.status_code == 200
    assert resp3.json()["ok"] is True

    # 4. Verify remaining
    resp4 = client.get("/api/batch/current")
    assert resp4.json()["summary"]["total_episodes"] == 2

    # 5. Clear batch
    resp5 = client.post("/api/batch/clear")
    assert resp5.status_code == 200
    assert resp5.json()["ok"] is True

    resp6 = client.get("/api/batch/current")
    assert resp6.json()["active"] is False
