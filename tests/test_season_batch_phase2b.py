"""Comprehensive Phase 2B Tests for ABOODVIDEO Studio Season Subtitle + Font Matching.

Tests:
1. Deterministic external matching:
   - 01.mkv + 01.ass -> 01.ass
   - 02.mkv + 02.srt -> 02.srt
   - 03.mkv (no sub) -> بدون ترجمة
2. Named pattern matching:
   - Episode 01.mkv + Episode 01.ass
   - S01E02.mkv + S01E02.ass
3. Ambiguous external subtitles:
   - 01.mkv with 01.ar.ass and 01.en.ass -> NEEDS_REVIEW, no guessing
   - 01.mkv with 01.ass and 01.srt -> NEEDS_REVIEW
4. Font resolution with season folder fonts/ subdirectory:
   - Missing font -> NEEDS_REVIEW, lists missing font names
   - Available font in season/fonts/ -> 1/1 ✓
5. Embedded subtitle stream detection from ffprobe:
   - Detects embedded tracks when no external sub exists
6. User subtitle selection:
   - Switch between candidates, external, embedded, and none
7. Restart persistence:
   - Subtitle selection and font results persist across SQLite jobs.db reload
8. Sequential runner execution:
   - Episode with NEEDS_REVIEW pauses sequential execution
   - Packaging includes matched softsubs and fonts in manifest.json
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
import pytest

from studio.core import batch_runner as batch_runner_module
from studio.core.batch_store import BatchStore, EpisodeState
from studio.core.batch_runner import BatchRunner
from studio.core.episode_scanner import scan_season_folder
from studio.core.fonts import analyze_ass_fonts, package_required_fonts
from tests.synthetic_fonts import write_synthetic_ttf


@pytest.fixture(autouse=True)
def isolated_converted_output(tmp_path: Path, monkeypatch):
    """Ensure all test encodes land under pytest's disposable workspace."""
    output_root = tmp_path / "converted-output"
    output_root.mkdir(parents=True, exist_ok=True)
    # Font matching must not depend on Arial being installed by Windows.
    from studio.core import fonts as fonts_module
    managed_fonts = tmp_path / "managed-test-fonts"
    write_synthetic_ttf(managed_fonts / "Arial.ttf", family="Arial")
    monkeypatch.setattr(fonts_module, "MANAGED_FONTS_DIR", managed_fonts)
    monkeypatch.setattr(batch_runner_module, "load_studio_config", lambda: {
        "default_encoder": "cpu",
        "default_preset": "balanced",
        "converted_output_folder": str(output_root),
        "include_360p": True,
    })


def make_tiny_video(target_path: Path, duration: float = 1.0, width: int = 640, height: int = 360) -> Path:
    """Creates a tiny valid video file using FFmpeg."""
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"testsrc=size={width}x{height}:rate=25",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "64k",
        str(target_path),
    ]
    subprocess.run(cmd, check=True)
    return target_path


def make_video_with_embedded_sub(video_path: Path, srt_content: str = "1\n00:00:00,000 --> 00:00:01,000\nHello World\n") -> Path:
    """Creates a video file with an embedded subtitle stream."""
    video_path = Path(video_path).resolve()
    video_path.parent.mkdir(parents=True, exist_ok=True)
    temp_srt = video_path.parent / "temp_emb.srt"
    temp_srt.write_text(srt_content, encoding="utf-8")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", "testsrc=size=640x360:rate=25",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:sample_rate=48000",
        "-i", str(temp_srt),
        "-t", "1.0",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-c:s", "mov_text" if video_path.suffix == ".mp4" else "subrip",
        "-metadata:s:s:0", "language=ara",
        "-metadata:s:s:0", "title=Arabic Softsub",
        str(video_path),
    ]
    subprocess.run(cmd, check=True)
    temp_srt.unlink(missing_ok=True)
    return video_path


