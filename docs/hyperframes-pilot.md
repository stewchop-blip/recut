# HyperFrames pilot for ReCut (disabled in production)

This is an optional **local motion-graphics provider**, not a replacement for
the existing Script Video FFmpeg assembly. It creates at most two short graphic
clips per **nonfiction explainer** before stock or paid-image asset selection.
It reuses the narration already in the plan and makes no additional AI calls.

## Safety and cost controls

- Default: SCRIPT_VIDEO_HYPERFRAMES_ENABLED=false.
- Standard Railway Dockerfile is unchanged. There are no extra installs or
  Chromium processes on the existing production deployment.
- Dockerfile.hyperframes pins HyperFrames CLI 0.8.174 and installs system
  Chromium. This is only an **opt-in pilot image**, not the main deployment.
- Narrative/fictional stories never enter the browser rendering path.
- The program creates fixed local HTML/CSS. The planner can only supply escaped
  text. No untrusted user JavaScript, remote stylesheets or arbitrary model HTML.
- Maximum 2 clips and 5 seconds each. One Chromium worker, a per-clip timeout
  and 20 MB cap. Script Video's shared render semaphore remains in effect.
- Subprocess receives minimal sanitized environment, excluding Telegram,
  OpenRouter, database credentials and other secrets.
- On failure, missing CLI, timeout, oversized output, or invalid ffprobe:
  restore the existing stock/image path, unchanged. No new interface buttons.
- No changes to the current AI-image ceiling or financial preflight.

## Verified container smoke, October 19, 2026

The separate GitHub Actions Docker smoke used the optional image and ran a
real ReCut Python adapter -> HyperFrames CLI -> Chromium -> MP4 render in a
container limited to 1 GiB RAM and 2 CPUs, without network access or
OpenRouter credentials. A short 2.4-second composition rendered successfully.

- Render wall time: 4.29 seconds on the CI machine.
- Approximate peak RSS of Python and subprocess tree: 194.8 MiB.
- Encoded MP4: 182,374 bytes; measured duration 2.417 seconds.
- Full optional Docker image: 2,132,528,391 bytes (about 2.13 GB).
- Paid AI/TTS API calls for graphic: none.
- Browser override: HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium.

This is not a full production load test. Peak memory is a process-tree sample,
not a guarantee of total host/cgroup memory, and simultaneous media tasks,
Cloud CPU pricing, image storage and Railway limits may change operational
cost. Do not change the existing plan or production Dockerfile automatically.

## Restricted end-to-end pilot (2026-10-20)

This experimental branch is **not enabled on the live Railway service**.
The default production Dockerfile still has no HyperFrames/Chromium.
The pilot adds an explicit per-request gate inside the existing Script Video
resolver: only users in `settings.internal_user_ids` can reach the browser
renderer. Public users receive the unchanged FFmpeg pipeline even when the
global feature flag is true. Internal IDs comprise the configured
`ALLOWED_TELEGRAM_USER_IDS`, `ADMIN_TELEGRAM_IDS`,
`TESTER_TELEGRAM_IDS`, and `UNLIMITED_TELEGRAM_IDS`.

To run a Railway pilot after reviewing its available memory and CPU:

1. Deploy the reviewed pilot revision. Point the Railway build to
   `Dockerfile.hyperframes` (instead of `Dockerfile`).
   This installs Chromium and the pinned CLI without runtime downloads.
2. Configure Railway variable `SCRIPT_VIDEO_HYPERFRAMES_ENABLED=true`.
   Keep `MAX_CONCURRENT_JOBS=1`; do not add public users to the internal list.
3. Confirm `HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium` inside that image.
   No new Telegram buttons or user-visible switches are needed.
4. Submit the *same nonfiction explainer* from one internal tester twice,
   first with the feature flag false (baseline), then true (pilot).
   Real stock footage and AI keyframes may vary between runs: compare
   on-screen quality but interpret wall-time comparisons accordingly.
5. Compare Railway CPU and RAM charts, full generation `elapsed_seconds`,
   per-graphic `elapsed_seconds`, `peak_process_tree_rss_mb`,
   `sampled_cpu_seconds`, and the final `graphics_videos` count.
   The process samples are approximate and exclude other service processes.
6. If `graphics_videos=0`, do **not** label the result a HyperFrames test:
   inspect pilot selection and renderer fallback events. Revert immediately
   on OOM, instability, queue delays, or excessively slow render.

Rollback: set `SCRIPT_VIDEO_HYPERFRAMES_ENABLED=false`; later restore the
standard production Dockerfile. The flag is the immediate runtime kill switch
for the already-running pilot image.

## Isolated smoke test

Do not change the live Railway project's Dockerfile until RAM, CPU, image
build size and running costs are measured. Chromium can require appreciable
RAM and disk, potentially increasing hosting costs despite zero API spend.

On a machine with Docker and network access:

    docker build -f Dockerfile.hyperframes -t recut-hyperframes-pilot .

Check the installed CLI/browser with the image's hyperframes doctor command,
then run a 2-3 second synthetic card (no API keys, Telegram or external media).
Inspect the resulting MP4 with ffprobe and review a few frames. Test success
and intentional CLI failure. Do not enable in live workloads yet.

Opt-in experimental environment:

    SCRIPT_VIDEO_HYPERFRAMES_ENABLED=true
    SCRIPT_VIDEO_HYPERFRAMES_TIMEOUT_SECONDS=75

CI tests in tests/test_script_video_hyperframes.py use offline mocks. Their
success does NOT establish that Chromium runs correctly on Railway hardware.
That needs the separate smoke test.

## Rollback

Set SCRIPT_VIDEO_HYPERFRAMES_ENABLED=false. Or restore the standard Dockerfile.
No changes to downloader, Telegram UX, settings, TTS, subtitles, database schema
or the existing fallback renderer.
