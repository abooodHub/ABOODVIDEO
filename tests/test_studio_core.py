"""Unit and Integration Tests for ABOODVIDEO Studio Core Engine."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from studio import __version__
from studio.config import TEMP_DIR
from tests.synthetic_fonts import write_synthetic_ttf
from studio.core.hardware import check_nvenc_support, get_full_telemetry
from studio.core.probe import probe_video
from studio.core.fonts import parse_ass_fonts, analyze_ass_fonts, attach_font_file
from studio.core.subtitles import convert_to_webvtt, build_hardsub_filter
from studio.core.encoder import transcode_package_renditions, generate_poster_webp
from studio.core.packager import build_and_verify_package


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """Generates a tiny valid 2-second 720p MP4 test video with audio using FFmpeg."""
    tmp_dir = tmp_path_factory.mktemp("studio_test_media")
    video_path = tmp_dir / "sample_720p.mp4"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", "testsrc=size=1280x720:rate=25",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", "2.0",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        str(video_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert res.returncode == 0, f"FFmpeg video creation failed: {res.stderr}"
    assert video_path.exists() and video_path.stat().st_size > 0
    return video_path


def test_single_studio_job_preserves_enabled_360p(sample_video, tmp_path):
    from studio.config import load_studio_config, save_studio_config
    from studio.core.job_runner import StudioJob
    previous = load_studio_config()
    output = tmp_path / 'single-job-output'
    output.mkdir()
    try:
        save_studio_config({'converted_output_folder':str(output), 'include_360p':True, 'default_preset':'fast'})
        job = StudioJob(str(sample_video), '360p propagation regression', ['360p'],
                        encoder_choice='cpu', subtitle_mode='none', auto_upload=False)
        job.start()
        job.thread.join(60)
        assert not job.thread.is_alive()
        assert job.state['status'] == 'completed', job.state.get('error')
        assert [r['quality_label'] for r in job.state['result']['manifest']['renditions']] == ['360p']
    finally:
        save_studio_config(previous)


@pytest.fixture
def sample_ass(tmp_path) -> Path:
    """Generates a sample .ass subtitle file referencing Arial and a missing font."""
    ass_path = tmp_path / "sample.ass"
    ass_text = """[Script Info]
