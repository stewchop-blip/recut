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
