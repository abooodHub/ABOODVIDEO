"""System diagnostics live in the dashboard; legacy URL retains its guards."""
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_application
from app.api.admin_routes import jinja_env
from test_admin_auth_and_management import admin_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['owner', 'admin', 'user', None])
async def test_system_url_redirects_with_existing_admin_guard(admin_fixture, role):
    async with AsyncClient(transport=ASGITransport(app=create_application()), base_url='http://localhost') as client:
        if role:
            response = await client.post('/api/v1/auth/login', json={
                'username_or_email': admin_fixture[role].username,
                'password': admin_fixture[role+'_pass'],
            })
            assert response.status_code == 200
        response = await client.get('/admin/system', follow_redirects=False)
        assert response.status_code == 302
        if role in {'owner', 'admin'}:
            assert response.headers['location'] == '/admin#system-diagnostics'
            assert response.headers['cache-control'] == 'no-store'
            dashboard = await client.get('/admin')
            assert dashboard.status_code == 200
            assert 'id="system-diagnostics"' in dashboard.text
        else:
            assert response.headers['location'] == '/admin/login'
            assert (await client.get('/api/v1/health/reconciler')).status_code in {401, 403}


def test_diagnostics_are_collapsed_in_dashboard_and_not_a_sidebar_page():
    page = jinja_env.get_template('admin_dashboard.html').render(page_id='dashboard')
    assert '<details id="system-diagnostics" class="dash-diagnostics">' in page
    assert 'href="/admin/system"' not in page
    assert 'id="dashDiagnosticsRefresh"' in page
    script = Path('backend/app/static/js/admin_dashboard.js').read_text(encoding='utf-8')
    assert "window.location.hash !== '#system-diagnostics'" in script
    assert '!diagnostics?.open || document.hidden || diagnosticsLoading' in script
