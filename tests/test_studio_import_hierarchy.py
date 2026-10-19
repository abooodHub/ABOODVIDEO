"""Real local imports, durable hierarchy and output safety on disposable media."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from studio.core.import_discovery import discover_import
from studio.core.episode_scanner import parse_episode_number
from studio.core.batch_runner import BatchRunner
from studio.core.batch_store import BatchStore
from studio.core.output_paths import claim_output_directory


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "fixture.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-y", str(path)], check=True, capture_output=True, timeout=30)
    return path


def put(media, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(media, path)
    return path


def configure(monkeypatch, tmp_path):
    import studio.core.batch_runner as module
    out = tmp_path / "converted"
    out.mkdir()
    cfg = {"converted_output_folder": str(out), "default_encoder": "cpu", "default_preset": "balanced",
           "include_360p": False, "processing_mode": "local", "server_url": "http://127.0.0.1:1/api/v1"}
    monkeypatch.setattr(module, "load_studio_config", lambda: cfg)
    return cfg


@pytest.mark.parametrize("name,number", [
    ("[Tianxia-Subs] Show - S01E02 [WEB 4K][EF45090E].mkv", 2),
    ("[Group 123] Show (2024) - 10 [1080p][12345678].mkv", 10),
    ("Show 2024 2160p [ABC01234].mkv", None),
    ("Show 12345678.mkv", None), ("S00E01 [OVA].mkv", 1),
    ("Show Episode 6.5.mkv", 6.5), ("[Group] Show Without Number.mkv", None),
])
def test_episode_identity_ignores_release_metadata(name, number):
    assert parse_episode_number(name) == number


def test_recursive_preview_numeric_order_specials_duplicates_unicode(media, tmp_path):
    root = tmp_path / "مسلسل عربي A Good Day (2024)"
    for name in ["Season 10/S10E02.mp4", "Season 02/Disc 1/S02E10.mp4", "Season 02/Disc 1/S02E02.mp4",
                 "Season 01/[Group] S01E01 [4K].mp4", "Season 01/[Other] S01E01.mp4",
                 "Specials/S00E01 [OVA].mp4", "Season 01/No Number [1080p].mp4"]:
        put(media, root / name)
    (root / "readme.txt").write_text("ignore", encoding="utf-8")
    result = discover_import(root, "series", {"converted_output_folder": str(tmp_path / "output")})
    assert result["episode_count"] == 7
    assert result["duplicate_count"] == 2
    assert result["ambiguous_count"] == 3
    assert [e["season_number"] for e in result["episodes"]] == [0, 1, 1, 1, 2, 2, 10]
    assert [e["episode_number"] for e in result["episodes"] if e["season_number"] == 2] == [2, 10]
    assert result["episodes"][3]["episode_number"] is None
    assert len({e["output_directory"] for e in result["episodes"]}) == 7
    assert "Season 02/Disc 1" in Path(result["episodes"][4]["output_directory"]).as_posix()


@pytest.mark.parametrize("kind", ["file", "season", "series"])
def test_real_import_preserves_metadata_and_restart(kind, media, tmp_path, monkeypatch):
    cfg = configure(monkeypatch, tmp_path)
    root = tmp_path / "عمل عربي Unicode"
    source = put(media, root / "Season 01" / "nested" / "[Release] S01E02.mp4")
    sub = source.with_suffix(".ar.srt")
    sub.write_text("1\n00:00:00,100 --> 00:00:00,800\nمرحبا\n", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    argument = source if kind == "file" else root / "Season 01" if kind == "season" else root
    preview = discover_import(argument, kind, cfg)
    runner = BatchRunner(BatchStore(tmp_path / "jobs.db"))
    method = {"file": runner.import_media_file, "season": runner.import_season_folder, "series": runner.import_series_folder}[kind]
    result = method(argument, mode="PIPELINED") if kind == "season" else method(argument)
    if kind == "season":
        assert result["batch"]["mode"] == "PIPELINED"
    ep = runner.store.list_all_episodes()[0]
    assert result["new_episode_count"] == 1
    assert ep["settings"]["relative_source_path"] == "Season 01/nested/[Release] S01E02.mp4"
    assert ep["settings"]["output_directory"] == preview["episodes"][0]["output_directory"]
    assert ep["subtitle_path"] == str(sub)
    assert ep["state"] == "PENDING"
    assert not runner.is_running
    runner.shutdown()
    resumed = BatchRunner(BatchStore(tmp_path / "jobs.db"))
    restored = resumed.store.list_all_episodes()[0]
    assert restored["settings"]["output_directory"] == ep["settings"]["output_directory"]
    assert restored["settings"]["upload_destination"]["season_number"] == 1
    assert not resumed.is_running
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    resumed.shutdown()


def test_existing_output_overlap_and_claim_are_safe(media, tmp_path, monkeypatch):
    cfg = configure(monkeypatch, tmp_path)
    root = tmp_path / "Show"
    put(media, root / "Season 01" / "S01E01.mp4")
    cfg["converted_output_folder"] = str(Path(cfg["converted_output_folder"]) / "Show")
    first = discover_import(root, "series", cfg)["episodes"][0]
    assert Path(first["output_directory"]) == Path(cfg["converted_output_folder"]) / "Season 01" / "Episode 001"
    old = Path(first["output_directory"])
    old.mkdir(parents=True)
    (old / "retained.txt").write_text("keep")
    second = discover_import(root, "series", cfg)["episodes"][0]
    assert Path(second["output_directory"]).name == "Episode 001 (2)"
    claim_output_directory(Path(second["output_directory"]), "one", Path(first["source_path"]))
    with pytest.raises(ValueError, match="محجوز"):
        claim_output_directory(Path(second["output_directory"]), "two", Path(first["source_path"]))
    assert (old / "retained.txt").read_text() == "keep"


def test_unreadable_directory_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "Show"
    root.mkdir()
    def blocked(*args, **kwargs):
        kwargs["onerror"](PermissionError(13, "denied", str(root / "restricted")))
        return iter(())
    monkeypatch.setattr("studio.core.import_discovery.os.walk", blocked)
    result = discover_import(root, "series")
    assert result["errors"][0]["path"].endswith("restricted")


def test_multiseason_queue_keeps_all_candidates_and_deduplicates_sources(media, tmp_path, monkeypatch):
    cfg = configure(monkeypatch, tmp_path)
    root = tmp_path / "Full Series عربي"
    for name in ("Season 01/[A] S01E01.mp4", "Season 01/[B] S01E01.mp4",
                 "Season 01/Unknown.mp4", "Season 02/Disc/S02E10.mp4", "Specials/S00E01.mp4"):
        put(media, root / name)
    runner = BatchRunner(BatchStore(tmp_path / "jobs.db"))
    result = runner.import_series_folder(root)
    rows = runner.store.list_all_episodes()
    assert result["new_episode_count"] == 5
    assert len(runner.store.list_batches()) == 1
    assert sum(e["state"] == "NEEDS_REVIEW" for e in rows) == 3
    assert len({e["settings"]["output_directory"] for e in rows}) == 5
    unknown = next(e for e in rows if e["filename"] == "Unknown.mp4")
    assert unknown["episode_number"] == ""
    assert unknown["settings"]["detected_episode_number"] is None
    special = next(e for e in rows if "S00" in e["filename"])
    assert special["settings"]["season_number"] == "00"
    assert not runner.is_running
    with pytest.raises(ValueError, match="موجودة بالفعل"):
        runner.import_series_folder(root)
    assert len(runner.store.list_all_episodes()) == 5
    runner.shutdown()


def test_actual_ffmpeg_runner_preserves_hierarchy_and_package_metadata(media, tmp_path, monkeypatch):
    cfg = configure(monkeypatch, tmp_path)
    source = put(media, tmp_path / "Show عربي" / "Season 02" / "Disc 1" / "S02E03.mp4")
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    runner = BatchRunner(BatchStore(tmp_path / "jobs.db"))
    result = runner.import_series_folder(source.parents[2])
    ep = runner.store.list_all_episodes()[0]
    expected = Path(ep["settings"]["output_directory"])
    assert expected.parts[-4:] == ("Show عربي", "Season 02", "Disc 1", "Episode 003")
    # Later UI mode changes must not change an already queued local job.
    cfg["processing_mode"] = "server"
    runner.start_batch(result["batch"]["id"])
    deadline = time.monotonic() + 90
    while runner.is_running and time.monotonic() < deadline:
        time.sleep(.1)
    runner.shutdown()
    final = runner.store.get_episode(ep["id"])
    assert final["state"] == "READY_TO_UPLOAD", final.get("error")
    manifest = json.loads((expected / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["renditions"]
    assert "360p" in ep["settings"]["selected_qualities"]
    assert ep["settings"]["include_360p"] is True
    assert json.loads(manifest["video"]["description"])["season_number"] == 2
    for rendition in manifest["renditions"]:
        assert (expected / rendition["playlist_path"]).is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
