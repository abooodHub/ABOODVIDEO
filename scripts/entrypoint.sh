#!/usr/bin/env bash
set -euo pipefail
umask 027

echo "=== [ABOODVIDEO] Starting Container Bootstrap ==="

# Wait for PostgreSQL
echo "--> Waiting for PostgreSQL..."
python -c "
import os, time, sys, psycopg
raw_url = os.environ.get('DATABASE_URL_SYNC', '').strip('\"\'')
url = raw_url.replace('postgresql+psycopg://', 'postgresql://').replace('postgresql+asyncpg://', 'postgresql://')
for i in range(30):
    try:
        conn = psycopg.connect(url, connect_timeout=3)
        conn.close()
        print('PostgreSQL is ready!')
        sys.exit(0)
    except Exception:
        if i == 0 or i == 29:
            print(f'Attempt {i+1}/30: PostgreSQL dependency unavailable', file=sys.stderr)
        time.sleep(1)
print('ERROR: PostgreSQL timeout', file=sys.stderr)
sys.exit(1)
"

# Wait for Redis
echo "--> Waiting for Redis..."
python -c "
import os, time, sys, redis
for k in ['REDIS_QUEUE_URL', 'REDIS_SESSION_URL', 'REDIS_CACHE_URL']:
    url = os.environ.get(k, '').strip('\"\'')
    if not url:
        print(f'ERROR: Required Redis setting {k} is empty', file=sys.stderr)
        sys.exit(1)
    try:
        r = redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
    except Exception:
        print(f'ERROR: Unable to initialize required Redis setting {k}', file=sys.stderr)
        sys.exit(1)
    ready = False
    for i in range(15):
        try:
            if r.ping():
                ready = True
                break
        except Exception:
            if i in (0, 14):
                print(f'Attempt {i+1}/15: Redis dependency {k} is unavailable', file=sys.stderr)
        if not ready:
            time.sleep(1)
    if not ready:
        print(f'ERROR: Redis dependency {k} did not become ready', file=sys.stderr)
        sys.exit(1)
print('All required Redis instances are ready!')
"

# Run Alembic Migrations idempotently
echo "--> Running Database Migrations (alembic upgrade head)..."
alembic upgrade head
echo "--> Migrations successfully applied."

# Storage directories assurance
mkdir -p "${STORAGE_UPLOADS_PATH:-/app/storage/uploads}"
mkdir -p "${STORAGE_VIDEOS_PATH:-/app/storage/videos}"
mkdir -p "${STORAGE_TEMP_PATH:-/app/storage/temp}"

# Rule 7: Do NOT create OWNER automatically here.
# Owner creation is strictly delegated to: python -m app.cli create-owner

echo "=== [ABOODVIDEO] Bootstrap Complete. Launching Application ==="

# Execute the passed command or default to uvicorn with configured workers
if [ "$#" -gt 0 ]; then
    exec "$@"
else
    WORKERS="${WEB_CONCURRENCY:-1}"
    echo "--> Launching Uvicorn with workers=${WORKERS} on port ${PORT:-8000}..."
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WORKERS}" --no-access-log
fi
