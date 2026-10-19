# Project map — 1.1.0

## Source directories

| Path | Contents |
| --- | --- |
| `backend/app/api/` | Administration, upload, watch and embed routes |
| `backend/app/services/` | Processing, packages, library and playback services |
| `backend/app/security/` | Authentication, roles, ownership and playback authorization |
| `backend/app/models/`, `schemas/` | Database models and API contracts |
| `backend/app/workers/` | Processing and package validation workers |
| `backend/app/templates/`, `static/` | Administration and playback interfaces |
| `backend/migrations/` | Schema migration chain and compatibility history |
| `studio/core/` | Import, durable queue, encoding, fonts, packaging and upload |
| `studio/web/` | Windows client's local FastAPI application and WebView UI |
| `nginx/`, `docker-compose.yml` | Server deployment configuration |
| `tests/` | Automated regressions and synthetic fixtures |
| `scripts/` | Build, test, publication and operational utilities |
| `docs/` | Current setup, architecture, API and operational guides |
| `design/brand/` | Editable icon artwork used by the Studio build |

Studio processing flows from `studio/web/routes/` through `batch_runner.py`, `probe.py`, `encoder.py`, subtitle/font handling, `packager.py` and `uploader.py`. Durable queue state lives in `batch_store.py`; explicit start authorization belongs in the runner.

## Release files

- Platform version: `backend/app/__init__.py`.
- Studio version: `studio/__init__.py`; imported by UI, API, CLI and package metadata.
- Windows executable metadata: `studio_version_info.txt`.
- Build and installer: `studio.spec`, `installer.iss`, `scripts/build_studio_release.py`.
- Source publication: `scripts/export_github_source.py`.

Both product versions are 1.1.0; Windows numeric metadata is 1.1.0.0. Internal protocol/schema versions and migration identifiers are independent and must remain compatible.

## Local data and generated output

The installed Studio profile is `%LOCALAPPDATA%\ABOODVIDEO Studio\` and contains queue records, settings, protected credentials, fonts and logs. The installation is under `%LOCALAPPDATA%\Programs\ABOODVIDEO Studio\`. Runtime storage, `studio/data/`, `studio/fonts/`, `.env`, `backups/`, `scratch/`, `release/`, `dist/`, `build/` and audit evidence are local files excluded from publication. Do not clear these as part of source cleanup.

Historical phase reports, mockups and superseded one-off scripts have been moved to an ignored local archive under `backups/v1_cleanup_<timestamp>/archived/`, with a hash manifest. They are not part of the version 1 source distribution.

See [development](docs/DEVELOPMENT.md), [script catalog](docs/SCRIPT_CATALOG.md) and [test catalog](docs/TEST_CATALOG.md). Use disposable test services and profiles; never use production services or real media for routine tests.

## Shared interface languages

`shared/i18n/` contains the Arabic/English UI catalog, request locale, template loader and browser direction assets used by both Platform and Studio. See [localization](docs/LOCALIZATION.md).
