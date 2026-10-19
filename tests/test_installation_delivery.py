"""First-install configuration and security boundaries; disposable files only."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_platform', ROOT / 'scripts/install_platform.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def settings(path):
    return {line.split('=', 1)[0]: line.split('=', 1)[1].strip('"')
            for line in path.read_text().splitlines() if '=' in line and not line.startswith('#')}


def test_new_public_install_has_unique_secrets_secure_cookies_and_internal_services(tmp_path):
    one, two = tmp_path / 'one.env', tmp_path / 'two.env'
    assert installer.configure(one, domain='video.example.com', ready_only=True) == 'https://video.example.com'
    installer.configure(two, domain='video.example.com')
    first, second = settings(one), settings(two)
    for key in ('SECRET_KEY', 'IP_PRIVACY_SALT', 'POSTGRES_PASSWORD'):
        assert len(first[key]) >= 32
        assert first[key] != second[key]
    assert first['SECRET_KEY'] != first['IP_PRIVACY_SALT']
    assert first['SECURITY_MODE'] == 'public'
    assert first['FORCE_HTTPS'] == first['SESSION_COOKIE_SECURE'] == 'true'
    assert first['REDIS_QUEUE_URL'] == 'redis://redis:6379/0'
    assert '@postgres:5432/' in first['DATABASE_URL']
    assert first['ENABLE_DIRECT_IMPORT'] == 'true'
    assert first['ENABLE_TRANSCODING'] == 'false'
    assert second['ENABLE_TRANSCODING'] == 'true'


def test_first_install_never_replaces_existing_configuration(tmp_path):
    path = tmp_path / 'existing.env'
    path.write_bytes(b'preserved private configuration')
    with pytest.raises(FileExistsError):
        installer.configure(path, domain='video.example.com')
    assert path.read_bytes() == b'preserved private configuration'


@pytest.mark.parametrize('domain', ['http://example.com', 'example.com/path', 'example.com:443',
                                     '127.0.0.1', 'user@example.com', 'example.com\nSECRET_KEY=oops', '-bad.example'])
def test_public_domain_rejects_urls_addresses_and_config_injection(domain):
    with pytest.raises(ValueError):
        installer.public_domain(domain)


def test_windows_installer_checks_runtime_before_install_and_preserves_application_identity():
    source = (ROOT / 'installer.iss').read_text(encoding='utf-8-sig')
    assert 'function PrepareToInstall' in source and 'if HasWebView2() then' in source
    assert 'HKCU32' in source and 'HKLM32' in source
    assert "'/silent /install'" in source
    assert 'PrivilegesRequired=lowest' in source
    assert 'D374D82C-8E24-4B42-9AE2-F752C8A9772B' in source


def test_application_container_drops_root_and_linux_capabilities():
    assert 'USER 10001:10001' in (ROOT / 'backend/Dockerfile').read_text()
    compose = (ROOT / 'docker-compose.yml').read_text()
    assert compose.count('user: "10001:10001"') == 3
    assert compose.count('cap_drop: [ALL]') == 3
    assert compose.count('no-new-privileges:true') == 3


def test_studio_dashboard_renders_with_current_template_api():
    from fastapi.testclient import TestClient
    from studio import __version__
    from studio.web.app import app
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/')
        assert response.status_code == 200
        assert f'v{__version__}' in response.text
        assert 'Content-Security-Policy' in response.headers
