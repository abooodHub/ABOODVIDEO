# Current source release

Updated 2026-10-19.

| Product | Version |
| --- | --- |
| ABOODVIDEO Platform | 1.1.0 |
| ABOODVIDEO Studio | 1.1.0 |
| Windows file/product numeric version | 1.1.0.0 |

This is the version of the current source and its build configuration. Changing source metadata does not update an installed executable or deployed server. Build and deploy the intended artifact separately.

## Included behavior

- Arabic/English UI with saved preferences and RTL/LTR sidebar placement across Platform and Studio.

- Empty HTTP 404 for the root and closed public catalog routes, with protected administration and valid playback links.
- Authenticated administration, upload and encoder configuration.
- Server conversion and prepared-media HLS/MP4 modes.
- Private playback, subtitle/font support, resumable package uploads and Studio's persistent, explicitly started queue.
- Isolated test tooling, CI checks, dependency pins, third-party notices and a credential-checked source exporter.
- Deployment-neutral defaults: Studio starts without a saved server URL, embed allowlists are configured by the operator, and generated links use the configured platform origin.

## Distribution validation

The Windows portable build has been launched and tested with isolated profiles, including persisted queue/settings and protected credentials across upgrades. The project binaries are unsigned. A clean Windows installation/upgrade/uninstall cycle and missing-WebView2 installation remain unverified; building the installer does not establish those results.

Documentation screenshots render actual application templates with synthetic offline API fixtures. Their counts, titles and hardware values are illustrative and contain no live deployment data.

## Source organization

Current guides remain in `docs/`. Previous phase reports, acceptance captures, design mockups, dated deployment overlays and superseded one-off scripts are preserved locally under ignored `backups/v1_cleanup_<timestamp>/archived/`; they are excluded from publication. Runtime data and installed applications were preserved.

See [release notes](../CHANGELOG.md), [development](DEVELOPMENT.md) and [GitHub publication](GITHUB_PUBLICATION.md). Prior local test evidence stays in ignored audit folders; rerun relevant checks for a new build or deployment.
