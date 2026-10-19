"""P0-4 fail-closed package staging and promotion-temp cleanup tests."""
import uuid
from types import SimpleNamespace

import pytest

from app.models.package import PackageSessionStatus
from app.services.package_service import PackageStorageCleanup


def session_for(root, *, status=PackageSessionStatus.FAILED.value):
    video_id, session_id = uuid.uuid4(), uuid.uuid4()
    path = root / str(video_id) / str(session_id)
    return SimpleNamespace(id=session_id, video_id=video_id, status=status, staging_path=str(path))


def test_terminal_staging_cleanup_is_idempotent_and_preserves_active_sessions(tmp_path):
    root = tmp_path / "packages"
    terminal = session_for(root)
    target = root / str(terminal.video_id) / str(terminal.id)
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}")
    assert PackageStorageCleanup.remove_terminal_staging(terminal, root)
    assert not target.exists()
    assert PackageStorageCleanup.remove_terminal_staging(terminal, root)

    active = session_for(root, status=PackageSessionStatus.UPLOADING.value)
    active_path = root / str(active.video_id) / str(active.id)
    active_path.mkdir(parents=True)
    assert not PackageStorageCleanup.remove_terminal_staging(active, root)
    assert active_path.exists()


@pytest.mark.parametrize("unsafe", ["../../outside", "/absolute/path", "C:/outside"])
def test_staging_cleanup_refuses_outside_traversal_and_wrong_package(tmp_path, unsafe):
    root = tmp_path / "packages"
    session = session_for(root)
    session.staging_path = unsafe
    assert not PackageStorageCleanup.remove_terminal_staging(session, root)
    assert root.parent.exists()


def test_staging_cleanup_refuses_root_and_another_package(tmp_path):
    root = tmp_path / "packages"
    session = session_for(root)
    root.mkdir()
    session.staging_path = str(root)
    assert not PackageStorageCleanup.remove_terminal_staging(session, root)
    other = root / str(session.video_id) / str(uuid.uuid4())
    other.mkdir(parents=True)
    session.staging_path = str(other)
    assert not PackageStorageCleanup.remove_terminal_staging(session, root)
    assert other.exists()


def test_abandoned_promotion_temp_is_exact_and_preserves_active_or_old_generation(tmp_path):
    root = tmp_path / "videos"
    video_id, temp_gen, active_gen = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    temp = root / str(video_id) / "generations" / f".tmp_{temp_gen}"
    valid_old = root / str(video_id) / "generations" / str(uuid.uuid4())
    temp.mkdir(parents=True)
    valid_old.mkdir(parents=True)
    assert not PackageStorageCleanup.remove_abandoned_promotion_temp(video_id=video_id, generation_id=temp_gen, active_generation_id=active_gen, has_active_job=True, videos_root=root)
    assert temp.exists()
    assert PackageStorageCleanup.remove_abandoned_promotion_temp(video_id=video_id, generation_id=temp_gen, active_generation_id=active_gen, has_active_job=False, videos_root=root)
    assert not temp.exists()
    assert valid_old.exists()
