"""Code audit regressions: disposable data and mocked probes, no encode/upload."""
import json
import ast
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_live_browser_check_reads_password_from_environment():
    script_path = Path("scripts/verify_content_library_ui.py")
    source = script_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(script_path))
    password_fills = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "fill"
        and len(call.args) >= 2
        and isinstance(call.args[0], ast.Constant)
        and "password" in str(call.args[0].value).lower()
    ]

    assert len(password_fills) == 1
    assert isinstance(password_fills[0].args[1], ast.Name)
    assert password_fills[0].args[1].id == "admin_password"
    assert 'os.environ.get("ABOODVIDEO_ADMIN_PASSWORD")' in source


def test_output_validation_preserves_existing_probe_file(tmp_path):
    from studio.config import validate_converted_output_folder
    existing = tmp_path / ".aboodvideo-write-test"
    existing.write_bytes(b"user retained data")
    assert validate_converted_output_folder(str(tmp_path)) == tmp_path
    assert existing.read_bytes() == b"user retained data"
    assert sorted(p.name for p in tmp_path.iterdir()) == [existing.name]


def test_fractional_episode_output_does_not_collide(tmp_path):
    from studio.core.output_paths import batch_output_directory
    season = tmp_path / "Series" / "Season 01"
    kwargs = dict(config={"converted_output_folder": str(tmp_path)}, source_path=season / "06.mkv", season_folder=season)
    integer = batch_output_directory(**kwargs, episode_number=6)
    special = batch_output_directory(**kwargs, episode_number=6.5)
    assert integer.name == "Episode 006"
    assert special.name == "Episode 006.5"
    assert integer != special


def test_season_output_paths_do_not_collide_and_override_legacy_work_dir(tmp_path):
    from studio.core.output_paths import batch_output_directory
    source = tmp_path / "Ling Cage (2019)" / "Season 01" / "01.mkv"
    legacy = tmp_path / "upload" / "Ling Cage (2019)" / "Episode 001"
    season_one = tmp_path / "upload" / "Ling Cage (2019)" / "Season 01" / "Episode 001"
    season_two = tmp_path / "upload" / "Ling Cage (2019)" / "Season 02" / "Episode 001"
    config = {"converted_output_folder": str(tmp_path / "upload")}
    Path(config["converted_output_folder"]).mkdir()

    first = batch_output_directory(
        config, source_path=source, season_folder=source.parent, episode_number=1
    )
    second = batch_output_directory(
        config,
        source_path=source.parent.parent / "Season 02" / "01.mkv",
        season_folder=source.parent.parent / "Season 02",
        episode_number=1,
    )
    recovered = batch_output_directory(
        config,
        source_path=source,
        season_folder=source.parent.parent,
        episode_number=1,
        existing_work_dir=str(legacy),
        preferred_work_dir=str(season_one),
    )

    assert first == season_one
    assert second == season_two
    assert first != second
    assert recovered == season_one


def test_fresh_batch_output_preserves_existing_package_directory(tmp_path):
    from studio.core.output_paths import fresh_batch_output_directory

    output = tmp_path / "upload"
    output.mkdir()
    season = tmp_path / "Ling Cage (2019)" / "Season 02"
    source = season / "03.mkv"
    existing = output / "Ling Cage (2019)" / "Season 02" / "Episode 003"
    existing.mkdir(parents=True)
    marker = existing / "old-package.ts"
    marker.write_bytes(b"keep")

    fresh = fresh_batch_output_directory(
        {"converted_output_folder": str(output)},
        source_path=source,
        season_folder=season,
        episode_number=3,
    )

    assert fresh == existing.with_name("Episode 003 (2)")
    assert marker.read_bytes() == b"keep"


