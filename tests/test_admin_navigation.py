"""Regression checks for the shared admin navigation and its wired routes."""

import re
from pathlib import Path
from urllib.parse import urlsplit
from fastapi.testclient import TestClient

from app.api.admin_routes import TEMPLATES_DIR, jinja_env
from app.core.config import settings
from app.main import app


def test_admin_pages_use_one_shared_sidebar_partial():
    for template_name in ("admin_base.html", "drive.html"):
        rendered = jinja_env.get_template(template_name).render(page_id="content")
        assert rendered.count('id="adminSidebar"') == 1
        assert rendered.count('aria-label="التنقل الرئيسي"') == 1

    partial = jinja_env.get_template("admin_sidebar.html").render(
        active_nav="analytics", page_id="analytics"
    )
    assert 'href="/admin/analytics"' in partial
    assert 'aria-current="page"' in partial


def test_admin_sidebar_links_resolve_to_registered_pages():
    partial = jinja_env.get_template("admin_sidebar.html").render(page_id="content")
    hrefs = {urlsplit(href).path for href in re.findall(r'href="(/admin[^\"]*)', partial)}
    assert hrefs
    with TestClient(app, base_url="http://localhost") as client:
        for href in hrefs:
            response = client.get(href, follow_redirects=False)
            assert response.status_code == 302, href
            assert response.headers["location"] == "/admin/login", href
    assert {
        "/admin/analytics",
        "/admin/api-keys",
        "/admin/videos",
        "/admin/upload",
    } <= set(hrefs)
    # Inspection/subtitles belong to a selected video, not large sidebar sections.
    assert not {"/admin/inspector", "/admin/subtitles"} & hrefs


def test_content_library_does_not_restore_upload_call_to_actions():
    source = (TEMPLATES_DIR / "drive.html").read_text(encoding="utf-8")
    for obsolete_id in ("btnQuickUpload", "emptyCtxUploadVideo", "menuItemUploadVideo"):
        assert obsolete_id not in source
    assert 'class="btn btn-primary">رفع محتوى جديد</a>' not in source


def test_inspector_reads_the_csrf_cookie_configured_by_backend():
    source = Path("backend/app/static/js/admin_inspector.js").read_text(encoding="utf-8")
    assert f"'{settings.CSRF_COOKIE_NAME}'" in source
    assert "dataset.csrfCookieName" in source
    assert "X-CSRF-Token" in source


def test_share_revoke_sends_csrf_and_uses_only_registered_api_routes():
    source = Path("backend/app/static/js/admin_shares.js").read_text(encoding="utf-8")
    assert f"'{settings.CSRF_COOKIE_NAME}'" in source
    assert "dataset.csrfCookieName" in source
    assert "'X-CSRF-Token': csrfToken()" in source
    assert "/api/v1/drive/shares" in source
    assert "/api/v1/admin/shares" not in source


def test_share_ui_handles_all_supported_target_types_and_safe_status_classes():
    source = Path("backend/app/static/js/admin_shares.js").read_text(encoding="utf-8")
    assert "if (t.includes('series')) return window.AVi18n.t('مسلسل')" in source
    assert "if (t.includes('season')) return window.AVi18n.t('موسم')" in source
    assert "if (share.status === 'expired') return 'expired'" in source
    assert "if (share.status) return share.status" not in source


def test_share_ui_escapes_template_literal_delimiters_in_api_values():
    source = Path("backend/app/static/js/admin_shares.js").read_text(encoding="utf-8")
    assert ".replace(/`/g, '&#96;')" in source
    assert ".replace(/\\$/g, '&#36;')" in source


def test_failed_share_revoke_keeps_confirmation_available_for_retry():
    source = Path("backend/app/static/js/admin_shares.js").read_text(encoding="utf-8")
    failure_handler = source.split("} catch (err) {", 1)[1].split("\n    }", 1)[0]
    assert "revokeConfirm.disabled = false" in failure_handler
    assert "closeRevokeModal()" not in failure_handler


def test_fixed_admin_scripts_have_cache_busting_versions():
    shares_template = (TEMPLATES_DIR / "admin_shares.html").read_text(encoding="utf-8")
    inspector_template = (TEMPLATES_DIR / "admin_video_detail.html").read_text(encoding="utf-8")
    import re
    assert re.search(r"admin_shares\.js\?v=[A-Za-z0-9_-]+", shares_template)
    assert re.search(r"admin_inspector\.js\?v=[A-Za-z0-9_-]+", inspector_template)


def test_shared_platform_polish_is_loaded_by_admin_shell_and_library():
    stylesheet = r"/static/css/platform-polish-v1\.css\?v=[A-Za-z0-9_-]+"
    for template_name in ("admin_base.html", "drive.html"):
        rendered = jinja_env.get_template(template_name).render(page_id="content")
        assert re.search(stylesheet, rendered)
    assert Path("backend/app/static/css/platform-polish-v1.css").is_file()
