"""Optional trusted external identity assertion verification endpoint."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.redis import get_redis
from app.security.external_identity import ExternalIdentityError, verify_external_identity


router = APIRouter(prefix="/identity/external", tags=["External Identity"])


class ExternalAssertion(BaseModel):
    assertion: str = Field(min_length=1, max_length=8192)


class VerifiedExternalIdentity(BaseModel):
    external_user_id: str
    display_name: str
    plan: str
    entitlements: dict[str, Any]
    issued_at: int
    expires_at: int


@router.post("/verify", response_model=VerifiedExternalIdentity)
async def verify_external_assertion(
    payload: ExternalAssertion,
    replay_store=Depends(get_redis),
):
    if not settings.EXTERNAL_IDENTITY_SECRET or not settings.EXTERNAL_IDENTITY_ISSUER or not settings.EXTERNAL_IDENTITY_AUDIENCE:
        raise HTTPException(status_code=503, detail="External identity integration is not configured")
    try:
        return await verify_external_identity(
            payload.assertion,
            secret=settings.EXTERNAL_IDENTITY_SECRET,
            issuer=settings.EXTERNAL_IDENTITY_ISSUER,
            audience=settings.EXTERNAL_IDENTITY_AUDIENCE,
            replay_store=replay_store,
        )
    except ExternalIdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External identity assertion rejected",
        ) from exc