def test_episode_zero_is_preserved_in_queue(tmp_path):
    from studio.core.batch_store import BatchStore
    store = BatchStore(tmp_path / "jobs.db")
    batch = store.create_batch(str(tmp_path))
    rows = store.add_episodes(batch["id"], [{"source_path": str(tmp_path / "00.mkv"), "filename": "00.mkv", "order_index": 1, "episode_number": 0}])
    assert rows[0]["episode_number"] == 0
    assert store.get_episode(rows[0]["id"])["episode_number"] == 0


@pytest.mark.parametrize("name", ["CON", "con.txt", "NUL.mkv", "AUX.ass", "LPT1.any"])
def test_output_path_rejects_windows_device_names(name):
    from studio.core.output_paths import safe_path_component
    assert safe_path_component(name, "Media") == "Media"


def test_single_output_handles_file_collision(tmp_path):
    from studio.core.output_paths import single_output_directory
    (tmp_path / "Pilot").write_bytes(b"retained")
    output = single_output_directory({"converted_output_folder": str(tmp_path)}, source_path=tmp_path / "a.mkv", title="Pilot", job_id="abcd1234")
    assert output.name == "Pilot (abcd1234)"
    assert (tmp_path / "Pilot").read_bytes() == b"retained"


def test_font_zip_import_preserves_same_named_existing_font(tmp_path, monkeypatch):
    import zipfile
    import studio.core.fonts as fonts
    target = tmp_path / "fonts"
    target.mkdir()
    existing = target / "Family.ttf"
    existing.write_bytes(b"retained font")
    archive = tmp_path / "fonts.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("Family.ttf", b"new font")
    monkeypatch.setattr(fonts, "extract_font_names_from_ttf", lambda _: {"Family"})
    imported = fonts.import_fonts_from_zip(archive, target)
    assert existing.read_bytes() == b"retained font"
    assert Path(imported[0]["path"]).read_bytes() == b"new font"
    assert imported[0]["path"] != str(existing)
    assert fonts.import_fonts_from_zip(archive, target)[0]["path"] == imported[0]["path"]


def test_font_upload_retains_existing_file_and_rejects_non_font(tmp_path, monkeypatch):
    import studio.core.fonts as fonts
    monkeypatch.setattr(fonts, "MANAGED_FONTS_DIR", tmp_path)
    monkeypatch.setattr(fonts, "extract_font_names_from_ttf", lambda _: {"Family"})
    (tmp_path / "Family.ttf").write_bytes(b"retained")
    result = fonts.attach_font_file("Family.ttf", b"new font")
    assert (tmp_path / "Family.ttf").read_bytes() == b"retained"
    assert Path(result["saved_path"]).read_bytes() == b"new font"
    with pytest.raises(ValueError):
        fonts.attach_font_file("file.html", b"invalid")
    assert not (tmp_path / "file.html").exists()


def test_package_fonts_keep_distinct_same_named_binaries(tmp_path, monkeypatch):
    import studio.core.fonts as fonts
    sources = []
    for folder, content in [("first", b"font A"), ("second", b"font B")]:
        path = tmp_path / folder / "Family.ttf"
        path.parent.mkdir()
        path.write_bytes(content)
        sources.append(path)
    monkeypatch.setattr(fonts, "get_font_library_attached_fonts", lambda: {"a": {"path": str(sources[0])}, "b": {"path": str(sources[1])}})
    monkeypatch.setattr(fonts, "get_studio_attached_fonts", lambda: {})
    monkeypatch.setattr(fonts, "get_windows_installed_fonts", lambda: {})
    monkeypatch.setattr(fonts, "get_season_folder_fonts", lambda _: {})
    destination = tmp_path / "package" / "fonts"
    records = fonts.package_required_fonts(["A", "B"], destination)
    assert len({record["filename"] for record in records}) == 2
    assert {path.read_bytes() for path in destination.iterdir()} == {b"font A", b"font B"}
    assert all(fonts.compute_sha256(destination / item["filename"]) == item["sha256"] for item in records)


@pytest.mark.parametrize("scopes,ok", [(None, False), ([], False), (["packages:create"], False),
    (["*"], True), (["packages:create", "packages:write", "packages:publish"], True), ("*", False)])
