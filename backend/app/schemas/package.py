"""Pydantic schemas for Pre-Encoded Studio Packages and Manifest Validation."""
import enum
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


class QualityLabelEnum(str, enum.Enum):
    P360 = "360p"
    P480 = "480p"
    P720 = "720p"
    P1080 = "1080p"
    P1440 = "1440p"
    P2160 = "2160p"


class PackageRenditionDeclaration(BaseModel):
    quality_label: QualityLabelEnum = Field(..., description="Standard rendition quality ladder label")
    width: int = Field(..., ge=160, le=3840)
    height: int = Field(..., ge=120, le=2160)
    video_bitrate_bps: int = Field(..., ge=100000, le=50000000)
    audio_bitrate_bps: int = Field(default=128000, ge=32000, le=1000000)
    video_codec: Optional[str] = Field(default=None, max_length=50)
    audio_codec: Optional[str] = Field(default=None, max_length=50)
    playlist_path: str = Field(..., max_length=255)
    segment_count: int = Field(..., ge=1, le=5000)


class PackageFontDeclaration(BaseModel):
    family_name: str = Field(..., min_length=1, max_length=100)
    filename: str = Field(..., min_length=1, max_length=255)
    path: str = Field(..., max_length=255)
    size_bytes: int = Field(..., ge=1)
    sha256: str = Field(..., min_length=64, max_length=64)
    source: Optional[str] = Field(default=None, max_length=50)


class PackageSubtitleDeclaration(BaseModel):
    language_code: str = Field(..., min_length=2, max_length=10)
    language_name: str = Field(..., min_length=1, max_length=50)
    label: str = Field(..., min_length=1, max_length=100)
    is_default: bool = False
    path: str = Field(..., max_length=255)
    sha256: str = Field(..., min_length=64, max_length=64)
    format: Optional[str] = Field(default=None, max_length=16)
    fonts: Optional[List[str]] = Field(default_factory=list)


class PackageFileDeclaration(BaseModel):
    path: str = Field(..., min_length=1, max_length=255)
    size_bytes: int = Field(..., ge=1, le=524288000)  # Max 500MB per file
    sha256: str = Field(..., min_length=64, max_length=64)
    media_type: Optional[str] = Field(default=None, max_length=50)


class PackageVideoMetadata(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    duration_seconds: float = Field(..., ge=0.1, le=14400.0)
    source_checksum: Optional[str] = Field(default=None, max_length=128)


class PackageManifest(BaseModel):
    manifest_version: str = Field(default="1.0", max_length=10)
    generator: Optional[str] = Field(default=None, max_length=100)
    video: PackageVideoMetadata
    renditions: List[PackageRenditionDeclaration] = Field(..., min_length=1, max_length=6)
    subtitles: List[PackageSubtitleDeclaration] = Field(default_factory=list, max_length=10)
    fonts: Optional[List[PackageFontDeclaration]] = Field(default_factory=list, max_length=200)
    files: List[PackageFileDeclaration] = Field(..., min_length=1, max_length=20000)


class PackageInitRequest(BaseModel):
    idempotency_key: Optional[str] = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    video_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional existing video_id to attach this generation to. If null, a new video is created.",
    )
    folder_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Authoritative destination folder ID.",
    )
    series_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Authoritative destination series ID.",
    )
    season_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Authoritative destination season ID.",
    )
    episode_number: Optional[int] = Field(
        default=None,
        ge=0,
        description="Authoritative episode number within season.",
    )
    manifest: PackageManifest


class PackageDeclaredFileItem(BaseModel):
    file_id: uuid.UUID
    relative_path: str
    expected_size: int
    expected_sha256: str
    media_type: str
    status: str
    received_bytes: int = 0


class PackageInitResponse(BaseModel):
    session_id: uuid.UUID
    video_id: uuid.UUID
    status: str
    total_files: int
    total_bytes: int
    files: List[PackageDeclaredFileItem]


class PackageSessionResponse(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    status: str
    total_files: int
    total_bytes: int
    completed_bytes: int
    progress_pct: float
    verification_job_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class PackageFileListResponse(BaseModel):
    session_id: uuid.UUID
    status: str
    files: List[PackageDeclaredFileItem]
    pending_count: int
    completed_count: int


class PackageFinalizeResponse(BaseModel):
    package_id: uuid.UUID
    status: str
    verification_job_id: Optional[str] = None
    message: str
