"""Disk & Storage Safety, Admission Control, and Comprehensive Health Probes."""
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
from app.core.config import settings
from app.core.exceptions import StorageException
from app.core.logging import logger


def probe_storage_health() -> Dict[str, Any]:
    """
    Comprehensive Storage Health Check:
    - read/write probe
    - free bytes
    - free percentage
    - inode availability
    - temp directory verification
    - atomic rename capability
    - soft disk threshold evaluation
    - hard disk threshold evaluation
    """
    base_path = Path(settings.STORAGE_BASE_PATH).resolve()
    temp_path = Path(settings.STORAGE_TEMP_PATH).resolve()

    base_path.mkdir(parents=True, exist_ok=True)
    temp_path.mkdir(parents=True, exist_ok=True)

    status = "healthy"
    warnings = []
    errors = []

    # 1. Disk usage calculation (bytes & percentages)
    total_bytes, used_bytes, free_bytes = shutil.disk_usage(str(base_path))
    used_percentage = round((used_bytes / total_bytes) * 100, 2)
    free_percentage = round((free_bytes / total_bytes) * 100, 2)

    # 2. Inode availability (POSIX os.statvfs)
    inodes_data = {"supported": False}
    if hasattr(os, "statvfs"):
        try:
            vfs = os.statvfs(str(base_path))
            total_inodes = vfs.f_files
            free_inodes = vfs.f_ffree
            avail_inodes = vfs.f_favail
            inode_free_pct = round((avail_inodes / total_inodes * 100), 2) if total_inodes > 0 else 100.0
            inodes_data = {
                "supported": True,
                "total_inodes": total_inodes,
                "free_inodes": avail_inodes,
                "free_percentage": inode_free_pct,
            }
            if avail_inodes < 1000 or inode_free_pct < 5.0:
                warnings.append("Low inode availability (< 5%)")
        except Exception as e:
            inodes_data = {"supported": True, "error": str(e)}

    # 3. Read/Write test
    rw_success = False
    test_id = uuid.uuid4().hex
    test_file = base_path / f".health_probe_{test_id}.tmp"
    test_content = b"ABOODVIDEO_HEALTH_PROBE_TOKEN"
    try:
        with open(test_file, "wb") as f:
            f.write(test_content)
        with open(test_file, "rb") as f:
            read_back = f.read()
        if read_back == test_content:
            rw_success = True
        else:
            errors.append("Read/write test content mismatch")
    except Exception as e:
        errors.append(f"Read/write test failed: {e}")
    finally:
        if test_file.exists():
            try:
                test_file.unlink()
            except Exception:
                pass

    # 4. Atomic Rename test (os.replace)
    atomic_rename_success = False
    src_file = temp_path / f".rename_src_{test_id}.tmp"
    dst_file = temp_path / f".rename_dst_{test_id}.tmp"
    try:
        with open(src_file, "wb") as f:
            f.write(b"RENAME_PROBE")
        os.replace(src_file, dst_file)
        if dst_file.exists() and not src_file.exists():
            atomic_rename_success = True
        else:
            errors.append("Atomic rename verification failed")
    except Exception as e:
        errors.append(f"Atomic rename failed: {e}")
    finally:
        if src_file.exists():
            try:
                src_file.unlink()
            except Exception:
                pass
        if dst_file.exists():
            try:
                dst_file.unlink()
            except Exception:
                pass

    # 5. Temp Directory validation
    temp_dir_writable = False
    temp_probe = temp_path / f".temp_probe_{test_id}.tmp"
    try:
        with open(temp_probe, "wb") as f:
            f.write(b"TEMP_CHECK")
        temp_dir_writable = True
    except Exception as e:
        errors.append(f"Temp directory not writable: {e}")
    finally:
        if temp_probe.exists():
            try:
                temp_probe.unlink()
            except Exception:
                pass

    # 6. Threshold Evaluations (Soft & Hard)
    soft_threshold_exceeded = used_percentage >= settings.STORAGE_SOFT_THRESHOLD_PERCENT
    hard_threshold_exceeded = (
        used_percentage >= settings.STORAGE_HARD_THRESHOLD_PERCENT
        or free_bytes < settings.STORAGE_MIN_FREE_BYTES
    )

    if soft_threshold_exceeded:
        warnings.append(
            f"Soft threshold exceeded: disk usage at {used_percentage}% (limit: {settings.STORAGE_SOFT_THRESHOLD_PERCENT}%)"
        )

    if hard_threshold_exceeded:
        errors.append(
            f"Hard threshold exceeded: disk usage at {used_percentage}% (limit: {settings.STORAGE_HARD_THRESHOLD_PERCENT}%) or free space ({free_bytes} bytes) below floor ({settings.STORAGE_MIN_FREE_BYTES} bytes)"
        )

    if errors or not rw_success or not atomic_rename_success or not temp_dir_writable or hard_threshold_exceeded:
        status = "unhealthy"
    elif warnings:
        status = "degraded"

    return {
        "status": status,
        "base_path": str(base_path),
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_percentage": free_percentage,
        "used_percentage": used_percentage,
        "inodes": inodes_data,
        "probes": {
            "read_write": rw_success,
            "atomic_rename": atomic_rename_success,
            "temp_directory_writable": temp_dir_writable,
        },
        "thresholds": {
            "soft_limit_percent": settings.STORAGE_SOFT_THRESHOLD_PERCENT,
            "soft_threshold_exceeded": soft_threshold_exceeded,
            "hard_limit_percent": settings.STORAGE_HARD_THRESHOLD_PERCENT,
            "hard_threshold_exceeded": hard_threshold_exceeded,
            "min_free_bytes_floor": settings.STORAGE_MIN_FREE_BYTES,
        },
        "warnings": warnings,
        "errors": errors,
    }