def test_package_upload_requires_declared_scopes(monkeypatch, scopes, ok):
    import studio.core.uploader as uploader
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"scopes": scopes}))
    monkeypatch.setattr(uploader.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    result = uploader.test_connection("http://isolated.test", "audit-only", require_package_upload=True)
    assert result["ok"] is ok


@pytest.mark.parametrize("host,origin,fetch,status", [
    ("127.0.0.1:8555", "http://127.0.0.1:8555", "same-origin", 200),
    ("127.0.0.1:8555", "https://external.example", "cross-site", 403),
    ("127.0.0.1:8555", "http://127.0.0.1:8556", "same-site", 403),
    ("evil.example:8555", "http://evil.example:8555", "same-origin", 400),
    ("[::1]:8555", "http://[::1]:8555", "same-origin", 200),
    ("localhost:8555", "null", "cross-site", 403),
])
def test_local_studio_rejects_foreign_browser_actions(monkeypatch, host, origin, fetch, status):
    from studio.web.app import app
    from studio.web.routes import batch_api
    calls = []
    monkeypatch.setattr(batch_api.batch_runner, "pause_batch", lambda: calls.append(True) or {})
    client = TestClient(app, base_url="http://127.0.0.1:8555")
    response = client.post("/api/batch/pause", headers={"host": host, "origin": origin, "sec-fetch-site": fetch})
    assert response.status_code == status
    assert calls == ([True] if status == 200 else [])


def test_local_studio_websocket_rejects_foreign_origin():
    from studio.web.app import app
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/api/ws/telemetry", headers={"origin": "https://external.example"}):
                pass
    assert error.value.code == 1008


def test_attachment_fonts_are_dumped_once_without_hashing_video(tmp_path, monkeypatch):
    import studio.core.fonts as fonts
    from studio.core import ffmpeg_finder
    video = tmp_path / "01.mkv"
    video.write_bytes(b"mock media")
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "mock-ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [
                {"index": 2, "tags": {"filename": "bad:name.ttf"}},
                {"index": 3, "tags": {"filename": "second.otf"}},
            ]}))
        for index, arg in enumerate(cmd):
            if arg.startswith("-dump_attachment:"):
                Path(cmd[index + 1]).write_bytes(arg.encode())
        return SimpleNamespace(returncode=0)
    original_hash = fonts.compute_sha256
    def forbid_video_hash(path):
        assert Path(path) != video
        return original_hash(path)
    monkeypatch.setattr(fonts, "compute_sha256", forbid_video_hash)
    monkeypatch.setattr(fonts, "run_headless", fake_run)
    monkeypatch.setattr(fonts, "extract_font_names_from_ttf", lambda path: {"Verified Family"})
    monkeypatch.setattr(ffmpeg_finder, "get_ffprobe_path", lambda: "mock-ffprobe")
    monkeypatch.setattr(ffmpeg_finder, "get_ffmpeg_path", lambda: "mock-ffmpeg")
    monkeypatch.setattr(fonts, "TEMP_DIR", tmp_path / "temp")
    records = fonts.extract_mkv_font_attachments(video)
    assert len(records) == 2
    assert len(calls) == 2
    assert all(":" not in Path(record["saved_path"]).name for record in records)


def test_embedded_subtitle_probe_uses_bundled_binary_and_utf8(tmp_path, monkeypatch):
    import studio.core.episode_scanner as scanner
    from studio.core import ffmpeg_finder
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "bundled-ffprobe"
        assert kwargs["encoding"] == "utf-8"
        assert "stream_disposition=forced,default" in cmd[cmd.index("-show_entries") + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"index": 2, "codec_name": "ass", "tags": {"language": "ara", "title": "العربية"}, "disposition": {"forced": 1, "default": 1}}]}))
    monkeypatch.setattr(ffmpeg_finder, "get_ffprobe_path", lambda: "bundled-ffprobe")
    monkeypatch.setattr(scanner, "run_headless", fake_run)
    result = scanner.probe_embedded_subtitles(tmp_path / "01.mkv")
    assert result[0]["title"] == "العربية"
    assert result[0]["forced"] and result[0]["default"]


