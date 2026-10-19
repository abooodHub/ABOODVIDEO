"""Runtime delivery and official admin uploader pacing policy."""
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.setting import SystemSetting
from app.security.auth import require_owner_or_admin
from app.security.csrf import check_csrf_protection

router = APIRouter(
    prefix="/admin/transfer-settings",
    tags=["Admin Transfer Settings"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection)],
)
SETTING_KEY = "transfer_policy"
DEFAULT_POLICY = {"playback_kbps": 0, "upload_kbps": 0, "concurrent_uploads": 2}


class TransferPolicyPayload(BaseModel):
    playback_kbps: int = Field(default=0, ge=0, le=1_000_000)
    upload_kbps: int = Field(default=0, ge=0, le=1_000_000)
    concurrent_uploads: int = Field(default=2, ge=1, le=8)


async def get_transfer_policy(db: AsyncSession) -> dict:
    row = await db.get(SystemSetting, SETTING_KEY)
    if not row:
        return dict(DEFAULT_POLICY)
    try:
        data = json.loads(row.value)
        return TransferPolicyPayload(**data).model_dump()
    except (ValueError, TypeError):
        return dict(DEFAULT_POLICY)


@router.get("")
async def read_transfer_policy(response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return await get_transfer_policy(db)


@router.put("")
async def update_transfer_policy(payload: TransferPolicyPayload, response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    value = json.dumps(payload.model_dump(), separators=(",", ":"))
    row = await db.get(SystemSetting, SETTING_KEY)
    if row is None:
        db.add(SystemSetting(key=SETTING_KEY, value=value,
                             description="Per-response Nginx playback cap and official browser upload pacing"))
    else:
        row.value = value
    await db.commit()
    return payload.model_dump()
