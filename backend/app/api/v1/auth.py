"""Authentication API Endpoints (Admin & Owner Session Management)."""
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.security.hashing import verify_password
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token
from app.security.auth import (
    get_current_user,
    record_audit_log,
    check_login_rate_limit,
    record_failed_login_attempt,
    reset_login_rate_limit,
)
from app.core.network import get_real_client_ip

from app.security.csrf import generate_csrf_token, check_csrf_protection
router = APIRouter(prefix="/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    username_or_email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)


class UserProfileResponse(BaseModel):
    id: uuid.UUID
    username: str
    email: str
    role: str


class LoginResponse(BaseModel):
    success: bool
    csrf_token: str
    user: UserProfileResponse


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate an active local user; API permissions separately gate administration.
    """
    # 1. Rate limiting check
    await check_login_rate_limit(request, username=payload.username_or_email)

    client_ip = get_real_client_ip(request)
    user_agent = request.headers.get("user-agent", "")[:500]

    # 2. Lookup user by username or email
    stmt = select(User).where(
        (User.username == payload.username_or_email)
        | (User.email == payload.username_or_email)
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    # 3. Validate user and credentials
    if (
        not user
        or not verify_password(payload.password, user.hashed_password)
        or not user.is_active
        or user.role == UserRole.BANNED
    ):
        await record_failed_login_attempt(request, username=payload.username_or_email)
        await record_audit_log(
            db=db,
            action="LOGIN_FAILED",
            ip_address=client_ip,
            metadata_json={"username_or_email": payload.username_or_email},
            request=request,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Reset failed rate limits on success
    await reset_login_rate_limit(request, username=payload.username_or_email)

    # 4. Create cryptographically random session token
    raw_token = secrets.token_urlsafe(32)
    token_hashed = hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_MAX_AGE_SECONDS)

    # Replace only the session presented to this login request; preserve other
    # devices while preventing a pre-authentication cookie from surviving rotation.
    prior_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if prior_token:
        prior = await db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_token(prior_token))
        )
        if prior:
            await db.delete(prior)

    user_session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hashed,
        ip_address=client_ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )
    user.last_authenticated_at = datetime.now(timezone.utc)
    db.add(user_session)
    await db.flush()

    # 5. Generate CSRF token
    csrf_token = generate_csrf_token()

    # 6. Set secure session & CSRF cookies
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        secure=settings.SESSION_COOKIE_SECURE,
        path="/",
    )
    response.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=False,  # Client JS reads this to send in X-CSRF-Token header
        samesite=settings.SESSION_COOKIE_SAMESITE,
        secure=settings.SESSION_COOKIE_SECURE,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"

    # 7. Audit log
    await record_audit_log(
        db=db,
        action="LOGIN_SUCCESS",
        user_id=user.id,
        ip_address=client_ip,
        target_entity="user_sessions",
        target_id=str(user_session.id),
        request=request,
        commit=True,
    )

    return LoginResponse(
        success=True,
        csrf_token=csrf_token,
        user=UserProfileResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role.value,
        ),
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf_protection),
):
    """Revoke session and clear authentication cookies."""
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    user_id = None
    if token:
        token_hashed = hash_token(token)
        stmt = select(UserSession).where(UserSession.token_hash == token_hashed)
        result = await db.execute(stmt)
        sess = result.scalar_one_or_none()
        if sess:
            user_id = sess.user_id
            await db.delete(sess)

    # Delete cookies
    response.delete_cookie(key=settings.SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=settings.CSRF_COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"

    client_ip = get_real_client_ip(request)
    await record_audit_log(
        db=db,
        action="LOGOUT",
        user_id=user_id,
        ip_address=client_ip,
        request=request,
        commit=True,
    )

    return {"success": True, "message": "Logged out successfully"}


@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    response: Response,
    current_user: User = Depends(get_current_user),
):
    """Retrieve current authenticated user profile."""
    response.headers["Cache-Control"] = "no-store"
    return UserProfileResponse(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        role=current_user.role.value,
    )