def _fake_audit(**overrides):
    return dict(subtitle_source="none", subtitle_path="", subtitle_format="", subtitle_stream_index=None,
        subtitle_label="none", subtitle_metadata={}, font_status="—", missing_fonts=[], font_audit={},
        subtitle_candidates=[], state="PENDING", error=None, **overrides)


def test_scanner_saves_first_episode_before_scanning_next_and_reuses_later_fonts(tmp_path, monkeypatch):
    import studio.core.episode_scanner as scanner
    for name in ("01.mkv", "02.mkv"):
        (tmp_path / name).write_bytes(b"dummy")
    events = []
    def pool(videos, subtitles, season):
        events.extend(f"fonts:{v.name}" for v in videos)
        return tmp_path
    def audit(video_path, *args, **kwargs):
        if Path(video_path).name == "01.mkv" and "fonts:02.mkv" not in events:
            result = _fake_audit()
            result.update(state="NEEDS_REVIEW", missing_fonts=["ExactFamily"], error="missing")
            return result
        return _fake_audit()
    monkeypatch.setattr(scanner, "_prepare_season_font_reuse", pool)
    monkeypatch.setattr(scanner, "probe_video", lambda _: {"subtitle_tracks": []})
    monkeypatch.setattr(scanner, "audit_subtitle_and_fonts", audit)
    episodes = scanner.scan_season_folder(tmp_path, on_episode_scanned=lambda ep: events.append(f"saved:{ep['filename']}:{ep['state']}"))
    assert events.index("saved:01.mkv:NEEDS_REVIEW") < events.index("fonts:02.mkv")
    assert events[-1] == "saved:01.mkv:PENDING"
    assert all(ep["state"] == "PENDING" for ep in episodes)


def test_runner_persists_incrementally_and_refreshes_sibling_font_audit(tmp_path, monkeypatch):
    from studio.core.batch_store import BatchStore
    from studio.core.batch_runner import BatchRunner
    import studio.core.batch_runner as runner_module
    import studio.core.episode_scanner as scanner
    for name in ("01.mkv", "02.mkv"):
        (tmp_path / name).write_bytes(b"fixture")
    store = BatchStore(tmp_path / "jobs.db")
    runner = BatchRunner(store)
    seen_second = False
    def pool(videos, subtitles, season):
        nonlocal seen_second
        if videos and videos[0].name == "02.mkv":
            saved = store.list_all_episodes()
            assert len(saved) == 1
            assert saved[0]["state"] == "NEEDS_REVIEW"
            assert store.get_latest_active_batch()["status"] == "PAUSED"
            seen_second = True
        return tmp_path
    def audit(video_path, *args, **kwargs):
        result = _fake_audit()
        if Path(video_path).name == "01.mkv" and not seen_second:
            result.update(state="NEEDS_REVIEW", missing_fonts=["Family"], error="missing fonts")
        return result
    monkeypatch.setattr(runner_module, "load_studio_config", lambda: {})
    monkeypatch.setattr(scanner, "probe_video", lambda _: {"subtitle_tracks": []})
    monkeypatch.setattr(scanner, "_prepare_season_font_reuse", pool)
    monkeypatch.setattr(scanner, "audit_subtitle_and_fonts", audit)
    result = runner.import_season_folder(tmp_path)
    assert result["new_episode_count"] == 2
    assert all(ep["state"] == "PENDING" for ep in store.list_all_episodes())
    assert store.get_latest_active_batch()["status"] == "PAUSED"
    assert not result["runner"]["is_running"]


