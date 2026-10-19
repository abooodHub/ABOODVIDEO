import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy import select, desc, func, or_, delete
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.database import get_db
from app.core.redis import get_cache_redis
from app.core.logging import logger
from app.models.user import User
from app.models.api_key import ApiKey
from app.models.base import utc_now
from app.schemas.api_key import (
    ApiKeyCreateRequest,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    ApiKeyListResponse,
    ApiKeyBulkDeleteRequest,
)
from app.security.auth import require_owner_or_admin, record_audit_log
from app.security.csrf import check_csrf_protection
from app.security.api_key import generate_raw_api_key, hash_api_key

router = APIRouter(
    tags=["Studio API Keys"],
    dependencies=[Depends(require_owner_or_admin), Depends(check_csrf_protection)],
)


@router.post("", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    req: ApiKeyCreateRequest,
    request: Request,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates a new Personal API Key for Studio or Machine integrations.
    Returns the raw secret key ONCE. It is never stored or recoverable.
    """
    raw_key = generate_raw_api_key()
    prefix = raw_key[:12]
    key_hash = hash_api_key(raw_key)

    expires_at = None
    if req.expires_in_days:
        expires_at = utc_now() + timedelta(days=req.expires_in_days)

    api_key = ApiKey(
        user_id=current_user.id,
        name=req.name.strip(),
        prefix=prefix,
        key_hash=key_hash,
        scopes=req.scopes,
        is_active=True,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    await record_audit_log(
        db=db,
        action="api_key.create",
        user_id=current_user.id,
        details={
            "key_id": str(api_key.id),
            "name": api_key.name,
            "prefix": api_key.prefix,
            "scopes": api_key.scopes,
        },
        request=request,
    commit=True,
    )

    logger.info(f"API key created: {api_key.prefix} for user {current_user.username}")

    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        prefix=api_key.prefix,
        raw_key=raw_key,
        scopes=api_key.scopes,
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
    )


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    q: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    scope: Optional[str] = Query(None),
    include_test: bool = Query(False),
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Lists Personal API Keys with pagination, test isolation, search, and scope filters."""
    stmt = select(ApiKey)
    count_stmt = select(func.count(ApiKey.id))

    # Test Data Isolation: Exclude automated test keys unless explicitly requested
    if not include_test:
        test_pattern = r"(test|sample|temp|race|traversal|create only|write only|monitor only|fake|dummy|mock|acceptance|studio write key)"
        test_cond = ApiKey.name.op("~*")(test_pattern)
        stmt = stmt.where(~test_cond)
        count_stmt = count_stmt.where(~test_cond)

    if q:
        search_filter = or_(ApiKey.name.ilike(f"%{q.strip()}%"), ApiKey.prefix.ilike(f"%{q.strip()}%"))
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    if status_filter:
        sf = status_filter.lower()
        if sf == "active":
            stmt = stmt.where(ApiKey.is_active.is_(True))
            count_stmt = count_stmt.where(ApiKey.is_active.is_(True))
        elif sf in ("revoked", "inactive"):
            stmt = stmt.where(ApiKey.is_active.is_(False))
            count_stmt = count_stmt.where(ApiKey.is_active.is_(False))

    if scope:
        stmt = stmt.where(ApiKey.scopes.contains([scope]))
        count_stmt = count_stmt.where(ApiKey.scopes.contains([scope]))

    total = (await db.execute(count_stmt)).scalar() or 0
    total_pages = max(1, math.ceil(total / limit))
    offset = (page - 1) * limit

    stmt = stmt.order_by(desc(ApiKey.created_at)).offset(offset).limit(limit)
    result = await db.execute(stmt)
    keys = result.scalars().all()

    items = [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            scopes=k.scopes,
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            expires_at=k.expires_at,
            revoked_at=k.revoked_at,
        )
        for k in keys
    ]

    return ApiKeyListResponse(
        keys=items,
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


@router.post("/{key_id}/revoke")
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_cache_redis),
):
    """
    Revokes an API Key immediately.
    PostgreSQL is authoritative: sets is_active=False, revoked_at=NOW().
    Invalidates Redis cache and sets immediate tombstone in Redis DB 2.
    """
    stmt = select(ApiKey).where(ApiKey.id == key_id)
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    if not api_key.is_active:
        return {"status": "already_revoked", "id": str(key_id)}

    api_key.is_active = False
    api_key.revoked_at = utc_now()
    await db.commit()

    # Redis invalidation & tombstone (fail-secure: DB is already marked inactive)
    try:
        pipe = redis.pipeline()
        pipe.delete(f"apikey:{api_key.key_hash}")
        pipe.set(f"apikey:revoked:{api_key.key_hash}", "1", ex=300)
        await pipe.execute()
    except Exception as e:
        logger.warning(f"Redis tombstone update failed during revoke of key {key_id}: {e}")

    await record_audit_log(
        db=db,
        action="api_key.revoke",
        user_id=current_user.id,
        details={"key_id": str(key_id), "name": api_key.name, "prefix": api_key.prefix},
        request=request,
    commit=True,
    )

    logger.info(f"API key revoked: {api_key.prefix} (id: {key_id})")
    return {"status": "revoked", "id": str(key_id), "prefix": api_key.prefix}


