"""Local Filesystem Implementation of StorageService with Strict Path Traversal Safety."""
import os
import shutil
import uuid
import aiofiles
from pathlib import Path
from typing import BinaryIO, Dict, Any, Union
from app.storage.base import StorageService
from app.core.config import settings
from app.core.exceptions import StorageException


class LocalStorageService(StorageService):
    def __init__(self, base_path: Union[str, Path] = settings.STORAGE_BASE_PATH):
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        """
        Safely resolve a relative or absolute storage path against self.base_path.
        Constrained strictly to the configured storage root.
        Path traversal, absolute paths outside storage root, and targeting base root are blocked.
        """
        # Detect Windows drive letters (e.g. C:) when executing on POSIX/Linux
        if os.name != "nt":
            norm = path.replace("\\", "/")
            if len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha():
                raise StorageException(f"Path outside storage root detected: {path}")

        p = Path(path)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (self.base_path / path).resolve()

        # Path traversal and storage root constraint:
        try:
            resolved.relative_to(self.base_path)
        except ValueError:
            raise StorageException(f"Path traversal or path outside storage root detected: {path}")

        # Prevent targeting the storage root itself
        if resolved == self.base_path:
            raise StorageException("Cannot target storage root directory directly")

        return resolved

    async def save(self, file_path: str, content: BinaryIO) -> str:
        target = self._resolve(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "wb") as f:
            while chunk := content.read(1024 * 1024):  # 1MB chunks
                await f.write(chunk)
        return str(target)

    async def delete(self, file_path: str) -> bool:
        """Safely delete file or directory constrained strictly within storage root."""
        target = self._resolve(file_path)
        if target.is_dir():
            shutil.rmtree(target)
            return True
        elif target.exists():
            target.unlink()
            return True
        return False

    delete_file = delete

    async def delete_video_dir(self, video_id: uuid.UUID) -> bool:
        """
        Strictly deletes the directory for the given video UUID under storage/videos/{video_id}.
        Guarantees isolation to expected video UUID directories only.
        """
        expected_parent = (self.base_path / "videos").resolve()
        expected_parent.mkdir(parents=True, exist_ok=True)
        target_dir = (expected_parent / str(video_id)).resolve()

        try:
            target_dir.relative_to(expected_parent)
        except ValueError:
            raise StorageException(f"Invalid video directory target: {target_dir}")

        if target_dir == expected_parent:
            raise StorageException("Cannot delete videos directory root")

        if target_dir.exists() and target_dir.is_dir():
            shutil.rmtree(target_dir)
            return True
        return False

    async def exists(self, file_path: str) -> bool:
        target = self._resolve(file_path)
        return target.exists()

    async def stat(self, file_path: str) -> Dict[str, Any]:
        target = self._resolve(file_path)
        if not target.exists():
            raise StorageException(f"File not found: {file_path}")
        st = target.stat()
        return {
            "size": st.st_size,
            "created_at": st.st_ctime,
            "modified_at": st.st_mtime,
            "is_dir": target.is_dir(),
        }

    def get_url(self, file_path: str) -> str:
        return f"/stream/{file_path.lstrip('/')}"
