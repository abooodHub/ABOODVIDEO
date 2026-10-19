"""Admin Playback Inspector API — On-demand video and player diagnostics."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.security.auth import require_owner_or_admin
from app.security.csrf import check_csrf_protection
from app.services.playback_inspector import playback_inspector

router = APIRouter(
    prefix="/admin/inspector",
    tags=["Admin Playback Inspector"],
    dependencies=[Depends(require_owner_or_admin)],
)


class InspectRequest(BaseModel):
    identifier: str = Field(..., min_length=1, max_length=128, description="Video UUID or Share ID to inspect")
    probe_network: bool = Field(True, description="Whether to probe HTTP headers and routes")


@router.post("/check", dependencies=[Depends(check_csrf_protection)])
async def inspect_video_playback(
    body: InspectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_owner_or_admin),
):
    """Run comprehensive read-only diagnostics on video readiness, HLS, subtitles, and embed security."""
    base_url = str(request.base_url).rstrip("/")
    result = await playback_inspector.inspect(
        db=db,
        identifier=body.identifier,
        request_base_url=base_url,
        probe_network=body.probe_network,
        current_user=admin,
    )
    return result


@router.get("/check/{identifier}")
async def inspect_video_playback_get(
    identifier: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_owner_or_admin),
):
    """Read-only diagnostics probe via GET parameter."""
    base_url = str(request.base_url).rstrip("/")
    result = await playback_inspector.inspect(
        db=db,
        identifier=identifier,
        request_base_url=base_url,
        probe_network=True,
        current_user=admin,
    )
    return result
