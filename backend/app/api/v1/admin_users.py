"""Bounded administration API for local user lifecycle and roles."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.audit import AuditLog
from app.models.session import UserSession
from app.models.user import User, UserRole
from app.security.auth import Permission, require_permission
from app.security.csrf import check_csrf_protection
from app.security.hashing import hash_password


router = APIRouter(prefix="/admin/users", tags=["Admin Users"])


class UserCreate(BaseModel):
    email: str = Field(min_length=5, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: UserRole = UserRole.USER


class UserRoleUpdate(BaseModel):
    role: UserRole


class UserStatusUpdate(BaseModel):
    active: bool


def _user_json(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "username": user.username,
        "role": user.role.value,
        "active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "vip_until": user.vip_until.isoformat() if user.vip_until else None,
    }


def _audit(actor: User, action: str, target: User, result: str = "success") -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        user_id=actor.id,
        action=action,
        target_entity="user",
        target_id=str(target.id),
        metadata_json={"result": result, "role": target.role.value, "active": target.is_active},
        created_at=datetime.now(timezone.utc),
    )


@router.get("", dependencies=[Depends(require_permission(Permission.USERS_READ))])
async def list_users(
    q: Optional[str] = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    db: AsyncSession = Depends(get_db),
):
    query = select(User)
    count_query = select(func.count(User.id))
    if q:
        needle = q.replace("%", r"\%").replace("_", r"\_")
        predicate = User.email.ilike(f"%{needle}%") | User.username.ilike(f"%{needle}%")
        query = query.where(predicate)
        count_query = count_query.where(predicate)
    rows = (await db.execute(query.order_by(User.created_at.desc(), User.id).offset(offset).limit(limit))).scalars().all()
    total = await db.scalar(count_query)
    return {"items": [_user_json(row) for row in rows], "limit": limit, "offset": offset, "total": int(total or 0)}


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[
    Depends(require_permission(Permission.USERS_MANAGE)), Depends(check_csrf_protection),
])
async def create_user(
    payload: UserCreate,
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    email = str(payload.email).strip().casefold()
    username = payload.username.strip()
    if payload.role == UserRole.BANNED:
        raise HTTPException(status_code=422, detail="New accounts cannot be created as banned")
    user = User(
        id=uuid.uuid4(), email=email, username=username,
        hashed_password=hash_password(payload.password), role=payload.role, is_active=True,
    )
    db.add(user)
    try:
        await db.flush()
        db.add(_audit(actor, "USER_CREATED", user))
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email or username already exists") from exc
    await db.refresh(user)
    return _user_json(user)


@router.patch("/{user_id}/role", dependencies=[
    Depends(require_permission(Permission.USERS_MANAGE)), Depends(check_csrf_protection),
])
async def update_user_role(
    user_id: uuid.UUID,
    payload: UserRoleUpdate,
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    # Serialize owner removal/promotions to prevent two concurrent requests
    # from both observing and removing the last active owner.
    await db.execute(text("SELECT pg_advisory_xact_lock(207341, 4)"))
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == actor.id and target.role != payload.role:
        raise HTTPException(status_code=409, detail="An actor cannot change their own role")
    if target.role == UserRole.OWNER and target.is_active and payload.role != UserRole.OWNER:
        owners = await db.scalar(select(func.count(User.id)).where(User.role == UserRole.OWNER, User.is_active.is_(True)))
        if int(owners or 0) <= 1:
            raise HTTPException(status_code=409, detail="The last active owner cannot be demoted")
    target.role = payload.role
    if payload.role == UserRole.BANNED:
        target.is_active = False
    db.add(_audit(actor, "USER_ROLE_CHANGED", target))
    await db.commit()
    await db.refresh(target)
    return _user_json(target)


@router.patch("/{user_id}/status", dependencies=[
    Depends(require_permission(Permission.USERS_MANAGE)), Depends(check_csrf_protection),
])
async def update_user_status(
    user_id: uuid.UUID,
    payload: UserStatusUpdate,
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(text("SELECT pg_advisory_xact_lock(207341, 4)"))
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == actor.id and not payload.active:
        raise HTTPException(status_code=409, detail="An actor cannot disable their own account")
    if target.role == UserRole.BANNED and payload.active:
        raise HTTPException(status_code=409, detail="A banned account must be assigned an allowed role first")
    if target.role == UserRole.OWNER and target.is_active and not payload.active:
        owners = await db.scalar(select(func.count(User.id)).where(User.role == UserRole.OWNER, User.is_active.is_(True)))
        if int(owners or 0) <= 1:
            raise HTTPException(status_code=409, detail="The last active owner cannot be disabled")
    target.is_active = payload.active
    if not payload.active:
        await db.execute(UserSession.__table__.delete().where(UserSession.user_id == target.id))
    db.add(_audit(actor, "USER_ENABLED" if payload.active else "USER_DISABLED", target))
    await db.commit()
    await db.refresh(target)
    return _user_json(target)
