# Script catalog

## Supported development and release tools

| Script | Purpose |
| --- | --- |
| `scripts/run_tests.py` | Validate isolated service targets, migrate the test database and run pytest |
| `scripts/check_repository_secrets.py` | Check working files, index and optionally local Git history |
| `scripts/export_github_source.py` | Export publishable source into a new directory with an empty Git repository |
| `scripts/build_studio_release.py` | Build versioned Windows portable/installer artifacts and verify metadata and payload hashes |
| `scripts/check_release_schema_compatibility.py` | Check release/database migration compatibility |
| `scripts/run_real_integration.py` | Run explicitly opted-in integration checks in owned disposable staging |
| `scripts/prepare_staging_media.py` | Prepare inputs for explicitly opted-in real-media integration checks |
| `scripts/capture_gpu_ownership.py` | Collect GPU process ownership for the opted-in stability suite |
| `scripts/entrypoint.sh` | Container process entrypoint |
| `scripts/setup_test_owner.py` | Explicit account setup using an externally supplied password |
| `scripts/backup_db.sh` | Database backup helper; inspect configuration before use |

Other inspection and acceptance utilities remain in `scripts/`. Some access live services or change data; review their targets and inputs before invoking them. They are not runtime dependencies merely because they are in this directory.

Superseded phase demos, episode-specific upload/processing helpers, old installer checks and one-time source-rewrite scripts have been archived locally under ignored `backups/v1_cleanup_<timestamp>/archived/scripts/`. Runtime entrypoints and helpers referenced by current tests remain at their existing paths.