def test_season_reimport_appends_without_order_collision(tmp_path, monkeypatch):
    from studio.core.batch_store import BatchStore
    from studio.core.batch_runner import BatchRunner
    import studio.core.batch_runner as module
    store = BatchStore(tmp_path / "jobs.db")
    runner = BatchRunner(store)
    batch = store.create_batch(str(tmp_path))
    source = tmp_path / "02.mkv"
    source.write_bytes(b"dummy")
    store.add_episodes(batch["id"], [{"source_path": str(source), "filename": source.name, "order_index": 1, "episode_number": 2}])
    (tmp_path / "01.mkv").write_bytes(b"fixture")
    def scan(folder, on_episode_scanned=None, *, video_files=None):
        rows = [dict(source_path=str(tmp_path / name), filename=name, order_index=i, episode_number=i, settings_json={}) for i, name in enumerate(("01.mkv", "02.mkv"), 1)]
        selected = {str(p.resolve()).casefold() for p in video_files}
        rows = [row for row in rows if str(Path(row["source_path"]).resolve()).casefold() in selected]
        for row in rows:
            on_episode_scanned(row)
        return rows
    monkeypatch.setattr(module, "scan_season_folder", scan)
    monkeypatch.setattr(module, "load_studio_config", lambda: {})
    result = runner.import_season_folder(tmp_path)
    assert result["new_episode_count"] == 1
    assert len(store.get_episodes(batch["id"])) == 2
    assert [ep["order_index"] for ep in store.get_episodes(batch["id"])] == [1, 2]
    assert not runner.get_runner_status()["is_running"]


def test_failed_first_insert_leaves_no_empty_batch(tmp_path, monkeypatch):
    from studio.core.batch_store import BatchStore
    from studio.core.batch_runner import BatchRunner
    import studio.core.batch_runner as module
    store = BatchStore(tmp_path / "jobs.db")
    runner = BatchRunner(store)
    monkeypatch.setattr(module, "load_studio_config", lambda: {})
    (tmp_path / "01.mkv").write_bytes(b"fixture")
    def scan(folder, on_episode_scanned=None, **kwargs):
        on_episode_scanned({"source_path": str(tmp_path / "01.mkv"), "filename": "01.mkv", "order_index": 1})
        return []
    def reject(*args, **kwargs):
        raise RuntimeError("mock database insertion failure")
    monkeypatch.setattr(module, "scan_season_folder", scan)
    monkeypatch.setattr(store, "add_episodes", reject)
    with pytest.raises(RuntimeError, match="mock database"):
        runner.import_season_folder(tmp_path)
    assert store.list_batches() == []
    assert store.list_all_episodes() == []


def test_duplicate_single_file_import_does_not_probe_again(tmp_path, monkeypatch):
    from studio.core.batch_store import BatchStore
    from studio.core.batch_runner import BatchRunner
    import studio.core.batch_runner as module
    source = tmp_path / "01.mkv"
    source.write_bytes(b"fixture")
    store = BatchStore(tmp_path / "jobs.db")
    batch = store.create_batch(str(tmp_path))
    store.add_episodes(batch["id"], [{"source_path": str(source), "filename": source.name, "order_index": 1}])
    def reject_probe(*args, **kwargs):
        pytest.fail("A duplicate source must not be probed again")
    monkeypatch.setattr(module, "probe_video", reject_probe)
    assert BatchRunner(store).import_media_file(source)["new_episode_count"] == 0
    assert source.read_bytes() == b"fixture"


def test_series_preview_includes_nested_episode_directory(tmp_path):
    from studio.web.app import app
    folder = tmp_path / "Series" / "Season 01" / "Episodes"
    folder.mkdir(parents=True)
    (folder / "01.mkv").write_bytes(b"fixture")
    response = TestClient(app).post("/api/batch/inspect-series", json={"folder_path": str(folder.parents[1])})
    assert response.status_code == 200
    assert response.json()["episode_count"] == 1
    season, = response.json()["seasons"]
    assert season["name"] == "Season 01"
    assert season["season_number"] == 1
    assert season["episode_count"] == 1
    assert season["episodes"][0]["relative_source_path"] == "Season 01/Episodes/01.mkv"


