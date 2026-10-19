# Architecture

ABOODVIDEO has two cooperating products. The Windows Studio prepares packages locally; the Platform validates, stores, and serves them. The ABOODVIDEO WordPress site is an external consumer and is not part of this repository.

## Processing and playback flow

```text
Windows Studio → probe and encode/remux with FFmpeg → HLS package
  → validate manifest and package → remove safe intermediate files
  → create final-files-only TAR → resumable archive upload
  → Platform staging and validation → READY
  → viewer/watch page → HLS player
```

Studio processing and upload are explicit queue actions. A 4K source can be copied/remuxed when the compatibility gate passes; otherwise it is encoded. The 1080p, 720p, and 480p ladder follows the selected Economy, Balanced, or Quality profile. 360p is optional and off by default. ASS/SSA may remain soft subtitles. Subtitle styles and required embedded/custom fonts are packaged when selected and available.

The local Studio queue and the Platform's server transcode queue remain separate execution systems. Studio reports sanitized, short-lived status to the Platform so administrators can see local probe, encode, package, upload, and verification progress beside server-side jobs. Heartbeats do not upload source paths, create server transcode jobs, or alter package state. The Platform accepts reports from a Studio API key with `packages:write`; active reports expire after three minutes and terminal reports after one day.

## Components

- `studio/` contains the Windows client, local FastAPI API, queue and batch persistence, encoder, subtitle/font handling, HLS packager, and resumable archive uploader.
- `backend/` contains the FastAPI Platform, SQLAlchemy models/services, API routes, workers, static player and administration pages, and Alembic migrations.
- PostgreSQL stores durable library, upload-session, and package metadata. Redis backs job queues, sessions, rate limits, and short-lived playback state.
- Nginx fronts the Platform and can serve validated media using internal redirects. The browser player consumes master and rendition HLS playlists.
- ASS rendering uses browser-side JASSUB/libass support where configured; renderer and font availability depend on the supported browser and package fonts.

Studio sends a finalized archive through the package archive init/chunk/finalize flow. The Platform stages it, validates the declared files and media, and only then marks the package READY. Playback uses viewer/watch authorization and the HLS playlists and segments associated with that READY package.
