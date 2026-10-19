"""Publishable source and interrupted-import recovery regressions."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_new_studio_profile_has_no_preconfigured_server():
    from studio.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG['server_url'] == ''


def test_default_embed_allowlist_contains_only_loopback(monkeypatch):
    from app.services.embed_settings import parse_domains_string
    monkeypatch.delenv('ABOODVIDEO_EMBED_ORIGINS', raising=False)
    assert parse_domains_string(None) == ['localhost', '127.0.0.1']


def test_secret_checker_redacts_and_detects_real_credential_shapes():
    spec = importlib.util.spec_from_file_location("repository_secret_check", ROOT / "scripts/check_repository_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = b"ak_live_" + b"x" * 40
    assert module.inspect("scripts/live.py", b"key = '" + token + b"'") == [(1, "provider credential")]
    assert module.inspect("scripts/live.py", b"hash_password('predictable-account-password')") == [(1, "fixed account password")]
    assert not module.inspect("scripts/live.py", b"password = os.environ['OWNER_PASSWORD']")
    assert module.inspect("settings.txt", b"prefix private-value suffix", [b"private-value"])


def test_import_interruption_retains_saved_rows_and_resumes_without_duplicates(tmp_path, monkeypatch):
    import studio.core.batch_runner as module
    from studio.core.batch_runner import BatchRunner
    from studio.core.batch_store import BatchStore
    for name in ("01.mkv", "02.mkv"):
        (tmp_path / name).write_bytes(b"synthetic")
    monkeypatch.setattr(module, "load_studio_config", lambda: {})
    def interrupted(folder, on_episode_scanned=None, *, video_files=None):
        first = video_files[0]
        on_episode_scanned(dict(source_path=str(first), filename=first.name, state="PENDING", settings_json={}))
        raise RuntimeError("Synthetic interrupted scan")
    monkeypatch.setattr(module, "scan_season_folder", interrupted)
    store = BatchStore(tmp_path / "queue.db")
    runner = BatchRunner(store)
    with pytest.raises(RuntimeError, match="interrupted"):
        runner.import_season_folder(tmp_path)
    saved, = store.list_all_episodes()
    assert saved["filename"] == "01.mkv"
    assert saved["batch_status"] == "PAUSED"
    def resumed(folder, on_episode_scanned=None, *, video_files=None):
        assert [p.name for p in video_files] == ["02.mkv"]
        rows = [dict(source_path=str(p), filename=p.name, state="PENDING", settings_json={}) for p in video_files]
        for row in rows:
            on_episode_scanned(row)
        return rows
    monkeypatch.setattr(module, "scan_season_folder", resumed)
    restarted = BatchRunner(BatchStore(tmp_path / "queue.db"))
    result = restarted.import_season_folder(tmp_path)
    assert result["new_episode_count"] == 1
    rows = restarted.store.get_episodes(result["batch"]["id"])
    assert [row["order_index"] for row in rows] == [1, 2]
    assert rows[0]["id"] == saved["id"]
    assert not restarted.is_running


def test_bundled_notices_are_in_release_manifest():
    spec = importlib.util.spec_from_file_location("release_manifest_notices", ROOT / "scripts/build_studio_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {row["path"] for row in module.source_manifest()["files"]}
    assert "backend/app/static/vendor/jassub/COPYRIGHT" in names
    assert "backend/app/static/vendor/jassub/LICENSE" in names
    assert "backend/app/static/js/vendor/hls.LICENSE" in names
    assert "THIRD_PARTY_NOTICES.md" in names
    assert not any(name.startswith("studio/data/") for name in names)


def test_logging_redacts_playback_sessions_and_wordpress_tokens():
    from app.core.logging import sanitize_path_for_logging
    assert sanitize_path_for_logging("/api/v1/playback/s/private-session/master.m3u8") == "/api/v1/playback/s/[REDACTED]/master.m3u8"
    assert sanitize_path_for_logging("/private-media/private-signed-token/e1/file.ts") == "/private-media/[REDACTED]/e1/file.ts"


def test_studio_import_does_not_create_directories_in_readonly_installation(tmp_path, monkeypatch):
    import runpy
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    original = Path.mkdir
    bundle_fonts = ROOT / "studio/fonts"
    def protected(path, *args, **kwargs):
        if path == bundle_fonts:
            raise PermissionError("Synthetic read-only installation")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", protected)
    config = runpy.run_path(str(ROOT / "studio/config.py"))
    assert config["MANAGED_FONTS_DIR"].is_dir()
    assert config["MANAGED_FONTS_DIR"].is_relative_to(tmp_path)
