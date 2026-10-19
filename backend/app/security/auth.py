"""Authentication Dependencies, Role Enforcers, Rate Limiting, and Audit Helpers."""
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_cache_client
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.models.audit import AuditLog
from app.security.tokens import hash_token
from app.core.exceptions import AboodVideoException, AuthenticationException, PermissionDeniedException
from app.core.network import get_real_client_ip


from enum import Enum
async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Extract and authenticate the user from Session Cookie, Bearer Token,
    or test-mode authorization header.
    """
    # 1. Test/Dev bypass via X-User-ID (only in non-production)
    if settings.ENVIRONMENT in ("test", "development"):
        dev_user_id = request.headers.get("X-User-ID")
        if dev_user_id:
            try:
                uid = uuid.UUID(dev_user_id)
                stmt = select(User).where(User.id == uid)
                result = await db.execute(stmt)
                user = result.scalar_one_or_none()
                if user and user.is_active:
                    return user
            except Exception:
                pass

    # 2. Extract token from Header or Cookie
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif settings.SESSION_COOKIE_NAME in request.cookies:
        token = request.cookies[settings.SESSION_COOKIE_NAME]

    if not token:
        raise AuthenticationException("Authentication required: missing token or session")

    # 3. Hash and lookup session in PostgreSQL
    token_hashed = hash_token(token)
    stmt = (
        select(UserSession)
        .options(selectinload(UserSession.user))
        .where(
            UserSession.token_hash == token_hashed,
            UserSession.expires_at > datetime.now(timezone.utc),
        )
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if not session or not session.user or not session.user.is_active:
        raise AuthenticationException("Session is invalid, expired, or user is inactive")

    if session.user.role == UserRole.BANNED:
        raise PermissionDeniedException("User account is banned")

    return session.user


async def get_optional_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Dependency returning current user if authenticated, or None if anonymous."""
    try:
        return await get_current_user(request=request, db=db)
    except Exception:
        return None


def require_role(*roles: UserRole):
    """Dependency factory restricting access to specified roles."""
    async def role_checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles and user.role != UserRole.OWNER:
            raise PermissionDeniedException(f"Requires one of roles: {[r.value for r in roles]}")
        return user
    return role_checker





async def record_audit_log(
    db: AsyncSession,
    action: str,
    user_id: Optional[uuid.UUID] = None,
    ip_address: Optional[str] = None,
    target_entity: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
    details: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    commit: bool = False,
) -> None:
    """Append an audit row to the caller's transaction; never suppress failure."""
    if details is not None and metadata_json is None:
        metadata_json = details
    if request is not None and ip_address is None:
        ip_address = get_real_client_ip(request)
    context = dict(metadata_json or {})
    if request is not None:
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            context["request_id"] = request_id
    # Exclude known credentials and bearer material even if a call site passes
    # an overly broad context dictionary. Values are kept bounded and scalar.
    forbidden = {"password", "token", "secret", "authorization", "cookie", "api_key", "raw_key", "session_secret"}
    safe_context = {
        str(key)[:64]: value[:256] if isinstance(value, str) else value
        for key, value in context.items()
        if str(key).lower() not in forbidden
        and not any(part in str(key).lower() for part in ("password", "token", "secret", "authorization", "cookie"))
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    log_entry = AuditLog(
        id=uuid.uuid4(),
        user_id=user_id,
        action=action[:100],
        target_entity=target_entity[:100] if target_entity else None,
        target_id=str(target_id)[:100] if target_id else None,
        request_id=(getattr(request.state, "request_id", None)[:64] if request is not None and getattr(request.state, "request_id", None) else None),
        ip_address=ip_address,
        metadata_json=safe_context,
        created_at=datetime.now(timezone.utc),
    )
    db.add(log_entry)
    await db.flush()
    if commit:
        await db.commit()


async def check_login_rate_limit(request: Request, username: Optional[str] = None) -> None:
    """
    Check failed login attempt rate limiting by client IP and username.
    Protects against single-target brute-force and wide credential stuffing.
    """
    client_ip = get_real_client_ip(request)
    normalized_user = username.strip().lower() if username else None

    try:
        redis_client = get_cache_client()

        # 1. IP-level limit across all accounts (defense against distributed credential spraying from an IP)
        ip_key = f"rate_limit:login:ip:{client_ip}"
        ip_attempts = await redis_client.get(ip_key)
        if ip_attempts and int(ip_attempts) >= settings.RATE_LIMIT_LOGIN_IP_ATTEMPTS:
            retry_after = max(await redis_client.ttl(ip_key), 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts from this network. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )

        if normalized_user:
            # 2. Specific IP + user combination (defense against targeted brute-force)
            ip_user_key = f"rate_limit:login:ip_user:{client_ip}:{normalized_user}"
            ip_user_attempts = await redis_client.get(ip_user_key)
            if ip_user_attempts and int(ip_user_attempts) >= settings.RATE_LIMIT_LOGIN_ATTEMPTS:
                retry_after = max(await redis_client.ttl(ip_user_key), 1)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts. Please try again later.",
                    headers={"Retry-After": str(retry_after)},
                )

            # 3. User account across all IPs (defense against botnet distributed attack on one user)
            user_key = f"rate_limit:login:user:{normalized_user}"
            user_attempts = await redis_client.get(user_key)
            if user_attempts and int(user_attempts) >= settings.RATE_LIMIT_LOGIN_USER_ATTEMPTS:
                retry_after = max(await redis_client.ttl(user_key), 1)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts for this account. Please try again later.",
                    headers={"Retry-After": str(retry_after)},
                )
        else:
            # Fallback if no username provided: check IP against base threshold
            if ip_attempts and int(ip_attempts) >= settings.RATE_LIMIT_LOGIN_ATTEMPTS:
                retry_after = max(await redis_client.ttl(ip_key), 1)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts. Please try again later.",
                    headers={"Retry-After": str(retry_after)},
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise AboodVideoException(
            "Authentication service is temporarily unavailable. Please try again later.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_code="SYSTEM_UNAVAILABLE",
        ) from exc


