# Test catalog

`pytest.ini` discovers tests under `tests/`. Use `python scripts/run_tests.py` with the isolated PostgreSQL and Redis configuration in [DEVELOPMENT.md](DEVELOPMENT.md). The harness guards service targets and isolates Studio data and generated storage.

| Area | Representative files |
| --- | --- |
| Hidden public pages | `test_hidden_home.py`, `test_admin_console_policy.py` |
| Authentication and administration | `test_admin_auth_and_management.py`, `test_admin_navigation.py`, `test_admin_security_browser_regression.py` |
| Upload and processing | `test_upload_resumable.py`, `test_upload_processing_choice.py`, `test_ready_import.py`, `test_transcoding_pipeline.py` |
| Playback and access | `test_secure_playback_and_subtitles.py`, `test_embed_route.py`, `test_share_lifecycle.py`, `test_player_*` |
| Studio core and queue | `test_studio_core.py`, `test_season_batch_*`, `test_paused_batch_startup.py`, `test_processing_history_compat.py` |
| Studio import and upload | `test_studio_import_*`, `test_studio_archive_upload.py`, `test_studio_data_integrity.py` |
| Release and publication | `test_studio_release.py`, `test_release_schema_gate.py`, `test_github_readiness.py` |
| Opt-in integration and stability | `test_real_platform_*`, `test_studio_packaged_release.py`, `test_full_length_stability.py` |

Browser tests require Chromium and explicit enablement where specified. Real-media/load/packaged-app suites require separate opt-in and owned disposable staging; see [tests/REAL_INTEGRATION.md](../tests/REAL_INTEGRATION.md). Routine tests use short synthetic media and generated font fixtures. Test names containing historical phase identifiers remain unchanged to preserve regression coverage and invocation paths.
