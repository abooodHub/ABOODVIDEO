"""Storage Service Abstract Base Class."""
from abc import ABC, abstractmethod
from typing import BinaryIO, Optional, Dict, Any


class StorageService(ABC):
    """Abstract storage interface for local disk, MinIO, or S3 storage."""

    @abstractmethod
    async def save(self, file_path: str, content: BinaryIO) -> str:
        """Save a file stream to storage."""
        pass

    @abstractmethod
    async def delete(self, file_path: str) -> bool:
        """Delete a file from storage."""
        pass

    @abstractmethod
    async def exists(self, file_path: str) -> bool:
        """Check whether a file exists in storage."""
        pass

    @abstractmethod
    async def stat(self, file_path: str) -> Dict[str, Any]:
        """Retrieve file metadata (size, timestamps)."""
        pass

    @abstractmethod
    def get_url(self, file_path: str) -> str:
        """Generate relative or signed URL for accessing the file."""
        pass