def make_video_with_embedded_ass(video_path: Path, ass_path: Path, font_path: Path) -> Path:
    """Creates a disposable Matroska source with an embedded ASS track and font attachment."""
    video_path = Path(video_path).resolve()
    video_path.parent.mkdir(parents=True, exist_ok=True)
    from studio.core.fonts import extract_font_names_from_ttf

    font_names = extract_font_names_from_ttf(font_path)
    assert font_names, f"No family name found in test font {font_path.name}"
    family_name = sorted(font_names, key=lambda name: ("regular" in name.lower(), len(name), name))[0]
    ass_path.write_text(f"""[Script Info]
Title: Embedded ASS Regression
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{family_name},20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.10,0:00:01.30,Default,,0,0,0,,اختبار ترجمة ASS مضمنة
""", encoding="utf-8")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-i", str(ass_path), "-t", "1.5", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "copy",
        "-metadata:s:s:0", "language=ara", "-metadata:s:s:0", "title=Arabic ASS",
        "-attach", str(font_path), "-metadata:s:t", "mimetype=application/x-truetype-font",
        "-metadata:s:t", f"filename={font_path.name}", str(video_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return video_path


def make_ass_subtitle(path: Path, font_name: str = "Arial") -> Path:
    """Creates a valid .ass subtitle file referencing a given font family name."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"""[Script Info]
Title: Test Subtitle
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.10,0:00:00.90,Default,,0,0,0,,تجربة الترجمة
"""
    path.write_text(content, encoding="utf-8")
    return path


def make_srt_subtitle(path: Path) -> Path:
    """Creates a valid .srt subtitle file."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = """1
00:00:00,100 --> 00:00:00,900
تجربة ترجمة سريعة
"""
    path.write_text(content, encoding="utf-8")
    return path


# ==============================================================================
# TEST 1: Deterministic External Matching (01.ass, 02.srt, 03 no sub)
# ==============================================================================
def test_deterministic_external_matching(tmp_path: Path):
    season_dir = tmp_path / "Season_1"
    season_dir.mkdir(parents=True, exist_ok=True)

    make_tiny_video(season_dir / "01.mkv")
    make_ass_subtitle(season_dir / "01.ass", font_name="Arial")

    make_tiny_video(season_dir / "02.mkv")
    make_srt_subtitle(season_dir / "02.srt")

    make_tiny_video(season_dir / "03.mkv")

    episodes = scan_season_folder(season_dir)
    assert len(episodes) == 3

    # Episode 1 -> 01.ass
    ep1 = episodes[0]
    assert ep1["filename"] == "01.mkv"
    assert ep1["subtitle_source"] == "external"
    assert ep1["subtitle_format"] == "ass"
    assert ep1["subtitle_label"] == "غير معروف · ASS · افتراضية"
    assert Path(ep1["subtitle_path"]).name == "01.ass"
    assert "✓" in ep1["font_status"]  # Provided by the isolated synthetic name-table fixture.

    # Episode 2 -> 02.srt
    ep2 = episodes[1]
    assert ep2["filename"] == "02.mkv"
    assert ep2["subtitle_source"] == "external"
    assert ep2["subtitle_format"] == "srt"
    assert ep2["subtitle_label"] == "غير معروف · SRT · افتراضية"
    assert Path(ep2["subtitle_path"]).name == "02.srt"
    assert ep2["font_status"] == "—"

    # Episode 3 -> no subtitle
    ep3 = episodes[2]
    assert ep3["filename"] == "03.mkv"
    assert ep3["subtitle_source"] == "none"
    assert ep3["subtitle_label"] == "بدون ترجمة"
    assert ep3["font_status"] == "—"


# ==============================================================================
# TEST 2: Named Pattern Matching (Episode 01, S01E02)
# ==============================================================================
def test_named_patterns_matching(tmp_path: Path):
    season_dir = tmp_path / "Season_Patterns"
    season_dir.mkdir(parents=True, exist_ok=True)

    make_tiny_video(season_dir / "Episode 01.mkv")
    make_ass_subtitle(season_dir / "Episode 01.ass", font_name="Arial")

    make_tiny_video(season_dir / "S01E02.mkv")
    make_ass_subtitle(season_dir / "S01E02.ass", font_name="Arial")

    episodes = scan_season_folder(season_dir)
    assert len(episodes) == 2

    assert episodes[0]["subtitle_source"] == "external"
    assert Path(episodes[0]["subtitle_path"]).name == "Episode 01.ass"

    assert episodes[1]["subtitle_source"] == "external"
    assert Path(episodes[1]["subtitle_path"]).name == "S01E02.ass"


# ==============================================================================
# TEST 3: Ambiguous Matching -> Marked NEEDS_REVIEW (No guessing, no LLM)
# ==============================================================================
def test_ambiguous_matching_marked_needs_review(tmp_path: Path):
    season_dir = tmp_path / "Season_Ambiguous"
    season_dir.mkdir(parents=True, exist_ok=True)

    # 01.mkv has both 01.ar.ass and 01.en.ass
    make_tiny_video(season_dir / "01.mkv")
    make_ass_subtitle(season_dir / "01.ar.ass", font_name="Arial")
    make_ass_subtitle(season_dir / "01.en.ass", font_name="Arial")

    # 02.mkv has both 02.ass and 02.srt
    make_tiny_video(season_dir / "02.mkv")
    make_ass_subtitle(season_dir / "02.ass", font_name="Arial")
    make_srt_subtitle(season_dir / "02.srt")

    episodes = scan_season_folder(season_dir)
    assert len(episodes) == 2

    ep1 = episodes[0]
    assert ep1["state"] == EpisodeState.NEEDS_REVIEW.value
    assert ep1["subtitle_source"] == "needs_review"
    assert "تحتاج مراجعة" in ep1["subtitle_label"]
    assert len(ep1["subtitle_candidates"]) >= 2

    ep2 = episodes[1]
    assert ep2["state"] == EpisodeState.NEEDS_REVIEW.value
    assert ep2["subtitle_source"] == "needs_review"
    assert "تحتاج مراجعة" in ep2["subtitle_label"]


# ==============================================================================
# TEST 4: Font Resolution from Season Folder fonts/ Subdirectory
# ==============================================================================
def test_font_audit_with_season_fonts_directory(tmp_path: Path):
    season_dir = tmp_path / "Season_Fonts"
    season_fonts_dir = season_dir / "fonts"
    season_fonts_dir.mkdir(parents=True, exist_ok=True)

    season_font_path = write_synthetic_ttf(season_fonts_dir / "TestSeasonFont.ttf")

    # Read extracted font names from the copied font
    from studio.core.fonts import extract_font_names_from_ttf
    font_names = list(extract_font_names_from_ttf(season_font_path))
    target_font_name = font_names[0] if font_names else season_font_path.stem

    # Create video and ASS referencing target_font_name
    make_tiny_video(season_dir / "01.mkv")
    make_ass_subtitle(season_dir / "01.ass", font_name=target_font_name)

    # Also create 02.mkv referencing a non-existent font
    make_tiny_video(season_dir / "02.mkv")
    make_ass_subtitle(season_dir / "02.ass", font_name="CompletelyMissingFontXYZ999")

    episodes = scan_season_folder(season_dir)
    assert len(episodes) == 2

    ep1 = episodes[0]
    assert ep1["state"] == EpisodeState.PENDING.value
    assert "✓" in ep1["font_status"]
    assert len(ep1["missing_fonts"]) == 0

    ep2 = episodes[1]
    assert ep2["state"] == EpisodeState.NEEDS_REVIEW.value
    assert "مفقود" in ep2["font_status"]
    assert "CompletelyMissingFontXYZ999" in ep2["missing_fonts"]


# ==============================================================================
# TEST 5: Embedded Subtitle Stream Detection
# ==============================================================================
def test_embedded_subtitle_detection(tmp_path: Path):
    season_dir = tmp_path / "Season_Embedded"
    season_dir.mkdir(parents=True, exist_ok=True)

    # Create video with embedded subtitle track and NO external subtitle file
    vid_path = make_video_with_embedded_sub(season_dir / "01.mkv")

    episodes = scan_season_folder(season_dir)
    assert len(episodes) == 1

    ep = episodes[0]
    assert ep["subtitle_source"] == "embedded"
    assert ep["subtitle_stream_index"] is not None
    assert ep["subtitle_label"] == "العربية · SUBRIP"


@pytest.mark.parametrize('attempt',range(3))
def test_embedded_ass_branch_is_reachable_and_packages_verified_output(tmp_path: Path, monkeypatch, attempt):
    """Runs the disposable embedded-ASS source through encoding, extraction, font packaging, and verification."""
    from studio.core import batch_runner as batch_runner_module
    from studio.core import fonts as fonts_module
    from studio.core import episode_scanner as episode_scanner_module

    source_dir = tmp_path / "embedded_ass_season"
    source_dir.mkdir()
    font_path = write_synthetic_ttf(tmp_path / "SyntheticArabic.ttf", family="Synthetic Arabic Test")
    make_video_with_embedded_ass(source_dir / "01.mkv", source_dir / "embedded_track.ass", font_path)

    # The batch database, font extraction, scanner cache, and output are all
    # disposable and local to this test. The scanner imports TEMP_DIR directly.
    font_temp = tmp_path / "font_extract_tmp"
    monkeypatch.setattr(fonts_module, "TEMP_DIR", font_temp)
    monkeypatch.setattr(batch_runner_module, "TEMP_DIR", font_temp)
    monkeypatch.setattr(episode_scanner_module, "TEMP_DIR", font_temp)
    monkeypatch.setattr(batch_runner_module, "load_studio_config", lambda: {
        "default_encoder": "cpu",
        "converted_output_folder": str(tmp_path / "converted-output"),
        "include_360p": True,
    })
    store = BatchStore(db_path=tmp_path / "jobs.db")
    runner = BatchRunner(store=store)
    runner.import_season_folder(source_dir)
    episodes = store.get_episodes(runner.active_batch_id)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["subtitle_source"] == "embedded"
    assert episode["subtitle_stream_index"] is not None

    runner.start_batch()
    deadline = time.time() + 90
    while runner.is_running and time.time() < deadline:
        time.sleep(0.2)
    assert not runner.is_running, "Disposable embedded-ASS processing timed out."

    processed = store.get_episode(episode["id"])
    assert processed["state"] == EpisodeState.READY_TO_UPLOAD.value, processed.get("error")
    package_dir = Path(processed["work_dir"])
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["subtitles"]) == 1
    subtitle = manifest["subtitles"][0]
    assert subtitle["format"] in ("ass", "ssa")
    subtitle_path = package_dir / subtitle["path"]
    assert subtitle_path.is_file() and subtitle_path.suffix.lower() == ".ass"
    assert sum(1 for line in subtitle_path.read_text(encoding="utf-8-sig").splitlines() if line.startswith("Dialogue:")) > 0
    assert manifest["fonts"], "Embedded ASS required fonts must be packaged."
    assert all((package_dir / item["path"]).is_file() for item in manifest["fonts"])
    from studio.core.fonts import parse_ass_fonts
    required_font_names, _ = parse_ass_fonts(subtitle_path)
    packaged_font_families = {item["family_name"].lower() for item in manifest["fonts"]}
    assert all(name.lower() in packaged_font_families for name in required_font_names)

    assert manifest["renditions"], "The disposable source must produce all allowed quality variants."
    for rendition in manifest["renditions"]:
        playlist = package_dir / rendition["playlist_path"]
        content = playlist.read_text(encoding="utf-8")
        assert "#EXTM3U" in content and "#EXT-X-ENDLIST" in content
        segment_names = [line.strip() for line in content.splitlines() if line.strip().endswith(".ts")]
        assert segment_names and len(segment_names) == rendition["segment_count"]
        assert all((package_dir / rendition["quality_label"] / name).is_file() for name in segment_names)
        assert rendition["width"] == 640 and rendition["height"] == 360

    declared_hashes = {item["path"]: item["sha256"] for item in manifest["files"]}
    import hashlib
    for rel_path, expected_hash in declared_hashes.items():
        digest = hashlib.sha256((package_dir / rel_path).read_bytes()).hexdigest()
        assert digest == expected_hash
    assert runner.inspect_local_package(episode["id"])["compatible"] is True

    # Re-upload must reuse the retained package and dispatch only the upload worker.
    import threading
    upload_dispatched = threading.Event()
    encoder_calls = []
    monkeypatch.setattr(batch_runner_module, "load_studio_config", lambda: {
        "default_encoder": "cpu",
        "server_url": "http://127.0.0.1:8000/api/v1",
        "api_key": "test-only",
    })
    monkeypatch.setattr(batch_runner_module, "test_connection", lambda *args, **kwargs: {
        "ok": True,
        "scopes": ["packages:create", "packages:write", "packages:publish"],
    })
    monkeypatch.setattr(batch_runner_module, "transcode_package_renditions", lambda *args, **kwargs: encoder_calls.append(True))
    monkeypatch.setattr(runner, "upload_episode", lambda episode_id: upload_dispatched.set())
    reupload = runner.reupload_local_package(episode["id"])
    assert reupload["ok"] is True
    assert upload_dispatched.wait(5), "The retained package upload worker was not dispatched."
    assert not encoder_calls, "Re-upload must never start FFmpeg encoding."


# ==============================================================================
# TEST 6: User Subtitle Selection & Switching API
# ==============================================================================
def test_user_subtitle_selection_api(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    store = BatchStore(db_path=db_path)
    runner = BatchRunner(store=store)

    season_dir = tmp_path / "Season_Switch"
    season_dir.mkdir(parents=True, exist_ok=True)

    make_tiny_video(season_dir / "01.mkv")
    sub_ar = make_ass_subtitle(season_dir / "01.ar.ass", font_name="Arial")
    sub_en = make_ass_subtitle(season_dir / "01.en.ass", font_name="Arial")

    runner.import_season_folder(season_dir)
    episodes = store.get_episodes(runner.active_batch_id)
    assert len(episodes) == 1
    assert episodes[0]["state"] == EpisodeState.NEEDS_REVIEW.value

    # User resolves by selecting 01.ar.ass
    updated = runner.select_episode_subtitle(
        episode_id=episodes[0]["id"],
        source="external",
        path=str(sub_ar),
    )
    assert updated["subtitle_source"] == "external"
    assert updated["subtitle_path"] == str(sub_ar.resolve())
    assert updated["state"] == EpisodeState.PENDING.value
    assert "✓" in updated["font_status"]

    # User switches to none
    updated_none = runner.select_episode_subtitle(
        episode_id=episodes[0]["id"],
        source="none",
    )
    assert updated_none["subtitle_source"] == "none"
    assert updated_none["subtitle_label"] == "بدون ترجمة"


# ==============================================================================
# TEST 7: Restart Persistence across SQLite jobs.db
# ==============================================================================
def test_restart_persistence(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    store = BatchStore(db_path=db_path)
    runner = BatchRunner(store=store)

    season_dir = tmp_path / "Season_Persist"
    season_dir.mkdir(parents=True, exist_ok=True)

    make_tiny_video(season_dir / "01.mkv")
    make_ass_subtitle(season_dir / "01.ass", font_name="Arial")

    make_tiny_video(season_dir / "02.mkv")
    make_srt_subtitle(season_dir / "02.srt")

    runner.import_season_folder(season_dir)
    batch_id = runner.active_batch_id

    # Simulate full Studio restart
    new_store = BatchStore(db_path=db_path)
    new_runner = BatchRunner(store=new_store)
    new_runner.reload_state()

    assert new_runner.active_batch_id == batch_id
    batch_data = new_runner.get_current_batch_data()
    assert batch_data["active"] is True
    reloaded_eps = batch_data["episodes"]
    assert len(reloaded_eps) == 2

    # Verify subtitle & font properties persisted intact
    assert reloaded_eps[0]["subtitle_source"] == "external"
    assert reloaded_eps[0]["subtitle_format"] == "ass"
    assert reloaded_eps[0]["subtitle_label"] == "غير معروف · ASS · افتراضية"
    assert "✓" in reloaded_eps[0]["font_status"]

    assert reloaded_eps[1]["subtitle_source"] == "external"
    assert reloaded_eps[1]["subtitle_format"] == "srt"
    assert reloaded_eps[1]["subtitle_label"] == "غير معروف · SRT · افتراضية"
    assert reloaded_eps[1]["font_status"] == "—"


# ==============================================================================
# TEST 8: Sequential Packaging with Softsubs and Fonts
# ==============================================================================
def test_sequential_packaging_with_subtitles_and_fonts(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    store = BatchStore(db_path=db_path)
    runner = BatchRunner(store=store)

    season_dir = tmp_path / "Season_Package"
    season_dir.mkdir(parents=True, exist_ok=True)

    # Episode 1 with ASS subtitle (Arial)
    make_tiny_video(season_dir / "01.mkv", duration=1.0)
    make_ass_subtitle(season_dir / "01.ass", font_name="Arial")

    # Episode 2 with SRT subtitle
    make_tiny_video(season_dir / "02.mkv", duration=1.0)
    make_srt_subtitle(season_dir / "02.srt")

    runner.import_season_folder(season_dir)
    runner.start_batch()

    # Wait for completion of both episodes
    t0 = time.time()
    while runner.is_running and time.time() - t0 < 30:
        time.sleep(0.3)

    episodes = store.get_episodes(runner.active_batch_id)
    assert all(ep["state"] == EpisodeState.READY_TO_UPLOAD.value for ep in episodes)

    # Inspect Episode 1 package manifest
    ep1_work_dir = Path(episodes[0]["work_dir"])
    manifest1_path = ep1_work_dir / "manifest.json"
    assert manifest1_path.exists()
    manifest1 = json.loads(manifest1_path.read_text(encoding="utf-8"))

    assert len(manifest1["subtitles"]) == 1
    assert manifest1["subtitles"][0]["format"] == "ass"
    assert (ep1_work_dir / manifest1["subtitles"][0]["path"]).exists()

    # Inspect Episode 2 package manifest
    ep2_work_dir = Path(episodes[1]["work_dir"])
    manifest2_path = ep2_work_dir / "manifest.json"
    assert manifest2_path.exists()
    manifest2 = json.loads(manifest2_path.read_text(encoding="utf-8"))

    assert len(manifest2["subtitles"]) == 1
    assert manifest2["subtitles"][0]["format"] == "vtt"
    assert (ep2_work_dir / manifest2["subtitles"][0]["path"]).exists()


# ==============================================================================
# TEST 9: Sequential Runner Halts on NEEDS_REVIEW
# ==============================================================================
def test_sequential_halts_on_needs_review(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    store = BatchStore(db_path=db_path)
    runner = BatchRunner(store=store)

    season_dir = tmp_path / "Season_Halt"
    season_dir.mkdir(parents=True, exist_ok=True)

    # Episode 1 with ambiguous subtitles
    make_tiny_video(season_dir / "01.mkv", duration=1.0)
    make_ass_subtitle(season_dir / "01.ar.ass", font_name="Arial")
    make_ass_subtitle(season_dir / "01.en.ass", font_name="Arial")

    # Episode 2 clean
    make_tiny_video(season_dir / "02.mkv", duration=1.0)
    make_srt_subtitle(season_dir / "02.srt")

    runner.import_season_folder(season_dir)
    episodes = store.get_episodes(runner.active_batch_id)
    assert episodes[0]["state"] == EpisodeState.NEEDS_REVIEW.value

    # Start batch -> Should halt without encoding episode 1
    runner.start_batch()
    time.sleep(1.0)

    assert not runner.is_running
    batch = store.get_batch(runner.active_batch_id)
    assert batch["status"] == "PAUSED"
    episodes = store.get_episodes(runner.active_batch_id)
    assert episodes[0]["state"] == EpisodeState.NEEDS_REVIEW.value
    # Episode 2 should not have started
    assert episodes[1]["state"] == EpisodeState.PENDING.value
