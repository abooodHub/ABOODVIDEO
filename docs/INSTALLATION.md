# Installation and upgrades — 1.1.0

## New Platform server

Prerequisites: Docker Compose 2.24.4+, Python 3 and sufficient media storage. Public HTTPS hosting additionally needs your own DNS hostname directed to the server and reachable TCP ports 80/443. Local HTTP installation does not need a domain.

```sh
python3 scripts/install_platform.py --domain video.example.com --start
```

Add `--ready-only` for prepared HLS/MP4 imports with conversion disabled. Omitting the domain selects local HTTP; do not expose that mode publicly. Setup generates independent signing and database secrets, protects the environment file, and refuses to overwrite it. There is no default account/password: initial owner creation is interactive.

The public Compose overlay starts Caddy for certificate issuance and renewal in front of the Nginx media gateway. PostgreSQL, Redis and the API have no public host ports. Backend and conversion workers run as UID/GID 10001, drop Linux capabilities and prevent privilege escalation. Ready-only setup starts no conversion workers.

Setup builds the maintained service images in `deploy/`: PostgreSQL and Redis run as their service users with current OS fixes; HTTPS uses Caddy 2.11.7 rebuilt with patched Go 1.27.2 and x/net 0.60.0. The first build needs internet access and may take several minutes. Existing database services are not upgraded by editing source files; schedule that maintenance separately.

Omit `--start` to generate configuration only. `--env-file` selects a new private file. Noninteractive startup requires `--skip-owner`; create the owner later with the same Compose arguments and `exec backend python -m app.cli create-owner`.

## Existing Platform installation

Keep private configuration, storage volumes, integrations and deployment mode. Do not run the first-install generator against an existing deployment. Back up private configuration and the database, check schema compatibility, and keep the old image for rollback. An installation already using HTTPS termination keeps its existing proxy; it does not need the new Caddy overlay.

Older storage owned by root requires a deliberate ownership migration to UID/GID 10001 before switching the application user. Identify the exact volume/mount and preserve its file contents; never recursively change an inferred host path. Review [deployment modes](DEPLOYMENT_MODES.md).

## Windows Studio

Run `ABOODVIDEO-Studio-Setup-v1.1.0.exe`. Setup installs per-user without administrator rights and offers a desktop shortcut. It checks WebView2 Runtime in both user and machine registry views. When missing, it runs the embedded signed Microsoft Evergreen bootstrapper before copying Studio. That prerequisite installation needs internet access.

For the portable version, extract the complete `ABOODVIDEO-Studio-1.1.0-windows-x64-portable.zip` and run `ABOODVIDEO Studio.exe`. WebView2 is also required for this version. FFmpeg/FFprobe and the exact distribution's license/source notices are bundled. Compare downloaded artifacts against `SHA256SUMS.txt`.

Settings, protected credentials and queue history remain in `%LOCALAPPDATA%\ABOODVIDEO Studio`. Close Studio normally before upgrading and keep a private profile backup. The installer preserves its application ID, never automatically closes Studio, and does not remove profile data or the shared WebView2 Runtime on uninstall. Portable builds use the same profile unless given a separate Windows user/profile for testing.

## Builds and verification

See [DEVELOPMENT.md](DEVELOPMENT.md). Installer builds require `--webview2-bootstrapper` with a valid Microsoft Authenticode signature. Unsigned or differently signed prerequisites are rejected. Project executables/installers remain unsigned unless a project signing certificate is configured separately.

Use a disposable Windows VM to verify missing-WebView2 setup, fresh installation, upgrade and uninstall. Source checks and a portable upgrade test do not establish those clean-machine installer results.

## Interface language

Choose Arabic or English in the language selector. Arabic uses a right sidebar and RTL; English uses a left sidebar and LTR. Studio persists the preference locally. The installer also offers Arabic and English. See [language behavior](LOCALIZATION.md).
