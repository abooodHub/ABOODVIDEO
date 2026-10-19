"""Short-lived, authenticated telemetry from local Studio processing."""
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.core.redis import get_cache_redis
from app.models.api_key import ApiKey
from app.security.api_key import check_api_key_rate_limit, require_api_key_scope

router = APIRouter(prefix="/studio/processing", tags=["Studio Processing"])
_ACTIVE = {"PROBING", "READY_TO_ENCODE", "ENCODING", "PACKAGING", "VERIFYING_LOCAL", "UPLOADING", "SERVER_VERIFYING"}
_TERMINAL = {"READY_TO_UPLOAD", "READY", "FAILED", "CANCELLED", "NEEDS_REVIEW"}
PROCESSING_INDEX_KEY = "studio:processing:index"


class ProcessingHeartbeat(BaseModel):
    studio_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    episode_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=240)
    batch_name: Optional[str] = Field(default=None, max_length=160)
    season_number: Optional[int] = Field(default=None, ge=0, le=999)
    episode_number: Optional[int] = Field(default=None, ge=0, le=99999)
    status: str = Field(max_length=32)
    stage: Optional[str] = Field(default=None, max_length=48)
    quality: Optional[str] = Field(default=None, max_length=16)
    progress: float = Field(default=0, ge=0, le=100)
    fps: Optional[float] = Field(default=None, ge=0, le=10000)
    speed: Optional[str] = Field(default=None, max_length=16)
    eta: Optional[str] = Field(default=None, max_length=24)


@router.post("/heartbeat")
async def processing_heartbeat(
    payload: ProcessingHeartbeat,
    api_key: ApiKey = Depends(require_api_key_scope("packages:write")),
    cache: aioredis.Redis = Depends(get_cache_redis),
):
    state = payload.status.upper()
    if state not in _ACTIVE | _TERMINAL:
        raise HTTPException(status_code=422, detail="Unsupported local processing state")
    await check_api_key_rate_limit(api_key.id, "processing_heartbeat", 300, 60, cache)
    record = payload.model_dump()
    record.update(status=state, updated_at=int(time.time()))
    ttl = 86400 if state in _TERMINAL else 180
    key = f"studio:processing:{api_key.id}:{payload.episode_id}"
    pipe = cache.pipeline(transaction=False)
    pipe.set(key, json.dumps(record, ensure_ascii=False, separators=(",", ":")), ex=ttl)
    pipe.zadd(PROCESSING_INDEX_KEY, {key: record["updated_at"]})
    pipe.expire(PROCESSING_INDEX_KEY, 86400)
    await pipe.execute()
    return {"ok": True}


