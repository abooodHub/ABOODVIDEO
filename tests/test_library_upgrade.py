"""DB-independent regression checks for the library/4K/player upgrade."""
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi import HTTPException, Response

from app.api.watch_routes import render_watch_template
from app.api.v1.admin_quality_settings import QualitySettingsPayload, read_quality_settings, update_quality_settings
from app.core.config import settings
from app.schemas.upload import InitiateUploadRequest
from app.services.quality_settings import QUALITY_ORDER, normalize_qualities
from app.services.transcoder import execute_transcode_rendition, generate_master_playlist, probe_source, select_quality_ladder


def test_4k_source_generates_real_4k_ladder_and_master(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", True)
    ladder = select_quality_ladder(3840, 2160, 25_000_000, normalize_qualities(",".join(QUALITY_ORDER)))
    assert [item["quality_label"] for item in ladder] == list(QUALITY_ORDER)
    assert ladder[-1]["width"] == 3840
    assert ladder[-1]["height"] == 2160
    configs = [{**item, "rfc_codecs": "avc1.640033,mp4a.40.2"} for item in ladder]
    master = tmp_path / "master.m3u8"
    generate_master_playlist(configs, master)
    content = master.read_text(encoding="utf-8")
    assert "RESOLUTION=3840x2160" in content
    assert "2160p/index.m3u8" in content


def test_quality_policy_and_source_resolution_never_upscale(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", True)
    selected = normalize_qualities("720p,1080p,2160p")
    ladder = select_quality_ladder(1920, 1080, None, selected)
    assert [item["quality_label"] for item in ladder] == ["720p", "1080p"]
    assert all(item["width"] <= 1920 and item["height"] <= 1080 for item in ladder)
    cropped = select_quality_ladder(1280, 719, None)
    assert all(item["height"] <= 719 for item in cropped)
    tiny = select_quality_ladder(426, 240, None)
    assert tiny[0]["quality_label"] == "source"
    assert tiny[0]["width"] <= 426 and tiny[0]["height"] <= 240


def test_deployment_can_disable_4k_without_rewriting_saved_selection(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", False)
    assert "2160p" not in normalize_qualities(None)
    assert "2160p" not in [r["quality_label"] for r in select_quality_ladder(3840, 2160, None)]


def test_upload_accepts_metadata_only_organization():
    payload = InitiateUploadRequest(
        title="Episode", filename="episode.mp4", file_size_bytes=1024,
        category="Animation", series_name="Series", season_number=2,
        episode_number=3, visibility="UNLISTED",
    )
    assert payload.category == "Animation"
    assert payload.season_number == 2
    assert payload.visibility.value == "UNLISTED"


def test_watch_template_escapes_title_and_subtitle_json():
    response = render_watch_template(
        title="<script>alert(1)</script>", share_id="sample",
        playback_token="sample", subtitles=[{
            "id": "sample", "label": "<img src=x>", "is_playable": True,
        }], library_meta="<b>Category</b>",
    )
    body = response.body.decode("utf-8")
    assert "&lt;script&gt;" in body
    assert "&lt;b&gt;Category&lt;/b&gt;" in body
    assert "<img src=x>" not in body
    assert "\\u003cimg src=x>" in body


@pytest.mark.asyncio
async def test_admin_quality_setting_persists_for_future_jobs(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", True)

    class FakeSession:
        row = None
        committed = False

        async def get(self, _model, _key):
            return self.row

        def add(self, row):
            self.row = row

        async def commit(self):
            self.committed = True

    session = FakeSession()
    initial = await read_quality_settings(Response(), session)
    assert "2160p" in initial["enabled_qualities"]
    saved = await update_quality_settings(
        QualitySettingsPayload(enabled_qualities=["360p", "720p", "2160p"]), Response(), session,
    )
    assert session.committed
    assert saved["enabled_qualities"] == ["360p", "720p", "2160p"]
    assert (await read_quality_settings(Response(), session))["enabled_qualities"] == saved["enabled_qualities"]
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", False)
    with pytest.raises(HTTPException) as error:
        await update_quality_settings(
            QualitySettingsPayload(enabled_qualities=["2160p"]), Response(), session,
        )
    assert error.value.status_code == 422


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg binaries are required")
def test_real_2160p_ffmpeg_rendition_is_encoded(tmp_path: Path, monkeypatch):
    """Encode actual 3840x2160 frames, not only a named preset or mock."""
    monkeypatch.setattr(settings, "ENABLE_4K_TRANSCODING", True)
    monkeypatch.setattr(settings, "FFMPEG_PRESET", "ultrafast")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=3840x2160:r=1",
        "-frames:v", "2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True, timeout=45)
    source_info = probe_source(source)
    rendition = select_quality_ladder(source_info["width"], source_info["height"], source_info["bitrate"])[-1]
    assert rendition["quality_label"] == "2160p"
    output = tmp_path / "2160p"
    execute_transcode_rendition(
        source, rendition, output, source_info["fps"], False, source_info["duration"],
        0, 1, {"current": 5.0},
    )
    assert "#EXT-X-ENDLIST" in (output / "index.m3u8").read_text(encoding="utf-8")
    segment = next(output.glob("*.ts"))
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
        "-of", "csv=p=0", str(segment),
    ], capture_output=True, text=True, check=True, timeout=20)
    assert {line for line in result.stdout.splitlines() if line} == {"3840,2160"}
