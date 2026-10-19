# Disposable real integration tests

Run from `C:\aboodvideo` with the existing virtual environment and Docker Engine:

```powershell
.venv\Scripts\python.exe scripts\run_real_integration.py
```

Run only physical disk failure and public HTTPS tests:

```powershell
.venv\Scripts\python.exe scripts\run_real_integration.py tests/test_real_platform_faults.py -q
```

The runner creates its own PostgreSQL, Redis, network and storage, executes the
existing isolated pytest runner, and removes only its owned Docker resources.
Existing resources with conflicting names or ports cause a refusal to start.
No local `.env`, real Studio profile or production credentials are used.
Production browser acceptance is excluded and its opt-in is disabled.
Results and JUnit XML are retained in the printed `INTEGRATION_EVIDENCE` directory.

The disk tests fill only a 64 MiB container tmpfs. They verify HTTP 507, unchanged
acknowledged data and offsets, bounded cleanup, and successful retries after space
is released. They cover source chunks, source finalization, package files,
archive chunks and extraction.

The TLS test uses a short-lived localhost certificate trusted by that HTTP client
only. Certificate and hostname verification remain enabled. The public-mode API
trusts exactly the configured isolated Nginx IP; production settings are unchanged.
It checks secure cookies, CSRF, HLS delivery, header spoofing, and loss of preview
authority after logout, account deactivation or ownership changes.

The complete suite also needs the existing Windows FFmpeg/FFprobe, Chromium and
a working GPU encoder for its explicit GPU test. Missing capabilities must be
reported rather than silently treated as successful GPU execution.

## Real-media staging and measured load

Prepare **read-only originals** as bounded stream-copy excerpts (the script creates
a new temporary folder; it never encodes, uploads or changes the originals):

```powershell
.venv\Scripts\python.exe scripts\prepare_staging_media.py --h264-ass '<real-4k-h264.mkv>' --hevc-multiaudio '<real-4k-multiple-audio.mkv>' --hevc-mp4 '<real-4k-hevc.mp4>' --srt '<existing.srt>'
$env:ABV_STAGING_MEDIA_MANIFEST = '<printed staging-media.json path>'
.venv\Scripts\python.exe scripts\run_real_integration.py tests/test_staging_readiness.py -q --tb=short --show-capture=no
```

Use the same environment variable with the default full-suite invocation to run
the staging cases alongside regression tests. Without an explicit manifest the
real-media cases are skipped, never replaced by synthetic results.

The staging module uses `medium` server encoding, the repository's CPU/memory
envelope (API 1 CPU/1 GiB; each worker 8 CPUs/3 GiB; Nginx 0.5 CPU/256 MiB),
real source interruption, duplicate/resumed/concurrent uploads, Redis RDB recovery,
native Studio GPU 4K output, original ASS/font extraction and a real SRT. The SRT
is a separate compatibility fixture, not a translation matching the video.

Chromium plays actual Nginx-served HLS at 360/720/2160, seeks and selects subtitles.
Measured load uses independent playback principals behind one NAT IP, including
24 paced viewers and an accelerated burst. It records response codes, latency,
throughput, container/native encoder memory and cgroup OOM counters. Nginx's
existing 30 requests/second/IP limit is retained; burst rejections are reported
separately. These short measurements do not prove long-duration leak freedom,
WAN throughput, other GPU compatibility, or full-episode timing/translation fidelity.
The independent verified-TLS/cookie/CSRF cases remain in `test_real_platform_faults.py`.
Original media size/mtime and SRT checksum are checked again at teardown.
The preparer also accepts `--hevc10-mkv '<existing 4K Main 10 MKV>'`.
When present in the manifest, the source-upload/browser matrix adds this real
10-bit case and verifies the High 10 codec declaration against the encoded output.
PostgreSQL and Redis images, Redis memory policy and snapshot schedule match
the repository Compose configuration; dependency image IDs are recorded.

To test an already built repository Dockerfile image, set `ABV_INTEGRATION_IMAGE`
to its unique `abv-staging-candidate:<tag>` name. It must already exist and have
the `abv.purpose=real-integration` label. The runner then uses baked application
code instead of source bind mounts; the staging test checks API/worker image IDs
and critical source hashes. `ABV_STAGING_NGINX_IMAGE` selects the corresponding
locally built Nginx image. Build contexts should contain only application source,
requirements, migrations, tests and the two required bootstrap scripts, with no
`.env`, storage, local Studio profile, credentials or user media.

## Full-length stability opt-in

`test_full_length_stability.py` requires `ABV_FULL_LENGTH_ROOT`: a new evidence
directory with `real-originals.json` containing existing `h264-ass` and `hevc-mp4`
original paths, durations (20–60 minutes), sizes and `mtime_ns`. It uses whole
originals read-only, not excerpt substitution. Set `ABV_INSTALLED_STUDIO_EXE` to
the existing installed executable and install `full_length_requirements.txt` into
that evidence directory's `test-dependencies/` for independent subtitle parsing.
The original excerpt manifest is also required for the genuine SRT fixture.

This opt-in launches the installed Studio with a separate profile/home/output
and loopback port 28557, processes a complete ASS episode and uploads its package,
while two whole-source uploads/worker jobs run concurrently. It measures WDDM
per-process GPU ownership, process CPU/RAM/I/O and Docker resources. It then checks
shared-IP legitimate viewers and session/IP abuse limits, full-duration real-time
Chromium playback, long ASS/SRT cue timings, resume and other available browsers.
Real devices and missing browser engines must not be reported as tested.

Audio contract: multi-audio inputs are supported as inputs, but each current HLS
rendition emits one selected/default audio stream as AAC stereo (48 kHz), subject
to the existing compatible AAC copy path. Alternate languages, all source audio
tracks and original surround layout are not preserved as selectable HLS tracks.
The stability changes do not alter this contract.
Transcoding currently leaves selection to FFmpeg's automatic stream selection;
the eligible Studio 2160p copy branch explicitly maps `0:a:0?`. There is no new
per-language audio-selection control or guarantee that every input track survives.

The full-duration browser observer runs at normal speed with audio muted so
the owner's other Windows media applications cannot take its audio focus. It
records play/pause/wait events, real media time, wall time, dropped frames and
failed playback responses. Muting does not replace video decode or HLS delivery.
Unavailable physical devices and an interrupted observer are not passing cases.

Playback traffic now has both per-session and aggregate per-IP Nginx ceilings;
the general API's existing limit is separate. NAT validation must exercise real
authenticated session URLs plus fabricated sessions/forwarded headers. A paced
HTTP viewer simulation must be described as such, not as 24 browser decoders.

An installed legacy Studio binary and a rebuilt portable candidate are separate
test targets. Source fixes or a successful portable build do not prove that the
owner's installed executable has been updated. Use independent test profiles,
record each binary hash, and never overwrite the real profile during validation.