@pytest.mark.asyncio
async def test_drive_share_listing_and_revoke_enforce_owner_scope():
    from app.api.v1.drive import list_user_shares, revoke_share
    from app.core.database import AsyncSessionLocal
    from app.models.user import User, UserRole
    from app.models.share import Share
    async with AsyncSessionLocal() as db:
        owner = User(id=uuid.uuid4(), username=f"audit_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test", hashed_password="test-only", role=UserRole.OWNER, is_active=True)
        other = User(id=uuid.uuid4(), username=f"audit_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test", hashed_password="test-only", role=UserRole.OWNER, is_active=True)
        db.add_all([owner, other])
        await db.flush()
        share = Share(id=uuid.uuid4(), owner_id=owner.id, token=uuid.uuid4().hex, target_type="video", target_id=uuid.uuid4(), expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        db.add(share)
        await db.commit()
        listed = await list_user_shares(db=db, current_user=owner, status_filter=None, page=1, limit=20, q=None, target_type=None, sort="created_at", direction="desc")
        assert next(item for item in listed["shares"] if item["id"] == str(share.id))["status"] == "expired"
        for identifier in (str(share.id), share.token):
            with pytest.raises(HTTPException) as error:
                await revoke_share(identifier, db=db, current_user=other)
            assert error.value.status_code == 404
        assert share.revoked_at is None
        assert (await revoke_share(str(share.id), db=db, current_user=owner))["success"]


@pytest.mark.asyncio
async def test_drive_download_and_policy_ownership_and_nested_share(tmp_path):
    from starlette.requests import Request
    from app.api.v1.drive import download_video_owner, download_shared_video
    from app.core.database import AsyncSessionLocal
    from app.models.user import User, UserRole
    from app.models.folder import Folder
    from app.models.video import Video, VideoStatus
    from app.models.share import Share
    from app.services.drive_service import DriveService
    async with AsyncSessionLocal() as db:
        owners = [User(id=uuid.uuid4(), username=f"audit_{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test", hashed_password="test-only", role=UserRole.OWNER, is_active=True) for _ in range(2)]
        db.add_all(owners)
        await db.flush()
        parent = Folder(id=uuid.uuid4(), user_id=owners[0].id, name="Audit parent", allow_download_policy="ALLOW")
        db.add(parent)
        await db.flush()
        child = Folder(id=uuid.uuid4(), user_id=owners[0].id, name="Audit child", parent_id=parent.id, allow_download_policy="ALLOW")
        db.add(child)
        await db.flush()
        source = tmp_path / "retained.mkv"
        source.write_bytes(b"fixture only")
        video = Video(id=uuid.uuid4(), user_id=owners[0].id, title="Audit", source_filename="a.mkv", source_path=str(source), folder_id=child.id, status=VideoStatus.READY, allow_download_policy="ALLOW")
        share = Share(id=uuid.uuid4(), owner_id=owners[0].id, token=uuid.uuid4().hex, target_type="folder", target_id=parent.id, allow_download=True)
        db.add_all([video, share])
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await download_video_owner(video.id, db=db, current_user=owners[1])
        assert error.value.status_code == 404
        with pytest.raises(ValueError):
            await DriveService.update_download_policy(db, "video", video.id, "DENY", owners[1].id)
        assert video.allow_download_policy == "ALLOW"
        assert (await download_video_owner(video.id, db=db, current_user=owners[0])).path == str(source)
        request = Request({"type": "http", "headers": []})
        assert (await download_shared_video(share.token, video.id, request=request, db=db)).path == str(source)
        video.trashed_at = datetime.now(timezone.utc)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await download_shared_video(share.token, video.id, request=request, db=db)
        assert error.value.status_code == 404
        assert source.read_bytes() == b"fixture only"
