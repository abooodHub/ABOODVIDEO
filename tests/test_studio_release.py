"""Release metadata checks; no installed profile or application is accessed."""
import importlib.util
from pathlib import Path


def builder():
    path = Path(__file__).resolve().parents[1] / 'scripts/build_studio_release.py'
    spec = importlib.util.spec_from_file_location('studio_release_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_and_application_release_versions_agree():
    import re
    from studio import __version__
    version, numeric = builder().release_version()
    assert version == __version__
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?', __version__)
    assert match
    assert numeric == tuple(int(part or 0) for part in match.groups())


def test_release_manifest_excludes_profiles_and_media():
    manifest = builder().source_manifest()
    assert len(manifest['sha256']) == 64
    names = {row['path'] for row in manifest['files']}
    assert 'studio/core/uploader.py' in names
    assert 'studio/core/batch_runner.py' in names
    assert 'backend/migrations/versions/0019_explicit_test_content.py' in names
    assert 'backend/Dockerfile' in names and 'nginx/nginx.conf' in names
    assert 'nginx/templates/default.conf.template' in names
    assert not any('/data/' in n or n.endswith(('jobs.db', 'secrets.dat', '.env')) for n in names)


def test_installer_has_versioned_name_and_never_auto_closes_existing_studio():
    text = (builder().ROOT / 'installer.iss').read_text(encoding='utf-8-sig')
    assert 'OutputBaseFilename=ABOODVIDEO-Studio-Setup-v{#MyAppVersion}' in text
    assert 'VersionInfoVersion={#MyAppNumericVersion}' in text
    assert 'ArchitecturesAllowed=x64compatible' in text
    assert 'CloseApplications=no' in text and 'RestartApplications=no' in text