Title: Studio Test Subtitle
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: FancyTitle,AboodNonExistentFont999,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:01.80,Default,,0,0,0,,{\\fnSegoe UI\\b1}مرحبا بكم في استوديو عبود{\\fn}
Dialogue: 0,0:00:01.80,0:00:02.00,FancyTitle,,0,0,0,,نهاية العرض التجريبي
"""
    ass_path.write_text(ass_text, encoding="utf-8")
    return ass_path


# --- 1. Video Probe Tests ---
def test_probe_video_specs(sample_video):
    """Proves ffprobe extracts resolution, codec, fps, duration, and audio."""
    info = probe_video(sample_video)
    assert info["width"] == 1280
    assert info["height"] == 720
    assert info["resolution"] == "1280x720"
    assert info["video_codec"] == "h264"
    assert info["fps"] == 25.0
    assert 1.9 <= info["duration_seconds"] <= 2.2
    assert info["has_audio"] is True
    assert info["audio_codec"] == "aac"
    assert info["file_size"] > 0


def test_probe_video_upscaling_prevention(sample_video):
    """Proves that 720p source strictly disables 1080p and 2160p (Downscale only), and 1440p is removed."""
    info = probe_video(sample_video)
    qualities = {q["key"]: q for q in info["qualities"]}

    # Allowed downscale
    assert qualities["360p"]["allowed"] is True
    assert qualities["480p"]["allowed"] is True
    assert qualities["720p"]["allowed"] is True

    # Blocked upscale
    assert qualities["1080p"]["allowed"] is False
    assert qualities["1080p"]["is_upscale"] is True
    assert "1440p" not in qualities
    assert qualities["2160p"]["allowed"] is False
    assert qualities["2160p"]["is_upscale"] is True


def test_quality_ladder_rules_and_cropped_4k():
    """
    Proves all Quality Ladder Hotfix rules:
    Rule 1: Real 3840x2160 source -> 4K + 1080p + 720p + 480p + 360p
    Rule 2: 1080p source -> 1080p + 720p + 480p + 360p
    Rule 3: 720p source -> 720p + 480p + 360p
    Rule 4: Never upscale
    Rule 5: Cropped 3840 source (e.g. 3840x1634 scope) -> 4K + 1080p + 720p + 480p + 360p
    Absence: 1440p is never present
    """
    from studio.core.probe import LADDER_SPECS, QUALITY_ORDER

    # 1. 1440p is completely absent from LADDER_SPECS and QUALITY_ORDER
    assert "1440p" not in LADDER_SPECS
    assert "1440p" not in QUALITY_ORDER
    assert QUALITY_ORDER == ["360p", "480p", "720p", "1080p", "2160p"]

    # Helper simulating probe eligibility logic
    def get_allowed(w, h):
        return [
            k for k in QUALITY_ORDER
            if (LADDER_SPECS[k]["height"] <= h or LADDER_SPECS[k]["width"] <= w)
        ]

    # Rule 1: Real 3840x2160 source
    assert get_allowed(3840, 2160) == ["360p", "480p", "720p", "1080p", "2160p"]

    # Rule 2: 1080p source (1920x1080)
    assert get_allowed(1920, 1080) == ["360p", "480p", "720p", "1080p"]

    # Rule 3: 720p source (1280x720)
    assert get_allowed(1280, 720) == ["360p", "480p", "720p"]

    # Rule 4: Never upscale (e.g. 640x360 source only allows 360p)
    assert get_allowed(640, 360) == ["360p"]

    # Rule 5: Cinematic / cropped 3840 source (3840x1634 Spirit Cage S2E01)
    # Highest rendition is classified as 4K (2160p) because width is 3840 (no upscale)
    assert get_allowed(3840, 1634) == ["360p", "480p", "720p", "1080p", "2160p"]


# --- 2. Hardware Telemetry Tests ---
def test_hardware_telemetry():
    """Proves GPU/CPU hardware telemetry reports valid data."""
    data = get_full_telemetry()
    hw = data["hardware"]
    assert "gpu" in hw
    assert "cpu_ram" in hw
    assert hw["recommended_encoder"] in ("nvenc", "cpu")
    assert hw["cpu_ram"]["cpu_cores_logical"] >= 1
    assert hw["cpu_ram"]["ram_total_mb"] > 1000


# --- 3. ASS Font Scanner & Missing Detection Tests ---
def test_ass_font_scanner_and_missing_detection(sample_ass, tmp_path, monkeypatch):
    """Proves ASS parsing and missing detection with explicit font fixtures."""
    from studio.core import fonts as fonts_module
    managed = tmp_path / "test-fonts"
    write_synthetic_ttf(managed / "Arial.ttf", family="Arial")
    write_synthetic_ttf(managed / "SegoeUI.ttf", family="Segoe UI")
    monkeypatch.setattr(fonts_module, "MANAGED_FONTS_DIR", managed)
    fonts, meta = parse_ass_fonts(sample_ass)
    assert "Arial" in fonts
    assert "AboodNonExistentFont999" in fonts
    assert "Segoe UI" in fonts

    assert meta["Arial"]["in_styles"] is True
    assert meta["Segoe UI"]["in_overrides"] is True

    audit = analyze_ass_fonts(sample_ass)
    assert audit["total_fonts"] == 3
    assert audit["missing_count"] == 1
    assert audit["all_fonts_present"] is False

    missing_items = [f for f in audit["fonts"] if f["status"] == "missing"]
    assert len(missing_items) == 1
    assert missing_items[0]["name"] == "AboodNonExistentFont999"


def test_fonts_api_lists_bundled_and_managed_fonts_without_500(tmp_path, monkeypatch):
    """GET /api/fonts returns both font pools and identifies identical files safely."""
    from studio.core import fonts as fonts_module
    from studio.web.app import app

    bundled_dir = tmp_path / "bundled"
    managed_dir = tmp_path / "managed"
    bundled_dir.mkdir()
    managed_dir.mkdir()
    source_font = write_synthetic_ttf(tmp_path / "Arial.ttf", family="Arial")
    (bundled_dir / "Bundled.ttf").write_bytes(source_font.read_bytes())
    (managed_dir / "Managed.ttf").write_bytes(source_font.read_bytes())
    monkeypatch.setattr(fonts_module, "FONTS_DIR", bundled_dir)
    monkeypatch.setattr(fonts_module, "MANAGED_FONTS_DIR", managed_dir)

    with TestClient(app) as client:
        response = client.get("/api/fonts")

    assert response.status_code == 200
    records = response.json()
    assert {item["source"] for item in records} == {"studio_fonts", "studio_managed"}
    duplicate = next(item for item in records if item["filename"] == "Managed.ttf")
    assert duplicate["duplicate_of"].endswith("Bundled.ttf")


def test_external_font_library_indexes_once_and_resolves_ass_fonts(tmp_path, monkeypatch):
    """External fonts are indexed/cached and can resolve an ASS family without installation."""
    from studio.core import fonts as fonts_module

    library = tmp_path / "font-library"
    library.mkdir()
    write_synthetic_ttf(library / "Arial.ttf", family="Arial")
    cache = tmp_path / "font-index.json"
    original_parser = fonts_module.extract_font_names_from_ttf
    parsed = []

    def count_parse(path):
        parsed.append(Path(path).name)
        return original_parser(path)

    monkeypatch.setattr(fonts_module, "extract_font_names_from_ttf", count_parse)
    first = fonts_module.get_font_library_index(root=library, cache_file=cache)
    second = fonts_module.get_font_library_index(root=library, cache_file=cache)
    assert first["files"][0]["family"] == "Arial"
    assert first["files"][0]["full_path"] == str((library / "Arial.ttf").resolve())
    assert first["schema_version"] == fonts_module.FONT_LIBRARY_INDEX_SCHEMA
    assert cache.is_file()
    assert second == first
    assert parsed == ["Arial.ttf"]
    stale_cache = json.loads(cache.read_text(encoding="utf-8"))
    stale_cache.pop("schema_version")
    cache.write_text(json.dumps(stale_cache), encoding="utf-8")
    fonts_module.get_font_library_index(root=library, cache_file=cache)
    assert parsed == ["Arial.ttf", "Arial.ttf"]

    monkeypatch.setattr(fonts_module, "_configured_font_library_root", lambda: library)
    monkeypatch.setattr(fonts_module, "FONT_LIBRARY_INDEX_FILE", cache)
    monkeypatch.setattr(fonts_module, "get_windows_installed_fonts", lambda: {})
    monkeypatch.setattr(fonts_module, "get_studio_attached_fonts", lambda: {})
    monkeypatch.setattr(fonts_module, "get_season_folder_fonts", lambda _path: {})
    monkeypatch.setattr(fonts_module, "extract_mkv_font_attachments", lambda *_args: [])
    ass = tmp_path / "library.ass"
    ass.write_text(
        "[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n",
        encoding="utf-8",
    )
    audit = fonts_module.analyze_ass_fonts(ass)
    assert audit["missing_count"] == 0
    assert audit["fonts"][0]["status"] == "font_library"
    assert Path(audit["fonts"][0]["file"]) == (library / "Arial.ttf").resolve()


# --- 4. Subtitle Conversion & Filter Tests ---
def test_subtitle_webvtt_conversion(sample_ass, tmp_path):
    """Proves ASS to WebVTT conversion produces standard WebVTT subtitles."""
    vtt_out = tmp_path / "converted.vtt"
    convert_to_webvtt(sample_ass, vtt_out)
    assert vtt_out.exists()
    content = vtt_out.read_text(encoding="utf-8")
    assert "WEBVTT" in content
    assert "مرحبا بكم في استوديو عبود" in content


def test_build_hardsub_filter(sample_ass):
    """Proves hardsub filter escapes paths safely for FFmpeg filtergraph."""
    flt = build_hardsub_filter(sample_ass)
    assert "subtitles=" in flt
    assert "/sample.ass" in flt
    assert "\\\\" not in flt
    assert "\\sample" not in flt  # No directory separator backslashes


# --- 5. Transcoder Engine & Multi-Rendition HLS Tests ---
def test_transcode_renditions_and_poster(sample_video, tmp_path):
    """Proves transcoding generates compliant HLS segments, playlists, and WebP poster."""
    out_dir = tmp_path / "hls_output"

    progress_events = []
    def on_prog(p):
        progress_events.append(p)

    res = transcode_package_renditions(
        source_path=sample_video,
        output_dir=out_dir,
        selected_qualities=["360p", "480p"],
        encoder_choice="auto",
        subtitle_mode="none",
        hls_segment_duration=2,
        progress_callback=on_prog,
        include_360p=True,
    )

    assert Path(res["output_dir"]).exists()
    assert len(res["renditions"]) == 2

    # Verify 360p
    p360_m3u8 = out_dir / "360p" / "index.m3u8"
    assert p360_m3u8.exists()
    p360_content = p360_m3u8.read_text(encoding="utf-8")
    assert "#EXTM3U" in p360_content
    assert "#EXT-X-ENDLIST" in p360_content
    p360_segs = list((out_dir / "360p").glob("seg_*.ts"))
    assert len(p360_segs) >= 1

    # Verify 480p
    p480_m3u8 = out_dir / "480p" / "index.m3u8"
    assert p480_m3u8.exists()

    # Verify Poster
    poster = out_dir / "poster.webp"
    assert poster.exists() and poster.stat().st_size > 0

    # Verify telemetry callbacks fired
    assert len(progress_events) > 0


# --- 6. Packager Assembly & Pre-Flight Verification Tests ---
def test_package_assembly_and_manifest(sample_video, tmp_path):
    """Proves packager builds compliant manifest.json with valid SHA-256 for all files."""
    out_dir = tmp_path / "pkg_test"
    trans_res = transcode_package_renditions(
        source_path=sample_video,
        output_dir=out_dir,
        selected_qualities=["360p"],
        encoder_choice="auto",
        hls_segment_duration=2,
        include_360p=True,
    )

    pkg_result = build_and_verify_package(
        package_dir=out_dir,
        title="Studio Test Video",
        duration_seconds=2.0,
        renditions=trans_res["renditions"],
        description="Packager test description",
    )

    manifest = pkg_result["manifest"]
    assert manifest["manifest_version"] == "1.0"
    assert manifest["generator"] == f"ABOODVIDEO Studio v{__version__}"
    assert manifest["video"]["title"] == "Studio Test Video"
    assert len(manifest["renditions"]) == 1
    assert manifest["renditions"][0]["quality_label"] == "360p"

    # All files have SHA-256 and media_type
    assert len(manifest["files"]) >= 2  # index.m3u8, seg_000.ts, poster.webp
    for f in manifest["files"]:
        assert len(f["sha256"]) == 64
        assert f["size_bytes"] > 0
        assert f["media_type"] is not None
        assert not f["path"].startswith(("/", "\\"))
        assert ".." not in f["path"]


# --- 7. True ASS Softsub & Font Packaging Tests ---
def test_true_ass_softsub_preservation_and_font_packaging(sample_ass, sample_video, tmp_path):
    """Proves True ASS softsubs preserve original .ass files, package required fonts, and deduplicate."""
    from studio.core.subtitles import process_softsub_file
    from studio.core.fonts import package_required_fonts, parse_ass_fonts

    pkg_dir = tmp_path / "true_ass_pkg"
    subs_dir = pkg_dir / "subtitles"
    fonts_dir = pkg_dir / "fonts"

    # 1. Process softsub
    res = process_softsub_file(sample_ass, subs_dir, language_code="ar")
    assert res["format"] == "ass"
    assert res["primary_rel_path"] == "subtitles/ar.ass"
    assert (pkg_dir / "subtitles" / "ar.ass").exists()

    # Original content preserved intact (not WebVTT)
    saved_text = (pkg_dir / "subtitles" / "ar.ass").read_text(encoding="utf-8")
    assert "[V4+ Styles]" in saved_text
    assert "WEBVTT" not in saved_text

    # 2. Package required fonts (mock/system)
    req_fonts, _ = parse_ass_fonts(sample_ass)
    packaged_fonts = package_required_fonts(req_fonts, fonts_dir)
    assert isinstance(packaged_fonts, list)

    # 3. Assemble and verify package
    dummy_renditions = [{
        "quality_label": "360p",
        "width": 640,
        "height": 360,
        "video_bitrate_bps": 700000,
        "playlist_path": "360p/index.m3u8",
        "segment_count": 1,
    }]
    (pkg_dir / "360p").mkdir(parents=True, exist_ok=True)
    (pkg_dir / "360p" / "index.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg_000.ts\n#EXT-X-ENDLIST\n", encoding="utf-8")
    (pkg_dir / "360p" / "seg_000.ts").write_bytes(b"TS_DATA" * 50)

    pkg_out = build_and_verify_package(
        package_dir=pkg_dir,
        title="True ASS Softsub Video",
        duration_seconds=2.0,
        renditions=dummy_renditions,
        subtitles=[{
            "language_code": "ar",
            "language_name": "العربية",
            "label": "العربية (ASS)",
            "is_default": True,
            "path": "subtitles/ar.ass",
            "format": "ass",
            "fonts": [f["path"] for f in packaged_fonts],
        }],
        fonts=packaged_fonts,
    )

    manifest = pkg_out["manifest"]
    assert len(manifest["subtitles"]) == 1
    sub_decl = manifest["subtitles"][0]
    assert sub_decl["format"] == "ass"
    assert sub_decl["path"] == "subtitles/ar.ass"

    # All files have unique paths
    paths = [f["path"] for f in manifest["files"]]
    assert len(paths) == len(set(paths))
    assert "subtitles/ar.ass" in paths


def test_zip_font_import_safe(tmp_path):
    """Proves ZIP font import safely extracts TTF/OTF fonts and rejects traversal."""
    import zipfile
    from studio.core.fonts import import_fonts_from_zip

    zip_file = tmp_path / "test_fonts.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        # Valid font
        zf.writestr("MyFont.ttf", b"\x00\x01\x00\x00" + b"\x00" * 100)
        # Dangerous traversal font (must be skipped)
        zf.writestr("../evil.ttf", b"\x00\x01\x00\x00" + b"\x00" * 100)
        # Non-font file (must be skipped)
        zf.writestr("readme.txt", b"Ignore this file")

    dest_dir = tmp_path / "imported"
    imported = import_fonts_from_zip(zip_file, dest_dir)
    assert len(imported) == 1
    assert imported[0]["filename"] == "MyFont.ttf"
    assert (dest_dir / "MyFont.ttf").exists()
    assert not (tmp_path / "evil.ttf").exists()
