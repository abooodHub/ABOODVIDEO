"""Admin Web UI Routes — Jinja2 templating with server-side session guard."""
import os
from pathlib import Path
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import jinja2

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.security.tokens import hash_token

from app.security.auth import Permission, user_has_permission
router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
)


async def get_optional_admin_user(request: Request, db: AsyncSession):
    """Return the browser request's active Owner/Admin user, or None."""
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None

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
    sess = result.scalar_one_or_none()
    if sess and sess.user and sess.user.is_active and sess.user.role in (UserRole.OWNER, UserRole.ADMIN):
        return sess.user
    return None


def render_admin_template(template_name: str, **ctx) -> HTMLResponse:
    """Render a Jinja2 admin template with no-cache headers."""
    page_titles = {
        "dashboard": "نظرة عامة", "content": "مكتبة المحتوى", "upload": "رفع الملفات",
        "processing": "مركز المعالجة", "shares": "المشاركة والتضمين", "series-detail": "تفاصيل المسلسل",
        "video-detail": "تفاصيل الفيديو", "subtitles": "الترجمات", "api-keys": "مفاتيح API",
        "settings": "الإعدادات والأدوات", "inspector": "فاحص المشاهدة", "analytics": "التحليلات",
        "users": "المستخدمون", "audit": "سجل التدقيق", "storage": "التخزين", "system": "صحة الخدمات",
    }
    ctx.setdefault("page_title", page_titles.get(ctx.get("page_id"), "إدارة المنصة"))
    ctx.setdefault("embed_mode", False)
    ctx.setdefault("server_transcoding_enabled", settings.ENABLE_TRANSCODING)
    ctx.setdefault("csrf_cookie_name", settings.CSRF_COOKIE_NAME)
    try:
        tmpl = jinja_env.get_template(template_name)
        content = tmpl.render(**ctx)
    except jinja2.TemplateNotFound:
        # Fallback: try plain HTML read
        file_path = TEMPLATES_DIR / template_name
        if not file_path.exists():
            return HTMLResponse("Template not found", status_code=500)
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return HTMLResponse(f"Template error: {e}", status_code=500)
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


def render_template(filename: str) -> HTMLResponse:
    """Legacy plain-HTML render (used by pages not yet migrated)."""
    return render_admin_template(filename)


@router.get("/admin/login")
async def admin_login_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Login page. If already authenticated, redirect to /admin."""
    is_authenticated = await get_optional_admin_user(request, db)
    if is_authenticated:
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_login.html")


@router.get("/admin")
async def admin_dashboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Dashboard with server-side auth guard."""
    user = await get_optional_admin_user(request, db)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    role_label = "المدير العام" if user.role == UserRole.OWNER else "مدير النظام"
    return render_admin_template(
        "admin_dashboard.html",
        active_nav="dashboard",
        page_id="dashboard",
        admin_name=user.username or "المدير",
        admin_role=role_label,
    )


@router.get("/admin/upload")
async def admin_upload_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Authenticated Owner/Admin Resumable Upload interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    template = "admin_ready_upload.html" if settings.ENABLE_DIRECT_IMPORT and not settings.ENABLE_TRANSCODING else "admin_upload.html"
    return render_admin_template(template, active_nav="upload", page_id="upload",
                                 embed_mode=request.query_params.get("embed") == "1")


@router.get("/admin/content")
@router.get("/admin/videos")
@router.get("/admin/drive")
@router.get("/admin/library")
async def admin_videos_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Content Library interactive filesystem interface."""
    user = await get_optional_admin_user(request, db)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    role_label = "المدير العام" if user.role == UserRole.OWNER else "مدير النظام"
    return render_admin_template(
        "drive.html",
        active_nav="content",
        page_id="content",
        admin_name=user.username or "المدير",
        admin_role=role_label,
    )


@router.get("/admin/processing")
async def admin_processing_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Transcoding & Queue Management interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_processing.html", active_nav="processing", page_id="processing")


@router.get("/admin/shares")
async def admin_shares_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Shares management interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_shares.html", active_nav="content", page_id="shares")


@router.get("/admin/series")
async def admin_series_page():
    """Redirect legacy Series route directly to unified Content Library."""
    return RedirectResponse(url="/admin/videos", status_code=status.HTTP_302_FOUND)


@router.get("/admin/series/detail")
async def admin_series_detail_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Series detail interface (series name via ?s= query param)."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_series_detail.html", active_nav="content", page_id="series-detail")


@router.get("/admin/videos/detail")
async def admin_video_detail_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Video Details interface (video id via ?v= query param)."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_video_detail.html", active_nav="content", page_id="video-detail")


@router.get("/admin/subtitles")
async def admin_subtitles_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin global Subtitles management interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_subtitles.html", active_nav="content", page_id="subtitles")


@router.get("/admin/api-keys")
async def admin_api_keys_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin API Keys management interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_api_keys.html", active_nav="settings", page_id="api-keys")


@router.get("/admin/settings")
async def admin_settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin System Settings interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template("admin_settings.html", active_nav="settings", page_id="settings", cfg={})


@router.get("/admin/inspector")
async def admin_inspector_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Playback & Quality Inspector interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template(
        "admin_inspector.html",
        active_nav="settings",
        page_id="inspector",
        admin_name=is_authenticated.username or "المدير",
    )


@router.get("/admin/analytics")
async def admin_analytics_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve Admin Analytics, Views, Qualities & Traffic interface."""
    is_authenticated = await get_optional_admin_user(request, db)
    if not is_authenticated:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    return render_admin_template(
        "admin_analytics.html",
        active_nav="analytics",
        page_id="analytics",
        admin_name=is_authenticated.username or "المدير",
    )


async def _render_platform_admin_page(
    request: Request,
    db: AsyncSession,
    *,
    permission: Permission,
    template: str,
    active_nav: str,
    page_id: str,
):
    user = await get_optional_admin_user(request, db)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    if not user_has_permission(user, permission):
        return HTMLResponse("غير مصرح لك بعرض هذه الصفحة.", status_code=status.HTTP_403_FORBIDDEN)
    role_label = "المدير العام" if user.role == UserRole.OWNER else "مدير النظام"
    return render_admin_template(
        template,
        active_nav=active_nav,
        page_id=page_id,
        admin_name=user.username or "المدير",
        admin_role=role_label,
        admin_user_id=str(user.id),
        can_manage_users=user_has_permission(user, Permission.USERS_MANAGE),
    )



@router.get("/admin/users")
async def admin_users_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _render_platform_admin_page(
        request, db, permission=Permission.USERS_READ, template="admin_users.html",
        active_nav="settings", page_id="users",
    )



@router.get("/admin/audit")
async def admin_audit_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _render_platform_admin_page(
        request, db, permission=Permission.AUDIT_READ, template="admin_audit.html",
        active_nav="settings", page_id="audit",
    )



@router.get("/admin/storage")
async def admin_storage_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _render_platform_admin_page(
        request, db, permission=Permission.SYSTEM_READ, template="admin_storage.html",
        active_nav="settings", page_id="storage",
    )



@router.get("/admin/system")
async def admin_system_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Keep the legacy URL pointing at diagnostics in the dashboard."""
    user = await get_optional_admin_user(request, db)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    if not user_has_permission(user, Permission.SYSTEM_READ):
        return HTMLResponse("غير مصرح لك بعرض هذه الصفحة.", status_code=status.HTTP_403_FORBIDDEN)
    return RedirectResponse(
        url="/admin#system-diagnostics", status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )
