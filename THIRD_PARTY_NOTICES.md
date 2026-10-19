# Bundled third-party assets

The project license applies to original project code. The following components
retain their own licenses and notices. Do not remove these files from distribution.

## JASSUB 1.8.8

- Source and build materials: <https://github.com/ThaUnknown/jassub/tree/1.8.8>.
- Release package: <https://registry.npmjs.org/jassub/-/jassub-1.8.8.tgz>.
- Local assets/notices: `backend/app/static/vendor/jassub/`.
- `LICENSE` covers the JavaScript wrapper; `COPYRIGHT` contains the upstream
  notices and licenses for the WebAssembly renderer and its dependencies.
- The UMD wrapper, both WASM binaries and default WOFF2 font were matched byte for
  byte against the npm 1.8.8 release. File hashes and release integrity are recorded
  in `PROVENANCE.json` beside the assets.
- The worker JavaScript has an existing project patch replacing dynamic Function
  construction with closures for the site's Content Security Policy. The patch is
  retained in `patches/csp-dispatch.patch`; the renderer binaries are unmodified.
- Upstream components include LGPL-2.1-or-later, FreeType and other licensed code.
  Corresponding source and build scripts are linked by the upstream release;
  distributors must preserve the applicable source/relinking rights. The font's
  notice is included in upstream `COPYRIGHT`.

## Other browser assets

- jsVectorMap: retain `backend/app/static/vendor/jsvectormap/LICENSE`.
- IBM Plex Arabic fonts: retain the OFL notice under `backend/app/static/fonts/`.
- HLS.js 1.5.8 is served locally from `backend/app/static/js/vendor/hls.min.js`.
  Its bytes match the npm 1.5.8 release. Retain the adjacent `hls.LICENSE`,
  `hls.NOTICE` and `hls.PROVENANCE.json`; upstream is
  <https://github.com/video-dev/hls.js/tree/v1.5.8> (Apache-2.0).

Studio runtime data, extracted user fonts and uploaded media are not project
assets and are excluded from Git and release manifests. Supply external font
libraries only with the necessary distribution permissions. A build that bundles
FFmpeg also needs the notices and source rights for that exact FFmpeg build;
installing a binary locally does not grant redistribution rights automatically.

## FFmpeg distributions for 1.0.0

The Linux runtime copies FFmpeg/FFprobe 9.0.2 from the digest-pinned
`mwader/static-ffmpeg:9.0.2-1` build. Build recipes and component source URLs/hashes:
<https://github.com/wader/static-ffmpeg>. Provenance is retained in
`backend/vendor/ffmpeg/`; runtime component versions and upstream documentation
are under `/usr/share/doc/ffmpeg/`. The GPLv3 notice is retained there.

Studio builds use an intact supplied Windows distribution and retain its
`LICENSE` and `README.txt` under `_internal/licenses/ffmpeg`. The 1.0.0 release
uses Gyan's 9.0.2 essentials distribution, linked from the upstream FFmpeg download
page: <https://www.gyan.dev/ffmpeg/builds/>. Preserve its exact source reference and
applicable corresponding-source rights when distributing binaries.