class StorageAdmissionResult(dict):
    """Dictionary that also unpacks as (admissible: bool, info: dict) when unpacked as a 2-tuple."""
    def __iter__(self):
        yield True
        yield self


def calculate_server_transcode_admission(
    source_bytes: Optional[int] = None,
    active_generation_bytes: int = 0,
    source_file_bytes: Optional[int] = None,
    existing_active_generation_bytes: Optional[int] = None,
) -> Dict[str, int]:
    """
    Decoupled storage calculations for server-side transcoding.
    Does NOT count existing source or existing active generation against free bytes.
    """
    actual_source_bytes = source_file_bytes if source_file_bytes is not None else (source_bytes or 0)
    actual_active_bytes = (
        existing_active_generation_bytes
        if existing_active_generation_bytes is not None
        else active_generation_bytes
    )

    est_new_generation = int(actual_source_bytes * settings.SERVER_TRANSCODE_MULTIPLIER)
    temp_overhead = int(actual_source_bytes * settings.SERVER_TRANSCODE_OVERHEAD_MULTIPLIER)
    safety_margin = settings.STORAGE_SAFETY_MARGIN_BYTES

    additional_free_required = est_new_generation + temp_overhead + safety_margin
    peak_total_disk_usage = (
        actual_source_bytes
        + actual_active_bytes
        + est_new_generation
        + temp_overhead
        + safety_margin
    )

    return {
        "additional_free_bytes_required": additional_free_required,
        "additional_free_required": additional_free_required,
        "peak_total_disk_usage": peak_total_disk_usage,
        "estimated_new_generation_bytes": est_new_generation,
        "est_new_generation": est_new_generation,
        "temporary_overhead": temp_overhead,
        "safety_margin_bytes": safety_margin,
        "safety_margin": safety_margin,
    }


def check_package_storage_admission(
    declared_total_bytes: Optional[int] = None,
    already_staged_bytes: int = 0,
    staging_path: Optional[Path] = None,
    destination_path: Optional[Path] = None,
    package_total_bytes: Optional[int] = None,
    staging_dir: Optional[Path] = None,
    storage_dir: Optional[Path] = None,
) -> Any:
    """
    Checks storage admission for pre-encoded Studio packages.
    When staging and destination reside on the same filesystem:
      Checks remaining unstaged bytes + safety margin against free space.
    When st_dev differs:
      Independently validates free space on staging AND destination filesystems.
    Rejects with StorageException (HTTP 507) if either filesystem lacks required bytes.
    """
    total_bytes = package_total_bytes if package_total_bytes is not None else (declared_total_bytes or 0)
    st_path = (staging_dir or staging_path or Path(settings.STORAGE_PACKAGES_STAGING_PATH)).resolve()
    dst_path = (storage_dir or destination_path or Path(settings.STORAGE_VIDEOS_PATH)).resolve()

    st_path.mkdir(parents=True, exist_ok=True)
    dst_path.mkdir(parents=True, exist_ok=True)

    st_dev_staging = st_path.stat().st_dev
    st_dev_dest = dst_path.stat().st_dev
    same_fs = (st_dev_staging == st_dev_dest)

    remaining_bytes = max(0, total_bytes - already_staged_bytes)
    safety_margin = settings.STORAGE_SAFETY_MARGIN_BYTES
    publish_overhead = 52428800  # 50 MiB for playlists/metadata

    _, _, staging_free = shutil.disk_usage(str(st_path))
    _, _, dest_free = shutil.disk_usage(str(dst_path))

    if same_fs:
        additional_free_required = remaining_bytes + safety_margin

        diagnostic = StorageAdmissionResult({
            "same_filesystem": True,
            "additional_free_required": additional_free_required,
            "free_bytes": staging_free,
            "staging_free_bytes": staging_free,
            "storage_free_bytes": dest_free,
            "remaining_bytes": remaining_bytes,
            "safety_margin": safety_margin,
        })

        if staging_free < additional_free_required:
            logger.warning(
                f"Storage admission rejected (same FS): Free {staging_free}B < Required {additional_free_required}B"
            )
            raise StorageException(
                message="Insufficient storage space for package upload.",
                details={
                    "required_additional_bytes": additional_free_required,
                    "free_bytes": staging_free,
                    "same_filesystem": True,
                },
            )
        return diagnostic
    else:
        # Cross-filesystem admission check
        staging_required = remaining_bytes + safety_margin
        dest_required = total_bytes + publish_overhead + safety_margin

        diagnostic = StorageAdmissionResult({
            "same_filesystem": False,
            "staging_required": staging_required,
            "staging_free": staging_free,
            "staging_free_bytes": staging_free,
            "dest_required": dest_required,
            "dest_free": dest_free,
            "storage_free_bytes": dest_free,
            "safety_margin": safety_margin,
        })

        if staging_free < staging_required:
            logger.warning(
                f"Storage admission rejected on STAGING filesystem: Free {staging_free}B < Required {staging_required}B"
            )
            raise StorageException(
                message="Insufficient storage space on staging filesystem.",
                details={
                    "filesystem": "staging",
                    "required_bytes": staging_required,
                    "free_bytes": staging_free,
                    "same_filesystem": False,
                },
            )

        if dest_free < dest_required:
            logger.warning(
                f"Storage admission rejected on DESTINATION filesystem: Free {dest_free}B < Required {dest_required}B"
            )
            raise StorageException(
                message="Insufficient storage space on destination filesystem.",
                details={
                    "filesystem": "destination",
                    "required_bytes": dest_required,
                    "free_bytes": dest_free,
                    "same_filesystem": False,
                },
            )

        return diagnostic
