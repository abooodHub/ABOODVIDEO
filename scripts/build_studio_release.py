"""Build a traceable Windows release without installing it or reading a user profile."""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def release_version():
    tree = ast.parse((ROOT / 'studio/__init__.py').read_text(encoding='utf-8-sig'))
    version = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets))
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?', version)
    if not match:
        raise ValueError('Expected major.minor.patch[-rcN] release version')
    numeric = tuple(int(v or 0) for v in match.groups())
    metadata = ast.parse((ROOT / 'studio_version_info.txt').read_text(encoding='utf-8-sig'))
    fixed = next(n for n in ast.walk(metadata) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'FixedFileInfo')
    values = {k.arg: ast.literal_eval(k.value) for k in fixed.keywords if k.arg in {'filevers', 'prodvers'}}
    strings = {ast.literal_eval(n.args[0]): ast.literal_eval(n.args[1]) for n in ast.walk(metadata)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'StringStruct'}
    assert values['filevers'] == values['prodvers'] == numeric, 'Windows numeric version mismatch'
    assert strings['FileVersion'] == '.'.join(map(str, numeric))
    assert strings['ProductVersion'] == version
    return version, numeric


def source_manifest():
    files = []
    for folder in ['studio', 'backend', 'nginx', 'shared']:
        for path in (ROOT / folder).rglob('*'):
            relative = path.relative_to(ROOT)
            if (not path.is_file() or any(part in {'__pycache__', '.pytest_cache', 'node_modules', 'data', 'fonts'} for part in relative.parts)
                    or (path.name not in {'Dockerfile', 'LICENSE', 'COPYRIGHT', 'NOTICE', 'COPYING.GPLv3'} and path.suffix not in {
                        '.py', '.js', '.css', '.html', '.txt', '.ini', '.sh', '.json', '.conf', '.template',
                        '.svg', '.wasm', '.woff', '.woff2', '.map', '.png', '.ico', '.patch', '.LICENSE', '.NOTICE'})):
                continue
            files.append({'path': relative.as_posix(), 'sha256': sha256(path)})
    for name in ['studio.spec', 'studio_version_info.txt', 'installer.iss', 'app.ico', 'docker-compose.yml',
                 'studio_platform.ico', 'scripts/build_studio_release.py', 'scripts/entrypoint.sh', 'scripts/setup_test_owner.py',
                 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'requirements-studio.txt', 'requirements-build.txt',
                 'compose.public.yml', 'deploy/Caddyfile', 'scripts/install_platform.py',
                 'deploy/postgres/Dockerfile', 'deploy/redis/Dockerfile', 'deploy/https/Dockerfile']:
        files.append({'path': name, 'sha256': sha256(ROOT / name)})
    files.sort(key=lambda row: row['path'])
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'files': files, 'sha256': digest}


