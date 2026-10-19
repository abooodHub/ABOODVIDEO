#!/usr/bin/env bash
# ==============================================================================
# ABOODVIDEO Database & Configuration Backup Script
# ==============================================================================
# Uses PostgreSQL Custom Format (pg_dump -Fc) with SHA-256 integrity checksums.
# ==============================================================================

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups/aboodvideo}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
PG_CONTAINER="${PG_CONTAINER:-aboodvideo_postgres}"
PG_USER="${POSTGRES_USER:-aboodvideo_user}"
PG_DB="${POSTGRES_DB:-aboodvideo_db}"

mkdir -p "${BACKUP_DIR}"

echo "=== [ABOODVIDEO] Starting Backup at ${TIMESTAMP} ==="

# 1. PostgreSQL Relational Dump (Custom Format -Fc)
DUMP_FILE="${BACKUP_DIR}/db_${PG_DB}_${TIMESTAMP}.dump"
echo "--> Dumping database ${PG_DB} in custom format (-Fc) from ${PG_CONTAINER}..."
docker exec "${PG_CONTAINER}" pg_dump -U "${PG_USER}" -d "${PG_DB}" -Fc > "${DUMP_FILE}"
echo "--> Generating SHA-256 checksum for database dump..."
sha256sum "${DUMP_FILE}" | awk '{print $1}' > "${DUMP_FILE}.sha256"
echo "--> Database dump saved: ${DUMP_FILE} ($(du -h "${DUMP_FILE}" | cut -f1))"

# 2. Application Configuration & Migration Snapshot
CONFIG_BACKUP="${BACKUP_DIR}/config_${TIMESTAMP}.tar.gz"
echo "--> Archiving environment template and alembic migrations..."
tar -czf "${CONFIG_BACKUP}" --exclude='.env' .env.example docker-compose.yml backend/alembic.ini backend/migrations
sha256sum "${CONFIG_BACKUP}" | awk '{print $1}' > "${CONFIG_BACKUP}.sha256"
echo "--> Configuration archive saved: ${CONFIG_BACKUP}"

# 3. Retention Policy: Retain backups for 14 days
echo "--> Pruning backups older than 14 days..."
find "${BACKUP_DIR}" -type f \( -name "*.dump" -o -name "*.tar.gz" -o -name "*.sha256" \) -mtime +14 -delete

echo "=== [ABOODVIDEO] Backup Completed Successfully ==="
