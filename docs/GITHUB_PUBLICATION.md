# GitHub publication

Publish source from a clean export so local runtime files and development history stay private.

```sh
python scripts/check_repository_secrets.py
python scripts/export_github_source.py --output release/ABOODVIDEO-1.0.0-source
```

The destination must not exist. The exporter follows ignore rules, refuses symlinks, scans credentials and copies current source without media, databases, private settings, backups or Git history. It initializes an empty repository without creating a commit or remote. The original working tree and index are preserved.

Review the export before the first commit. Use your own repository URL, configure a public noreply Git author address if desired, and scan the committed history with:

```sh
python scripts/check_repository_secrets.py --index --history
```

## Documentation assets

The README includes original SVG artwork, an animated workflow and actual UI templates rendered with synthetic offline data. No production screenshot or deployment address is needed.

```sh
python scripts/generate_documentation_visuals.py
python scripts/generate_brand_assets.py --ffmpeg /path/to/ffmpeg
```

Install Playwright and its Chromium browser in the development environment. Screenshot requests are intercepted locally; the generator never contacts a deployed platform.

## Releases

Keep installers and portable ZIPs out of source commits. Build them using `scripts/build_studio_release.py`, verify their source and payload manifests, and publish them as GitHub Release assets together with SHA256SUMS. State the actual checks and signing status in release notes. An installer build is not proof of a successful clean Windows install.

Never publish a private environment file, access key, account password, database dump or user media. If a credential is exposed, rotate it with its consumers before treating removal from source as sufficient; see [credential rotation](CREDENTIAL_ROTATION.md).
