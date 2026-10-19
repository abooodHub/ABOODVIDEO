"""Isolated Studio startup, queue-loss and archive transport regressions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from studio.core.batch_runner import BatchRunner
from studio.core.batch_store import BatchStore, EpisodeState


def test_single_instance_mutex_is_scoped_to_profile(tmp_path):
    from studio.main import get_single_instance_mutex_name

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    assert get_single_instance_mutex_name(profile_a) == get_single_instance_mutex_name(profile_a)
    assert get_single_instance_mutex_name(profile_a) != get_single_instance_mutex_name(profile_b)


def _seed_queue(db_path: Path, root: Path, *, episode_state: str = EpisodeState.PENDING.value) -> str:
    store = BatchStore(db_path)
    batch = store.create_batch(str(root / "Season 01"), batch_id="integrity-season-01")
    store.add_episodes(batch["id"], [
        {
            "source_path": str(root / "sources" / f"{episode:02}.mkv"),
            "filename": f"{episode:02}.mkv",
            "display_label": f"{episode:02}",
            "episode_number": episode,
            "order_index": episode,
            "state": episode_state,
        }
        for episode in range(1, 30)
    ], expected_insert_count=29)
    store.update_batch(batch["id"], status="PAUSED")
    return batch["id"]


def test_29_episode_queue_survives_30_passive_runner_restarts(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    batch_id = _seed_queue(db_path, tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("startup attempted to launch an encoder process")

    monkeypatch.setattr("studio.core.batch_runner.popen_headless", forbidden)
    monkeypatch.setattr("studio.core.batch_runner.run_headless", forbidden)
    for _ in range(30):
        runner = BatchRunner(store=BatchStore(db_path))
        assert runner.store.get_batch(batch_id)["status"] == "PAUSED"
        assert len(runner.store.get_episodes(batch_id)) == 29
        assert not runner._work_authorized
        assert runner._run_authorization is None
        assert not runner._worker_may_claim()
        assert not runner.is_running
        assert not runner.is_uploading
        runner.shutdown()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 29


def test_bulk_delete_rolls_back_unexpected_29_to_1_loss(tmp_path):
    db_path = tmp_path / "jobs.db"
    batch_id = _seed_queue(db_path, tmp_path)
    store = BatchStore(db_path)
    first_twenty_eight = [row["id"] for row in store.get_episodes(batch_id)[:28]]

    with pytest.raises(RuntimeError, match="unexpectedly reduce"):
        store.delete_episodes(first_twenty_eight, expected_deleted_count=28)

    assert len(store.get_episodes(batch_id)) == 29
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    ("persisted_state", "expected_state"),
    [
        (EpisodeState.PENDING.value, EpisodeState.PENDING.value),
        (EpisodeState.NEEDS_REVIEW.value, EpisodeState.NEEDS_REVIEW.value),
        (EpisodeState.FAILED.value, EpisodeState.FAILED.value),
        (EpisodeState.READY_TO_UPLOAD.value, EpisodeState.READY_TO_UPLOAD.value),
        (EpisodeState.ENCODING.value, EpisodeState.PENDING.value),
        (EpisodeState.UPLOADING.value, EpisodeState.FAILED.value),
    ],
)
def test_interrupted_state_recovery_is_passive(tmp_path, persisted_state, expected_state):
    db_path = tmp_path / f"{persisted_state}.db"
    store = BatchStore(db_path)
    batch = store.create_batch(str(tmp_path / "Season"))
    episode = store.add_episodes(batch["id"], [{
        "source_path": str(tmp_path / "source.mkv"),
        "filename": "source.mkv",
        "order_index": 1,
        "state": persisted_state,
        "progress": 35.0,
    }])[0]
    store.update_batch(batch["id"], status="PAUSED")

    runner = BatchRunner(store=store)

    assert store.get_batch(batch["id"])["status"] == "PAUSED"
    assert store.get_episode(episode["id"])["state"] == expected_state
    assert not runner._work_authorized and not runner._worker_may_claim()
    assert not runner.is_running and not runner.is_uploading


def test_archive_paths_reject_traversal_absolute_and_links():
    from app.core.exceptions import AboodVideoException
    from app.services.package_service import PackageService

    for name in ("../escape.ts", "/absolute.ts", "C:/drive.ts", "safe/../escape.ts"):
        with pytest.raises(AboodVideoException):
            PackageService.validate_archive_member(name, is_regular_file=True, size=12)
    with pytest.raises(AboodVideoException):
        PackageService.validate_archive_member("linked.ts", is_regular_file=False, size=12)
    assert PackageService.validate_archive_member("360p/segment.ts", is_regular_file=True, size=12) == "360p/segment.ts"


def test_interrupted_upload_recovers_only_to_manual_upload_resume(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    segment = package / "seg.ts"
    segment.write_bytes(b"synthetic hls segment")
    (package / "manifest.json").write_text(
        '{"files":[{"path":"seg.ts","size_bytes":21}]}', encoding="utf-8"
    )
    store = BatchStore(tmp_path / "resume.db")
    batch = store.create_batch(str(tmp_path / "Season"))
    episode = store.add_episodes(batch["id"], [{
        "source_path": str(tmp_path / "source.mkv"),
        "filename": "source.mkv",
        "order_index": 1,
        "state": EpisodeState.UPLOADING.value,
        "work_dir": str(package),
        "upload_session_id": "preserved-session",
        "server_video_id": "preserved-video",
    }])[0]
    store.update_batch(batch["id"], status="PAUSED")

    runner = BatchRunner(store=store)

    recovered = store.get_episode(episode["id"])
    assert recovered["state"] == EpisodeState.READY_TO_UPLOAD.value
    assert recovered["upload_session_id"] == "preserved-session"
    assert recovered["server_video_id"] == "preserved-video"
    assert not runner._work_authorized and not runner._worker_may_claim()
    assert not runner.is_running and not runner.is_uploading


def test_final_package_cleanup_is_dry_run_allowlisted_and_preserves_unknown(tmp_path):
    import hashlib
    import json
    from studio.core.package_cleaner import (
        build_final_package_inventory,
        classify_final_package_files,
        cleanup_final_package_intermediates,
    )

    root = tmp_path / "package"
    rendition = root / "2160p"
    rendition.mkdir(parents=True)
    segment = b"final hls segment"
    playlist_bytes = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nmerged_000.ts\n#EXT-X-ENDLIST\n"
    (rendition / "merged_000.ts").write_bytes(segment)
    (rendition / "index.m3u8").write_bytes(playlist_bytes)
    (rendition / "seg_000.ts").write_bytes(b"pre-remux intermediate")
    (rendition / "index2.vtt").write_bytes(b"WEBVTT\n")
    (rendition / "operator-notes.log").write_text("preserve", encoding="utf-8")
    (root / "subtitles").mkdir()
    (root / "subtitles" / "embedded_raw.ass").write_text("generated raw subtitle", encoding="utf-8")
    (root / "studio_package_identity.json").write_text("{}", encoding="utf-8")
    manifest = {
        "renditions": [{"quality_label": "2160p", "playlist_path": "2160p/index.m3u8"}],
        "subtitles": [], "fonts": [],
        "files": [
            {"path": "2160p/index.m3u8", "size_bytes": len(playlist_bytes), "sha256": hashlib.sha256(playlist_bytes).hexdigest()},
            {"path": "2160p/merged_000.ts", "size_bytes": len(segment), "sha256": hashlib.sha256(segment).hexdigest()},
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    dry = cleanup_final_package_intermediates(root)
    assert set(dry["SAFE_INTERMEDIATE_DELETE"]) == {
        "2160p/seg_000.ts", "2160p/index2.vtt", "subtitles/embedded_raw.ass"
    }
    assert dry["UNKNOWN_PRESERVE"] == ["2160p/operator-notes.log"]
    assert (rendition / "seg_000.ts").exists()

    inventory = build_final_package_inventory(root, verify_hashes=True)
    assert {item["path"] for item in inventory["files"]} == {
        "2160p/index.m3u8", "2160p/merged_000.ts"
    }
    assert inventory["declared_bytes"] == len(playlist_bytes) + len(segment)

    applied = cleanup_final_package_intermediates(root, apply=True)
    assert set(applied["deleted"]) == {
        "2160p/seg_000.ts", "2160p/index2.vtt", "subtitles/embedded_raw.ass"
    }
    assert (rendition / "merged_000.ts").exists()
    assert (rendition / "index.m3u8").exists()
    assert (rendition / "operator-notes.log").read_text(encoding="utf-8") == "preserve"
    assert build_final_package_inventory(root, verify_hashes=True)["file_count"] == 2


def test_2160p_h264_source_is_remuxed_without_video_reencode(tmp_path):
    import subprocess
    from studio.core.encoder import can_copy_2160p_video, transcode_package_renditions
    from studio.core.ffmpeg_finder import get_ffmpeg_path

    ffmpeg = get_ffmpeg_path()
    assert ffmpeg
    source = tmp_path / "synthetic_4k.mp4"
    created = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=25",
         "-t", "0.48", "-c:v", "libx264", "-preset", "ultrafast",
         "-profile:v", "high", "-level:v", "5.1", "-pix_fmt", "yuv420p",
         "-an", str(source)],
        capture_output=True, text=True, timeout=90,
    )
    assert created.returncode == 0, created.stderr

    from studio.core.probe import probe_video
    probe = probe_video(source)
    assert can_copy_2160p_video(probe)
    result = transcode_package_renditions(
        source, tmp_path / "out", ["2160p"], encoder_choice="cpu",
        subtitle_mode="softsub", preset="balanced",
    )
    assert result["rendition_methods"]["2160p"]["video"] == "copy"
    playlist = tmp_path / "out" / "2160p" / "index.m3u8"
    segment_name = next(line.strip() for line in playlist.read_text().splitlines() if line.strip() and not line.startswith("#"))
    segment = playlist.parent / segment_name

    def decoded_hashes(media_path: Path) -> list[str]:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(media_path),
             "-map", "0:v:0", "-f", "framemd5", "-"],
            capture_output=True, text=True, timeout=90,
        )
        assert proc.returncode == 0, proc.stderr
        return [line.rsplit(",", 1)[-1].strip() for line in proc.stdout.splitlines() if line and not line.startswith("#")]

    assert decoded_hashes(source) == decoded_hashes(segment)


def test_preset_profiles_and_rendition_selection_are_explicit():
    from studio.core.presets import RATE_PROFILES, NVENC_PRESETS, expected_output_size, selected_qualities

    assert NVENC_PRESETS == {"economy": "p3", "balanced": "p5", "quality": "p7"}
    assert RATE_PROFILES["economy"]["1080p"] == {"video_bitrate_bps": 3_200_000, "audio_bitrate_bps": 128_000}
    assert RATE_PROFILES["balanced"]["720p"] == {"video_bitrate_bps": 2_500_000, "audio_bitrate_bps": 128_000}
    assert RATE_PROFILES["quality"]["480p"] == {"video_bitrate_bps": 1_800_000, "audio_bitrate_bps": 128_000}
    assert selected_qualities(3840, 2160, False) == ["2160p", "1080p", "720p", "480p"]
    assert selected_qualities(3840, 2160, True)[-1] == "360p"
    assert "2160p" not in selected_qualities(1920, 1080, False)
    estimate = expected_output_size(3600, "balanced", False, ["2160p", "1080p", "720p", "480p"],
                                    four_k_copy=True, source_file_bytes=1_800_000_000)
    assert estimate["is_estimate"] is True
    assert estimate["per_rendition_bytes"]["2160p"] == 1_800_000_000
    assert estimate["estimated_output_bytes"] == sum(estimate["per_rendition_bytes"].values())


def test_queued_preset_snapshot_is_immutable_profile_and_has_size_breakdown():
    from studio.core.batch_runner import _job_preset_snapshot

    episode = {
        "source_width": 3840, "source_height": 2160, "source_video_codec": "h264",
        "source_video_profile": "High", "source_video_level": 51,
        "source_pixel_format": "yuv420p", "source_fps": 25,
        "source_bytes": 2_000_000_000, "duration_seconds": 1200,
        "source_video_bitrate_bps": 7_000_000, "source_audio_bitrate_bps": 192_000,
    }
    snapshot = _job_preset_snapshot(episode, "balanced", False)
    assert snapshot["preset_name"] == "balanced"
    assert snapshot["nvenc_preset"] == "p5"
    assert snapshot["video_bitrate_bps_per_rendition"]["1080p"] == 4_500_000
    assert snapshot["selected_qualities"] == ["2160p", "1080p", "720p", "480p"]
    assert snapshot["four_k_video_mode"] == "COPY"
    assert snapshot["expected_output_is_estimate"] is True
    assert snapshot["expected_output_by_rendition_bytes"]["2160p"] == int(1200 * (7_000_000 + 192_000) / 8)
