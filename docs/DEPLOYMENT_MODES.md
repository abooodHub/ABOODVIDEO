# Deployment modes

The same `backend/` source supports both modes. Use a private environment file;
`.env.example` contains no usable credentials. No media or production database is
included in a clone. Back up an existing installation before applying migrations.

| Setting | Full conversion platform | Prepared-media host |
|---|---|---|
| `ENABLE_TRANSCODING` | `true` | `false` |
| `ENABLE_DIRECT_IMPORT` | `false` | `true` |
| Upload page | Source upload and encoder choices | Ready HLS/MP4 import |
| Worker/reconciler | Required for server conversion | Do not start |

The defaults preserve the full conversion platform. A prepared-media deployment
rejects conversion and retry API calls server-side. Local Studio can still prepare
media on a separate machine. Private visibility and playback authorization remain
required in either mode.

## Build and start

Install Python 3.12 dependencies with `pip install -r backend/requirements.txt`.
Configure PostgreSQL, the three Redis URLs, storage, and independent signing secrets.
From `backend/`, run `alembic upgrade head` before `uvicorn app.main:app`.

For Docker, `docker compose build backend` builds from the repository root.
The existing Compose defaults start conversion services. To start a new prepared-media
installation explicitly, configure the two flags above, then select only these services:

```sh
docker compose up -d postgres redis backend nginx
```

This command is for a new installation: it does not stop workers from an existing
installation. Migrate an existing full installation deliberately, after draining
conversion work; do not change its mode while jobs are active. Do not overwrite
existing volume names, database URLs, media mounts, or reverse-proxy configuration.
Container entrypoint applies the existing Alembic migration chain.

## Ready imports

The canonical implementation is `app/api/v1/direct_import.py`,
`app/services/direct_import.py`, and `admin_ready_upload.html`.
Ready MP4 and checksummed HLS ZIP/folder imports are administrator authenticated and
CSRF protected; imported videos start PRIVATE. `DIRECT_IMPORT_MAX_BYTES` bounds the
import size. No conversion worker is queued by this workflow.

Server-folder import is optional: mount a dedicated input directory read-only and
set `ABV_IMPORT_PATH` to its container path. Never mount the server root. The importer
rejects traversal and symbolic links. Browser uploads do not need this mount.

## Optional WordPress bridge

`app/api/wp_private_media.py` verifies short-lived, resource-bound HMAC signatures
and the WordPress login-session proof before serving PRIVATE media. It is unusable
until configured with a private HMAC key and key ID (or a private rotation key ring).
Use `WP_PRIVATE_MEDIA_ALLOWED_ORIGIN` for the exact HTTPS WordPress origin. The
WordPress signer must implement the same token format; this repository contains
the media host component, not a copy of a WordPress installation or its users.

Keep these values outside Git. Rotating a bridge key must be coordinated with the
signer; changing a platform database password requires updating the database and
its private configuration together. Repository cleanup does not rotate live keys.
