# API flow reference

The FastAPI application exposes an OpenAPI document at `/docs` in a configured development environment. The routes below summarize the package and playback flows; authorization requirements are enforced by the corresponding route.

| Flow | Route family | Purpose |
|---|---|---|
| Health/readiness | `GET /api/v1/health`, `GET /api/v1/health/readiness` | Liveness and dependency readiness checks |
| Video metadata | `/api/v1/videos/...` | Create and inspect library metadata |
| Viewer/watch | `/watch/{share_id}` and `/api/v1/watch/...` | Viewer page, metadata, and authorized playback setup |
| Archive upload init | `POST /api/v1/studio/packages/{session_id}/archive/init` | Start or resume a declared archive transfer |
| Archive chunk/resume | `PUT /api/v1/studio/packages/{session_id}/archive/chunk`, `GET /api/v1/studio/packages/{session_id}/archive/status` | Send bounded chunks and query the confirmed offset |
| Archive finalize | `POST /api/v1/studio/packages/{session_id}/archive/finalize` | Validate and finalize the staged package |
| Studio processing heartbeat | `POST /api/v1/studio/processing/heartbeat` | Accept path-free local Studio status with `packages:write` API-key scope |
| Admin Studio processing | `GET /api/v1/admin/transcodes/local-studio` | Read recent local Studio statuses for owner/admin sessions |
| Playback | `/api/v1/playback/{session_or_token}/master.m3u8` and `/api/v1/playback/{session_or_token}/{path}` | Authorize master/rendition playlists and media access |

Exact request and response schemas are defined in the FastAPI route modules and generated OpenAPI document. Administrative endpoints and credentials are intentionally not reproduced here.

The Studio heartbeat reports display metadata, stage, progress, quality, speed, and ETA. It must never contain local paths or credentials. Active reports expire after three minutes; terminal reports expire after one day. This API reports work performed by the local Studio and does not enqueue or start server-side transcodes.
