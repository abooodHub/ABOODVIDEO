"""Pydantic Schemas for Resumable Upload System."""
import uuid
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator
from app.models.video import VideoVisibility


class InitiateUploadRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    category: Optional[str] = Field(None, max_length=100)
    series_name: Optional[str] = Field(None, max_length=150)
    season_number: Optional[int] = Field(None, ge=1, le=9999)
    episode_number: Optional[int] = Field(None, ge=1, le=99999)
    folder_id: Optional[uuid.UUID] = None
    series_id: Optional[uuid.UUID] = None
    season_id: Optional[uuid.UUID] = None
    visibility: VideoVisibility = VideoVisibility.PRIVATE
    filename: str = Field(..., min_length=1, max_length=255)
    file_size_bytes: int = Field(..., gt=0, description="Total size in bytes")
    chunk_size_bytes: int = Field(
        default=5242880,  # 5 MiB default chunk
        ge=1,             # allow small chunks for testing/edge cases
        le=52428800,      # 50 MiB maximum chunk
    )
    expected_sha256: Optional[str] = Field(None, min_length=64, max_length=64)
    mime_type: Optional[str] = Field(None, max_length=100)

    @field_validator("filename")
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        # Strip path traversal attempts and dangerous characters
        cleaned = v.replace("\\", "/").split("/")[-1].strip()
        if not cleaned:
            raise ValueError("Filename cannot be empty or solely path separators")
        return cleaned


class InitiateUploadResponse(BaseModel):
    upload_id: uuid.UUID
    video_id: uuid.UUID
    filename: str
    file_size_bytes: int
    chunk_size_bytes: int
    total_chunks: int
    status: str
    expires_at: datetime


class ChunkUploadResponse(BaseModel):
    upload_id: uuid.UUID
    chunk_index: int
    chunks_received_count: int
    total_chunks: int
    bytes_received: int
    progress_percent: float
    status: str


class UploadStatusResponse(BaseModel):
    upload_id: uuid.UUID
    video_id: uuid.UUID
    status: str
    filename: str
    file_size_bytes: int
    chunk_size_bytes: int
    total_chunks: int
    received_chunks: List[int]
    missing_chunks: List[int]
    bytes_received: int
    progress_percent: float
    expires_at: datetime


class FinalizeUploadResponse(BaseModel):
    video_id: uuid.UUID
    upload_id: uuid.UUID
    title: str
    status: str
    duration_seconds: Optional[float]
    resolution_width: Optional[int]
    resolution_height: Optional[int]
    codec: Optional[str]
    size_bytes: int