async def record_failed_login_attempt(request: Request, username: Optional[str] = None) -> None:
    """Increment failed login attempts counter in Redis."""
    client_ip = get_real_client_ip(request)
    normalized_user = username.strip().lower() if username else None

    try:
        redis_client = get_cache_client()
        pipe = redis_client.pipeline()

        ip_key = f"rate_limit:login:ip:{client_ip}"
        pipe.incr(ip_key)
        pipe.expire(ip_key, settings.RATE_LIMIT_LOGIN_WINDOW_SECONDS)

        if normalized_user:
            ip_user_key = f"rate_limit:login:ip_user:{client_ip}:{normalized_user}"
            pipe.incr(ip_user_key)
            pipe.expire(ip_user_key, settings.RATE_LIMIT_LOGIN_WINDOW_SECONDS)

            user_key = f"rate_limit:login:user:{normalized_user}"
            pipe.incr(user_key)
            pipe.expire(user_key, settings.RATE_LIMIT_LOGIN_WINDOW_SECONDS)

        await pipe.execute()
    except Exception as exc:
        raise AboodVideoException(
            "Authentication service is temporarily unavailable. Please try again later.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_code="SYSTEM_UNAVAILABLE",
        ) from exc


async def reset_login_rate_limit(request: Request, username: Optional[str] = None) -> None:
    """Reset failed login attempts counter on successful login."""
    client_ip = get_real_client_ip(request)
    normalized_user = username.strip().lower() if username else None

    try:
        redis_client = get_cache_client()
        keys = [f"rate_limit:login:ip:{client_ip}"]
        if normalized_user:
            keys.append(f"rate_limit:login:ip_user:{client_ip}:{normalized_user}")
            keys.append(f"rate_limit:login:user:{normalized_user}")
        await redis_client.delete(*keys)
    except Exception as exc:
        raise AboodVideoException(
            "Authentication service is temporarily unavailable. Please try again later.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_code="SYSTEM_UNAVAILABLE",
        ) from exc


class Permission(str, Enum):
    """Server-enforced capabilities mapped onto the existing role model."""

    LIBRARY_READ = "library.read"
    LIBRARY_WRITE = "library.write"
    MEDIA_UPLOAD = "media.upload"
    MEDIA_PROCESS = "media.process"
    MEDIA_DELETE = "media.delete"
    USERS_READ = "users.read"
    USERS_MANAGE = "users.manage"
    AUDIT_READ = "audit.read"
    ANALYTICS_READ = "analytics.read"
    INSPECTOR_READ = "inspector.read"
    SETTINGS_READ = "settings.read"
    SETTINGS_WRITE = "settings.write"
    SHARES_MANAGE = "shares.manage"
    STORAGE_MANAGE = "storage.manage"
    SYSTEM_READ = "system.read"
    SYSTEM_MANAGE = "system.manage"



_ROLE_PERMISSIONS = {
    UserRole.OWNER: frozenset(Permission),
    UserRole.ADMIN: frozenset({
        Permission.LIBRARY_READ, Permission.LIBRARY_WRITE,
        Permission.MEDIA_UPLOAD, Permission.MEDIA_PROCESS, Permission.MEDIA_DELETE,
        Permission.USERS_READ, Permission.ANALYTICS_READ, Permission.INSPECTOR_READ,
        Permission.AUDIT_READ,
        Permission.SETTINGS_READ, Permission.SETTINGS_WRITE, Permission.SHARES_MANAGE,
        Permission.STORAGE_MANAGE, Permission.SYSTEM_READ,
    }),
    UserRole.VIP: frozenset({Permission.LIBRARY_READ}),
    UserRole.USER: frozenset({Permission.LIBRARY_READ}),
    UserRole.BANNED: frozenset(),
}



def user_has_permission(user: User, permission: Permission | str) -> bool:
    """Return whether an active user has the named server-side capability."""
    if not user.is_active or user.role == UserRole.BANNED:
        return False
    try:
        required = Permission(permission)
    except (TypeError, ValueError):
        return False
    return required in _ROLE_PERMISSIONS.get(user.role, frozenset())



def require_permission(permission: Permission | str):
    """FastAPI dependency enforcing a capability at the API boundary."""
    async def permission_checker(user: User = Depends(get_current_user)) -> User:
        if not user_has_permission(user, permission):
            required = permission.value if isinstance(permission, Permission) else str(permission)
            raise PermissionDeniedException(f"Permission required: {required}")
        return user
    return permission_checker


require_owner_or_admin = require_permission(Permission.SYSTEM_READ)
