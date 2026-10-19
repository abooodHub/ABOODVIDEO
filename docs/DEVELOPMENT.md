# Development

## Requirements and setup

Use Python 3.12. Choose the pinned dependency set for the work:

- Backend runtime: `pip install -r backend/requirements.txt`.
- Studio: `pip install -r requirements-studio.txt` (includes psutil and Windows pywebview).
- Development/tests: `pip install -r requirements-dev.txt`, then `python -m playwright install chromium`.
- Windows release build: `pip install -r requirements-build.txt`; also install Inno Setup, WebView2, and permitted FFmpeg/FFprobe binaries. See `studio.spec` and `installer.iss`.

Install Node.js only for JavaScript syntax checks. Tests require FFmpeg/FFprobe on PATH for small synthetic-media fixtures. The full production-media/load suites remain explicitly opt-in. Platform tests require dedicated PostgreSQL and Redis services.

Create a virtual environment, install backend requirements, copy `.env.example` to a private `.env`, and configure unique local secrets and service URLs. Never commit `.env`.

## Run locally

- Platform: run the FastAPI application from `backend/` with the local isolated services configured. Apply migrations only to a disposable development database.
- Studio: run `run_studio.bat` on Windows. The Studio stores its profile under `%LOCALAPPDATA%\ABOODVIDEO Studio`; use an isolated `LOCALAPPDATA` for tests and smoke checks.
- Tests: use the disposable services and commands below. `tests/conftest.py` rejects other endpoints and redirects Studio profile/storage to disposable test locations. The runner applies migrations before pytest; `create_all()` is not an equivalent schema setup.

### Isolated tests on Windows

These are new disposable containers with no production volumes. Choose unused names and ports if they already exist; never remove another project's services.

```powershell
docker run -d --rm --name abv-dev-test-pg -e POSTGRES_USER=abv_test -e POSTGRES_PASSWORD=local-disposable-tests -e POSTGRES_DB=aboodvideo_test -p 127.0.0.1:15432:5432 postgres:16-alpine
docker run -d --rm --name abv-dev-test-redis -p 127.0.0.1:16379:6379 redis:7-alpine
$env:ABOODVIDEO_TEST_DATABASE_URL='postgresql+asyncpg://abv_test:local-disposable-tests@127.0.0.1:15432/aboodvideo_test'
$env:ABOODVIDEO_TEST_REDIS_QUEUE_URL='redis://127.0.0.1:16379/0'
$env:ABOODVIDEO_TEST_REDIS_SESSION_URL='redis://127.0.0.1:16379/1'
$env:ABOODVIDEO_TEST_REDIS_CACHE_URL='redis://127.0.0.1:16379/2'
python scripts/run_tests.py -q
python scripts/check_repository_secrets.py --index --history
python -m pip_audit -r requirements-dev.txt
```

Wait for `docker exec abv-dev-test-pg pg_isready -U abv_test -d aboodvideo_test` and `docker exec abv-dev-test-redis redis-cli ping` to succeed before running the tests. Stop only these two disposable containers after testing. The sample database password above is public test data and must never be used for a deployed account.

Enable selected browser tests with `$env:ABOODVIDEO_RUN_BROWSER_TEST='1'`; run the files listed in `.github/workflows/checks.yml`. Browser binaries are shared read-only tools; Studio profiles and generated media stay isolated.

See [DEPLOYMENT_MODES.md](DEPLOYMENT_MODES.md) for full conversion versus prepared-media operation, and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for bundled browser assets.

Automated tests must never point at production services, real Studio profiles, `D:\ok.anime`, `D:\upload`, or `D:\fonts`. Do not run real media conversion during routine tests.

## Build Windows Studio

The source release is 1.0.0 for both Platform and Studio. Install `requirements-build.txt`, Inno Setup and WebView2. Supply FFmpeg and FFprobe from a distribution you are permitted to redistribute. From the repository root:

```powershell
python scripts/build_studio_release.py --output release/Studio-1.0.0-build --ffmpeg-bin '<FFmpeg-distribution>\bin' --iscc '<InnoSetup>\ISCC.exe' --webview2-bootstrapper '<Microsoft-download>\MicrosoftEdgeWebview2Setup.exe'
```

Use a new output directory for every build. Omit `--iscc` to produce only the portable application and ZIP. The builder validates Windows version 1.0.0.0, runtime dependencies, source consistency and payload hashes. Output includes `ABOODVIDEO-Studio-1.0.0-windows-x64-portable.zip`, an optional `ABOODVIDEO-Studio-Setup-v1.0.0.exe` installer and manifests/checksums.

The icons come from `app.ico` and editable `design/brand/` sources. The installer retains its existing application ID and installation/profile locations for upgrade continuity. Building does not install the program or replace user data. Font files with unknown redistribution rights remain outside the bundle. Internal UI filenames such as `index_v3.html` and protocol/schema versions are implementation identifiers, independent of the product version.

Use an intact FFmpeg distribution with `LICENSE` and `README.txt` above its `bin` directory. The supplied WebView2 bootstrapper must have a valid Microsoft Authenticode signature. See [INSTALLATION.md](INSTALLATION.md) for deployment and Windows prerequisites.
