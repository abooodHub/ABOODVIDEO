"""Admin Embed & External CDN Configuration API."""
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.security.auth import require_owner_or_admin
from app.security.csrf import check_csrf_protection
from app.services.embed_settings import (
    get_all_embed_settings,
    save_embed_settings,
)

router = APIRouter(
    prefix="/admin/embed-settings",
    tags=["Admin Embed Settings"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection)],
)


class PauseBannerModel(BaseModel):
    enabled: bool = False
    title: str = Field(default="تابعنا على قنواتنا الرسمية", max_length=100)
    text: str = Field(default="انضم إلينا لمتابعة جميع الحلقات والإصدارات الجديدة بجودة 4K", max_length=255)
    link: str = Field(default="", max_length=255)


class EmbedSettingsResponse(BaseModel):
    allowed_domains: List[str]
    allowed_domains_raw: str
    embed_only_mode: bool
    external_redirect_url: str
    default_color: str
    default_watermark: str
    default_skip_intro: int
    pause_banner: PauseBannerModel


class UpdateEmbedSettingsPayload(BaseModel):
    allowed_domains_raw: Optional[str] = None
    embed_only_mode: Optional[bool] = None
    external_redirect_url: Optional[str] = None
    default_color: Optional[str] = None
    default_watermark: Optional[str] = None
    default_skip_intro: Optional[int] = Field(None, ge=0, le=600)
    pause_banner: Optional[PauseBannerModel] = None


@router.get("", response_model=EmbedSettingsResponse)
async def read_embed_settings(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Read current domain whitelist, embed-only mode, and player appearance defaults."""
    response.headers["Cache-Control"] = "no-store"
    data = await get_all_embed_settings(db)
    return data


@router.put("", response_model=EmbedSettingsResponse)
async def update_embed_settings(
    payload: UpdateEmbedSettingsPayload,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner_or_admin),
):
    """Update embed whitelist, redirect rules, and player defaults."""
    response.headers["Cache-Control"] = "no-store"
    payload_dict = payload.model_dump(exclude_unset=True)
    if "pause_banner" in payload_dict and isinstance(payload_dict["pause_banner"], dict):
        # Keep raw dict for json serialization
        pass
    saved = await save_embed_settings(db, payload_dict)
    return saved
