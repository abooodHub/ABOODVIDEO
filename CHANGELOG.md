# Release notes

## 1.1.0 — 2026-10-19

- Arabic/English selection in Platform administration, login, player controls and Windows Studio.
- Saved language preference; Arabic RTL sidebar on the right and English LTR sidebar on the left, including responsive layouts.
- Shared UI message catalog, localized forms, system messages, accessibility labels and native picker captions.
- User titles, filenames, subtitle content, authentication and private-media access remain unchanged.
- Bilingual Windows installer, English README, Arabic README and screenshots in both languages.
- Localization validation and browser coverage. No database migration or media changes.

## 1.0.0 — 2026-10-19

First unified source release of ABOODVIDEO Platform and ABOODVIDEO Studio.

- Platform administration, upload, encoder settings, media library and protected HLS/MP4 playback.
- Windows Studio episode import, persistent queue, explicit processing controls, subtitles/fonts, packaging and resumable upload.
- Empty 404 responses for the public homepage and closed catalog routes.
- Separate server-conversion and prepared-media deployment modes.
- Isolated test runner, CI, dependency pins, credential checks and third-party license notices.
- Consistent 1.0.0 product metadata and 1.0.0.0 Windows executable metadata.
- Current documentation and brand sources retained; obsolete phase documents, prototypes and one-off scripts archived outside published source.

Internal package schemas, API paths, migration identifiers, installer application identity and existing user data are preserved. See [development and build instructions](docs/DEVELOPMENT.md) to produce artifacts from this source.