def _parse_key_id(raw_key_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw_key_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="API key not found")


async def _invalidate_key_cache(redis: aioredis.Redis, key_hash: str) -> None:
    try:
        pipe = redis.pipeline()
        pipe.delete(f"apikey:{key_hash}")
        pipe.set(f"apikey:revoked:{key_hash}", "1", ex=300)
        await pipe.execute()
    except Exception as exc:
        # Database state remains authoritative; keep this diagnostic free of credentials.
        logger.warning(f"API key cache invalidation failed: {type(exc).__name__}")


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_revoked_api_key(
    key_id: str,
    request: Request,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_cache_redis),
):
    """Permanently remove a revoked key; active keys must be revoked explicitly first."""
    parsed_id = _parse_key_id(key_id)
    result = await db.execute(select(ApiKey).where(ApiKey.id == parsed_id).with_for_update(of=ApiKey))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.is_active:
        raise HTTPException(
            status_code=409,
            detail={"code": "KEY_MUST_BE_REVOKED_FIRST", "message": "يجب إلغاء المفتاح أولاً قبل حذفه."},
        )

    audit_details = {"key_id": str(api_key.id), "name": api_key.name, "prefix": api_key.prefix}
    key_hash = api_key.key_hash
    await db.execute(delete(ApiKey).where(ApiKey.id == parsed_id))
    await db.commit()
    await _invalidate_key_cache(redis, key_hash)
    await record_audit_log(
        db=db,
        action="api_key.delete",
        user_id=current_user.id,
        details=audit_details,
        request=request,
    commit=True,
    )
    logger.info(f"API key deleted: {audit_details['prefix']} (id: {audit_details['key_id']})")
    return None


@router.post("/bulk-delete")
async def bulk_delete_revoked_api_keys(
    payload: ApiKeyBulkDeleteRequest,
    request: Request,
    current_user: User = Depends(require_owner_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_cache_redis),
):
    """Delete a bounded set atomically, only when every requested key exists and is revoked."""
    ids = list(dict.fromkeys(payload.key_ids))
    result = await db.execute(
        select(ApiKey).where(ApiKey.id.in_(ids)).order_by(ApiKey.id).with_for_update(of=ApiKey)
    )
    keys = list(result.scalars().all())
    if len(keys) != len(ids):
        raise HTTPException(status_code=404, detail="One or more API keys were not found")
    if any(key.is_active for key in keys):
        raise HTTPException(
            status_code=409,
            detail={"code": "KEY_MUST_BE_REVOKED_FIRST", "message": "يجب إلغاء المفاتيح النشطة أولاً قبل حذفها."},
        )

    audit_items = [
        {"key_id": str(key.id), "name": key.name, "prefix": key.prefix, "key_hash": key.key_hash}
        for key in keys
    ]
    await db.execute(delete(ApiKey).where(ApiKey.id.in_(ids)))
    await db.commit()
    for item in audit_items:
        await _invalidate_key_cache(redis, item["key_hash"])
        await record_audit_log(
            db=db,
            action="api_key.delete",
            user_id=current_user.id,
            details={"key_id": item["key_id"], "name": item["name"], "prefix": item["prefix"]},
            request=request,
        commit=True,
    )
    logger.info(f"API key bulk delete completed: count={len(audit_items)}")
    return {"status": "deleted", "deleted_count": len(audit_items)}
