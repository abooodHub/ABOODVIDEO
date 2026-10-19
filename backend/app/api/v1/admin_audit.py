"""Bounded read-only administrative audit history endpoint."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.audit import AuditLog
from app.security.auth import Permission, require_permission


router = APIRouter(
    prefix="/admin/audit",
    tags=["Admin Audit"],
    dependencies=[Depends(require_permission(Permission.AUDIT_READ))],
)

SAFE_CONTEXT_KEYS = {
    "result", "role", "active", "changed_fields", "reason_code",
    "request_id", "count", "protocol_version", "capability",
}


@router.get("")
async def list_audit_events(
    actor_id: Optional[UUID] = Query(default=None),
    action: Optional[str] = Query(default=None, min_length=1, max_length=100),
    target: Optional[str] = Query(default=None, min_length=1, max_length=100),
    result: Optional[str] = Query(default=None, min_length=1, max_length=30),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    db: AsyncSession = Depends(get_db),
):
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))
    filters = []
    if actor_id:
        filters.append(AuditLog.user_id == actor_id)
    if action:
        filters.append(AuditLog.action == action)
    if target:
        filters.append(AuditLog.target_entity == target)
    if since:
        filters.append(AuditLog.created_at >= since)
    if until:
        filters.append(AuditLog.created_at < until)
    if result:
        filters.append(AuditLog.metadata_json["result"].astext == result)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)
    events = (await db.execute(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id).offset(offset).limit(limit)
    )).scalars().all()
    total = await db.scalar(count_query)
    return {
        "items": [
            {
                "id": str(event.id),
                "actor_id": str(event.user_id) if event.user_id else None,
                "action": event.action,
                "target_type": event.target_entity,
                "target_id": event.target_id,
                "request_id": event.request_id,
                "result": (event.metadata_json or {}).get("result"),
                "created_at": event.created_at.isoformat(),
                "context": {
                    key: value for key, value in (event.metadata_json or {}).items()
                    if key in SAFE_CONTEXT_KEYS and isinstance(value, (str, int, float, bool, type(None)))
                },
            }
            for event in events
        ],
        "limit": limit,
        "offset": offset,
        "total": int(total or 0),
    }
