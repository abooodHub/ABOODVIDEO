"""Storage Health Probe and Disk Safety Tests."""
import pytest
from app.utils.disk import probe_storage_health
from app.storage.local import LocalStorageService
from app.core.exceptions import StorageException


def test_storage_health_probe_criteria():
    """Verify all 8 criteria of Rule 5."""
    health = probe_storage_health()

    assert health["status"] in ["healthy", "degraded", "unhealthy"]
    assert "free_bytes" in health
    assert "total_bytes" in health
    assert "free_percentage" in health
    assert "used_percentage" in health

    # Inode availability data structure
    assert "inodes" in health

    # Probes: read/write, atomic rename, temp directory
    probes = health["probes"]
    assert probes["read_write"] is True
    assert probes["atomic_rename"] is True
    assert probes["temp_directory_writable"] is True

    # Thresholds
    thresholds = health["thresholds"]
    assert "soft_limit_percent" in thresholds
    assert "hard_limit_percent" in thresholds
    assert "soft_threshold_exceeded" in thresholds
    assert "hard_threshold_exceeded" in thresholds


@pytest.mark.asyncio
async def test_local_storage_service():
    storage = LocalStorageService()
    assert await storage.exists("non_existent_file.tmp") is False

    # Path traversal protection: relative traversal
    with pytest.raises(StorageException):
        storage._resolve("../../etc/passwd")

    # Path traversal protection: absolute path outside storage
    with pytest.raises(StorageException):
        storage._resolve("/etc/shadow")

    with pytest.raises(StorageException):
        storage._resolve("C:\\Windows\\System32\\calc.exe")

    # Protection against targeting storage root directly
    with pytest.raises(StorageException):
        storage._resolve(str(storage.base_path))

    # Deletion safety outside storage root
    with pytest.raises(StorageException):
        await storage.delete("../../../outside.txt")

    with pytest.raises(StorageException):
        await storage.delete("/etc/passwd")

    # delete_video_dir safety
    import uuid
    v_id = uuid.uuid4()
    # Deleting non-existent video dir should safely return False without error
    res = await storage.delete_video_dir(v_id)
    assert res is False
