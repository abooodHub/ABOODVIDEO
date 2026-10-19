"""Configure a NEW installation with independent secrets; never overwrite .env."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def public_domain(value):
    value = value.strip().lower().rstrip('.')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError('Use a DNS hostname for automatic HTTPS, not an IP address')
    if len(value) > 253 or '.' not in value or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in value.split('.')
    ):
        raise ValueError('Domain must be a DNS hostname without a scheme, port or path')
    return value


def configure(path, *, domain=None, ready_only=False):
    hostname = public_domain(domain) if domain else 'localhost'
    password = secrets.token_urlsafe(36)
    database = f'aboodvideo_user:{password}@postgres:5432/aboodvideo_db'
    origin = ('https://' if domain else 'http://') + hostname
    values = {
        'SECRET_KEY': secrets.token_urlsafe(48), 'IP_PRIVACY_SALT': secrets.token_urlsafe(48),
        'POSTGRES_PASSWORD': password, 'DATABASE_URL': 'postgresql+asyncpg://' + database,
        'DATABASE_URL_SYNC': 'postgresql+psycopg://' + database,
        'DOMAIN': hostname, 'PUBLIC_BASE_URL': origin, 'CORS_ORIGINS': origin,
        'ALLOWED_HOSTS': hostname + ',localhost,127.0.0.1,backend,nginx',
        'SECURITY_MODE': 'public' if domain else 'lan',
        'FORCE_HTTPS': 'true' if domain else 'false',
        'SESSION_COOKIE_SECURE': 'true' if domain else 'false',
        'TRUSTED_PROXIES': '127.0.0.1,::1,172.28.0.0/16',
        'REDIS_QUEUE_URL': 'redis://redis:6379/0',
        'REDIS_SESSION_URL': 'redis://redis:6379/1',
        'REDIS_CACHE_URL': 'redis://redis:6379/2',
        'ENABLE_TRANSCODING': 'false' if ready_only else 'true',
        'ENABLE_DIRECT_IMPORT': 'true' if ready_only else 'false',
    }
    lines = (ROOT / '.env.example').read_text(encoding='utf-8-sig').splitlines()
    output = []
    for line in lines:
        key = line.split('=', 1)[0] if '=' in line and not line.startswith('#') else ''
        output.append(f'{key}="{values[key]}"' if key in values else line)
    path = Path(path).resolve()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as file:
        file.write('\n'.join(output) + '\n')
    if os.name == 'nt':
        account = subprocess.check_output(['whoami'], text=True).strip()
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', account + ':(F)', '*S-1-5-18:(F)'],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        path.chmod(0o600)
    return origin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', help='Public hostname; omit for local HTTP only')
    parser.add_argument('--ready-only', action='store_true', help='Prepared HLS/MP4; no conversion workers')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--start', action='store_true', help='Build/start Docker and prompt for initial owner')
    parser.add_argument('--skip-owner', action='store_true', help='Create owner later using app.cli create-owner')
    args = parser.parse_args()
    env_path = args.env_file.resolve()
    if env_path.exists():
        parser.error('Environment file already exists; existing installations are never overwritten')
    if args.start:
        if not shutil.which('docker'):
            parser.error('Install Docker Engine/Desktop and Docker Compose first')
        version = subprocess.check_output(['docker', 'compose', 'version', '--short'], text=True).strip().lstrip('v')
        numbers = tuple(int(n) for n in re.findall(r'\d+', version)[:3])
        if numbers < (2, 24, 4):
            parser.error('Docker Compose 2.24.4 or newer is required')
        if not args.skip_owner and not sys.stdin.isatty():
            parser.error('Use an interactive terminal for owner creation or specify --skip-owner')
    try:
        origin = configure(env_path, domain=args.domain, ready_only=args.ready_only)
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))
    print('Created private configuration with unique secrets. No secret values were printed.')
    command = ['docker', 'compose', '--env-file', str(env_path), '-f', str(ROOT / 'docker-compose.yml')]
    if args.domain:
        command += ['-f', str(ROOT / 'compose.public.yml')]
    if args.start:
        subprocess.run(command + ['config', '--quiet'], check=True, cwd=ROOT)
        services = ['postgres', 'redis', 'backend', 'nginx']
        if not args.ready_only:
            services += ['worker', 'reconciler']
        if args.domain:
            services.append('https')
        build_services = ['postgres', 'redis', 'backend', 'nginx']
        if args.domain:
            build_services.append('https')
        subprocess.run(command + ['build', '--pull', *build_services], check=True, cwd=ROOT)
        subprocess.run(command + ['up', '-d', '--wait', *services], check=True, cwd=ROOT)
        if not args.skip_owner:
            subprocess.run(command + ['exec', 'backend', 'python', '-m', 'app.cli', 'create-owner'], check=True, cwd=ROOT)
    print('Administration: ' + origin + '/admin')
    if args.domain:
        print('Automatic HTTPS requires DNS to this host and reachable TCP ports 80/443.')
    if args.skip_owner or not args.start:
        print('Create the initial owner explicitly: docker compose exec backend python -m app.cli create-owner')


if __name__ == '__main__':
    main()
