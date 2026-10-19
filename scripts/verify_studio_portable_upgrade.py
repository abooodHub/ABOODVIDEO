"""Verify an actual ZIP's portable upgrade/rollback using a disposable profile.

Does not run an installer, touch installed software or connect to a backend.
The optional native review gate leaves the new executable open for UI testing.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import winreg
import zipfile

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
APP_ID = '{D374D82C-8E24-4B42-9AE2-F752C8A9772B}_is1'


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def registry_fingerprint():
    result = []
    for hive, name in [(winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')]:
        for view in [winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY]:
            try:
                with winreg.OpenKey(hive, 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + APP_ID,
                                    0, winreg.KEY_READ | view) as key:
                    values = []
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        n, v, t = winreg.EnumValue(key, i)
                        values.append((n, repr(v), t))
                    digest = hashlib.sha256(json.dumps(sorted(values)).encode()).hexdigest()
                    result.append((name, view, digest))
            except FileNotFoundError:
                result.append((name, view, 'ABSENT'))
    return result


def stop_owned(process):
    if process is None:
        return
    try:
        for child in reversed(psutil.Process(process.pid).children(recursive=True)):
            try:
                child.terminate()
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    if process.poll() is None:
        process.terminate()
        process.wait(30)


def wait_api(client):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            response = client.get('/api/config')
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(.3)
    raise AssertionError('Disposable packaged application did not start')


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return {table: [dict(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                for table in ['batches', 'episodes']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--old-exe', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native-review', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    assert ROOT / 'release' in output.parents
    output.mkdir(exist_ok=False, parents=True)
    release = args.release.resolve()
    original = args.old_exe.resolve()
    assert 'candidate-dist-3' in original.parts
    assert sha(original) == 'b27ea09a880a48afc81e5f917ddd53e5402b0727b72a770b0911317b2f1a20f5'
    source = args.source.resolve()
    assert source.is_file() and source.suffix.lower() in {'.mp4', '.mkv'}
    original_source_sha = sha(source)
    before_registry = registry_fingerprint()
    before_owner = json.loads((release.parent / 'installed-preservation-before.json').read_text())
    assert all(sha(Path(p)) == h for p, h in before_owner.items())
    result = {'checks': {}, 'installer_executed': False, 'production_contacted': False}
    checks = result['checks']
    process = None
    try:
        artifacts = json.loads((release / 'ARTIFACTS.json').read_text())
        zip_record = next(r for r in artifacts if r['path'].endswith('.zip'))
        archive = release / zip_record['path']
        assert sha(archive) == zip_record['sha256']
        extracted = output / 'extracted'
        extracted.mkdir()
        with zipfile.ZipFile(archive) as packed:
            assert packed.testzip() is None
            for entry in packed.infolist():
                target = (extracted / entry.filename).resolve()
                assert target.is_relative_to(extracted) and not Path(entry.filename).is_absolute()
            packed.extractall(extracted)
        bundle = extracted / 'ABOODVIDEO-Studio'
        manifest = json.loads((extracted / 'PAYLOAD_MANIFEST.json').read_text())
        assert all(sha(bundle / r['path']) == r['sha256'] for r in manifest)
        assert len(manifest) == len([p for p in bundle.rglob('*') if p.is_file()])
        checks['zip_crc_extraction_and_243_payload_hashes'] = 'PASS'
        profile = output / 'profile'
        data = profile / 'ABOODVIDEO Studio'
        data.mkdir(parents=True)
        converted = output / 'converted'
        fonts = output / 'fonts'
        converted.mkdir(); fonts.mkdir()
        config = {'server_url': 'http://127.0.0.1:29997/api/v1', 'converted_output_folder': str(converted),
                  'font_library_folder': str(fonts), 'first_run_complete': True,
                  'default_encoder': 'cpu', 'default_preset': 'quality', 'include_360p': True,
                  'cpu_concurrent_jobs': 2, 'gpu_concurrent_jobs': 1, 'processing_mode': 'local'}
        (data / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        env = dict(os.environ, LOCALAPPDATA=str(profile), APPDATA=str(profile / 'roaming'),
                   USERPROFILE=str(profile / 'home'), HOME=str(profile / 'home'), ENVIRONMENT='test',
                   PATH=os.path.join(os.environ['WINDIR'], 'System32') + ';' + os.environ['WINDIR'])
        for name in ['PYTHONPATH', 'PYTHONHOME', 'ABOODVIDEO_FFMPEG_BIN_DIR']:
            env.pop(name, None)
        for name in ['APPDATA', 'USERPROFILE']:
            Path(env[name]).mkdir(parents=True)
        port = 28611
        with socket.socket() as check:
            check.bind(('127.0.0.1', port))
        base_url = 'http://127.0.0.1:' + str(port)
        def launch(exe, native=False):
            return subprocess.Popen([str(exe), '--port', str(port), *([] if native else ['--no-browser'])],
                                    cwd=output, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with httpx.Client(base_url=base_url, headers={'Origin': base_url}, timeout=60) as client:
            process = launch(original)
            old_cfg = wait_api(client)
            assert Path(old_cfg['appdata_root']).resolve() == data
            test_key = secrets.token_urlsafe(36)
            assert client.post('/api/config', json={'api_key': test_key}).status_code == 200
            assert client.get('/api/config').json()['has_api_key']
            imported = client.post('/api/batch/import-file', json={'file_path': str(source), 'destination_label': 'Disposable release queue'})
            assert imported.status_code == 200
            stop_owned(process); process = None
            # Seed representative history/states into this disposable database
            # only. These records are fixtures, not claimed processing results.
            db_path = data / 'jobs.db'
            with sqlite3.connect(db_path) as db:
                db.row_factory = sqlite3.Row
                template = dict(db.execute('SELECT * FROM episodes LIMIT 1').fetchone())
                batch_template = dict(db.execute('SELECT * FROM batches LIMIT 1').fetchone())
                assert template['state'] in {'PENDING', 'READY_TO_ENCODE', 'NEEDS_REVIEW'}
                db.execute("UPDATE batches SET status='PAUSED',pause_requested=1")
                for state in ['PAUSED', 'FAILED', 'READY']:
                    batch = dict(batch_template, id=str(uuid.uuid4()), status='COMPLETED' if state == 'READY' else 'PAUSED', pause_requested=1)
                    episode = dict(template, id=str(uuid.uuid4()), batch_id=batch['id'], state=state,
                                   current_stage=state, progress=100.0 if state == 'READY' else 0.0)
                    for table, row in [('batches', batch), ('episodes', episode)]:
                        db.execute('INSERT INTO ' + table + ' (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
            baseline_rows = rows(db_path)
            saved_config = json.loads((data / 'config.json').read_text())
            cipher_hash = sha(data / 'secrets.dat')
            assert test_key.encode() not in (data / 'secrets.dat').read_bytes()
            assert 'api_key' not in saved_config
            checks['older_portable_launch_queue_and_protected_credential_creation'] = 'PASS'
            new_exe = bundle / 'ABOODVIDEO Studio.exe'
            def verify_profile(label):
                cfg = wait_api(client)
                assert cfg['has_api_key'] and 'api_key' not in cfg
                for key, value in saved_config.items():
                    assert cfg[key] == value, key
                assert rows(db_path) == baseline_rows
                assert sha(data / 'secrets.dat') == cipher_hash
                # Decrypt only the generated fixture credential in the isolated
                # profile; compare in memory, never print or persist plaintext.
                check = subprocess.run([sys.executable, '-c',
                    'import sys; from studio.config import load_secure_api_key; assert load_secure_api_key()==sys.stdin.read()'],
                    input=test_key, text=True, cwd=ROOT, env=env, capture_output=True)
                assert check.returncode == 0, 'Disposable credential did not survive'
                current = client.get('/api/batch/current').json()
                assert current['runner']['active_encodes_count'] == 0
                assert not current['runner']['is_uploading']
                history = client.get('/api/batch/history').json()
                assert len(history['seasons']) == 1
                checks[label] = 'PASS'
            process = launch(new_exe, native=args.native_review)
            verify_profile('actual_zip_upgrade_settings_history_sqlite_queue_dpapi')
            if args.native_review:
                (output / 'native-ready.json').write_text(json.dumps({'pid': process.pid, 'exe': str(new_exe), 'url': base_url, 'source': str(source)}))
                print('NATIVE_REVIEW_READY=' + str(output), flush=True)
                deadline = time.monotonic() + 600
                while not (output / 'native-complete.json').exists():
                    if time.monotonic() > deadline:
                        raise AssertionError('Native review timed out')
                    time.sleep(.5)
                result['native_review'] = json.loads((output / 'native-complete.json').read_text())
            stop_owned(process); process = None
            process = launch(original)
            verify_profile('actual_portable_rollback_to_0_4_1_preserves_profile')
            stop_owned(process); process = None
            process = launch(new_exe)
            verify_profile('actual_portable_reupgrade_preserves_profile')
            stop_owned(process); process = None
        assert registry_fingerprint() == before_registry
        assert all(sha(Path(p)) == h for p, h in before_owner.items())
        assert sha(source) == original_source_sha
        checks['owner_app_profile_registry_and_media_unchanged'] = 'PASS'
        result['PASS'] = True
        result['disposable_episode_count'] = len(baseline_rows['episodes'])
        result['new_exe_sha256'] = sha(new_exe)
        result['windows_clean_vm'] = 'BLOCKED: only existing owner Windows VM with no clean snapshot found'
        result['installer_fresh_upgrade_uninstall_registry_runtime'] = 'NOT_EXECUTED: no clean disposable Windows VM'
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
    except BaseException as error:
        result.update(PASS=False, error_type=type(error).__name__, error=str(error)[:1000])
        raise
    finally:
        stop_owned(process)
        (output / 'PORTABLE_UPGRADE_RESULTS.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