def validate_payload(folder, version, numeric):
    import pefile
    required = ['ABOODVIDEO Studio.exe', '_internal/base_library.zip', '_internal/python312.dll',
                '_internal/bin/ffmpeg.exe', '_internal/bin/ffprobe.exe',
                '_internal/pythonnet/runtime/Python.Runtime.dll',
                '_internal/webview/lib/Microsoft.Web.WebView2.Core.dll',
                '_internal/webview/lib/Microsoft.Web.WebView2.WinForms.dll',
                '_internal/webview/lib/runtimes/win-x64/native/WebView2Loader.dll',
                '_internal/studio/web/static/js/studio_v3.js', '_internal/studio/web/static/css/studio_v3.css',
                '_internal/studio/web/templates/index_v3.html', '_internal/app.ico']
    required += ['_internal/licenses/ffmpeg/LICENSE', '_internal/licenses/ffmpeg/README.txt',
                 '_internal/licenses/project/LICENSE', '_internal/licenses/project/THIRD_PARTY_NOTICES.md']
    required += ['_internal/shared/i18n/en.json', '_internal/shared/i18n/web/runtime.js',
                 '_internal/shared/i18n/web/catalog.js', '_internal/shared/i18n/web/direction.css']
    # PyInstaller places top-level data in _internal; fail rather than shipping
    # an incomplete one-directory executable or accidentally including a profile.
    for name in required:
        assert (folder / name).is_file(), f'Missing release dependency: {name}'
    for path in folder.rglob('*'):
        if path.is_file():
            assert path.name.casefold() not in {'config.json', 'secrets.dat', 'jobs.db', '.env', 'studio.lock'}
            assert not {'data', '.git'} & set(path.relative_to(folder).parts)
    for relative in ['static/js/studio_v3.js', 'static/css/studio_v3.css', 'templates/index_v3.html']:
        assert sha256(folder / '_internal/studio/web' / relative) == sha256(ROOT / 'studio/web' / relative)
    pe = pefile.PE(str(folder / 'ABOODVIDEO Studio.exe'))
    assert pe.FILE_HEADER.Machine == 0x8664, 'Expected x64 executable'
    info = pe.VS_FIXEDFILEINFO[0]
    actual = (info.FileVersionMS >> 16, info.FileVersionMS & 65535, info.FileVersionLS >> 16, info.FileVersionLS & 65535)
    assert actual == numeric
    strings = {k.decode(): v.decode() for group in pe.FileInfo for entry in group
               if getattr(entry, 'Key', None) == b'StringFileInfo' for table in entry.StringTable for k, v in table.entries.items()}
    assert strings['ProductVersion'] == version
    return {'required_files_verified': required, 'pe_machine': 'x64', 'numeric_version': actual,
            'product_version': strings['ProductVersion'], 'authenticode_signed': bool(pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size),
            'webview2_evergreen_runtime': 'external Windows prerequisite; loader and managed bindings bundled'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New, empty build directory')
    parser.add_argument('--ffmpeg-bin', type=Path, required=True)
    parser.add_argument('--iscc', type=Path)
    parser.add_argument('--webview2-bootstrapper', type=Path, help='Signed Microsoft Evergreen installer')
    args = parser.parse_args()
    if args.iscc:
        if not args.webview2_bootstrapper or not args.webview2_bootstrapper.is_file():
            parser.error('Installer builds require --webview2-bootstrapper')
        # Check the signer without interpolating a shell command or accepting an
        # arbitrary executable as a prerequisite distributed to every user.
        signature_env = dict(os.environ, ABV_BOOTSTRAPPER=str(args.webview2_bootstrapper.resolve()))
        # A PowerShell 7 parent can supply module paths incompatible with the
        # Windows PowerShell used below. Let that host resolve its own modules.
        signature_env['PSModulePath'] = os.path.join(os.environ['WINDIR'], 'System32', 'WindowsPowerShell', 'v1.0', 'Modules')
        verification = subprocess.check_output(['powershell', '-NoProfile', '-NonInteractive', '-Command',
            "$s=Get-AuthenticodeSignature -LiteralPath $env:ABV_BOOTSTRAPPER; "
            "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {exit 1}; "
            "$s.SignerCertificate.Subject"],
            env=signature_env, text=True).strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    version, numeric = release_version()
    before = source_manifest()
    (output / 'SOURCE_MANIFEST.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain=v1'], cwd=ROOT, text=True)
    env = dict(os.environ, ABOODVIDEO_FFMPEG_BIN_DIR=str(args.ffmpeg_bin.resolve()))
    with (output / 'build.log').open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(output / 'portable'),
                        '--workpath', str(output / 'build'), str(ROOT / 'studio.spec')],
                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    portable = output / 'portable/ABOODVIDEO-Studio'
    validation = validate_payload(portable, version, numeric)
    clean_env = dict(os.environ, PATH=os.path.join(os.environ['WINDIR'], 'System32'))
    ffmpeg_version = subprocess.check_output([str(portable / '_internal/bin/ffmpeg.exe'), '-version'],
                                            env=clean_env, cwd=output, text=True).splitlines()[0]
    subprocess.run([str(portable / '_internal/bin/ffprobe.exe'), '-version'], env=clean_env,
                   cwd=output, stdout=subprocess.DEVNULL, check=True)
    assert source_manifest()['sha256'] == before['sha256'], 'Source changed during build; reject artifact'
    release = {'version': version, 'git_head': revision, 'git_working_tree_dirty': bool(status.strip()),
               'source_manifest_sha256': before['sha256'], 'python_build_version': sys.version.split()[0],
               'pyinstaller_version': importlib.metadata.version('pyinstaller'), 'installed_app_modified': False,
               'validation': validation, 'ffmpeg_version': ffmpeg_version,
               'ffmpeg_sha256': sha256(portable / '_internal/bin/ffmpeg.exe')}
    (portable / 'RELEASE.json').write_text(json.dumps(release, indent=2), encoding='utf-8')
    payload = [{'path': p.relative_to(portable).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha256(p)}
               for p in sorted(portable.rglob('*')) if p.is_file()]
    (output / 'PAYLOAD_MANIFEST.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    archive = output / f'ABOODVIDEO-Studio-{version}-windows-x64-portable.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for entry in payload:
            target.write(portable / entry['path'], 'ABOODVIDEO-Studio/' + entry['path'])
        target.write(output / 'PAYLOAD_MANIFEST.json', 'PAYLOAD_MANIFEST.json')
        target.write(output / 'SOURCE_MANIFEST.json', 'SOURCE_MANIFEST.json')
    artifacts = [portable / 'ABOODVIDEO Studio.exe', archive]
    if args.iscc:
        release['webview2_bootstrapper'] = {'sha256': sha256(args.webview2_bootstrapper), 'signer': verification}
        with (output / 'installer-build.log').open('w', encoding='utf-8') as log:
            subprocess.run([str(args.iscc.resolve()), '/Qp', '/DMyAppVersion=' + version,
                            '/DMyAppNumericVersion=' + '.'.join(map(str, numeric)),
                            '/DWebView2Bootstrapper=' + str(args.webview2_bootstrapper.resolve()),
                            '/DBuildOutputDir=' + str(output / 'installer'), '/DStudioDistDir=' + str(portable),
                            str(ROOT / 'installer.iss')], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        artifacts.append(output / f'installer/ABOODVIDEO-Studio-Setup-v{version}.exe')
    hashes = [{'path': str(p.relative_to(output)), 'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in artifacts]
    assert source_manifest()['sha256'] == before['sha256'], 'Source changed during build; reject artifact'
    (output / 'ARTIFACTS.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    (output / 'SHA256SUMS.txt').write_text(''.join(f"{r['sha256']}  {r['path']}\n" for r in hashes), encoding='utf-8')
    (output / 'BUILD_VERIFICATION.json').write_text(json.dumps(release, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output), 'version': version, 'artifacts': hashes}))


if __name__ == '__main__':
    main()
