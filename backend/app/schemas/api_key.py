"""Pydantic schemas for Studio Personal API Keys."""
import uuid
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Descriptive label for key")
    scopes: List[str] = Field(
        default_factory=lambda: ["videos:read", "packages:create", "packages:write", "packages:publish"],
        description="Allowed permission scopes for this key",
    )
    expires_in_days: Optional[int] = Field(
        default=None,
        ge=1,
        le=365,
        description="Optional expiration in days",
    )


class ApiKeyCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    raw_key: str = Field(..., description="Full secret key. Displayed ONCE to user.")
    scopes: List[str]
    created_at: datetime
    expires_at: Optional[datetime] = None


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: List[str]
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class ApiKeyListResponse(BaseModel):
    keys: List[ApiKeyResponse]
    total: int
    page: int = 1
    limit: int = 20
    total_pages: int = 1


class ApiKeyBulkDeleteRequest(BaseModel):
    key_ids: List[uuid.UUID] = Field(..., min_length=1, max_length=50)
