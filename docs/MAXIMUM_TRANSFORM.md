# Maximum Transform

After uploading a source: **Ещё → Максимальная обработка → Максимальная обработка**.
The first button selects the mode, the second starts the existing QuickPrep pipeline.
The same source can be reused through CurrentMedia. No extra effect settings.

Four content-hash-selected profiles combine contain layout (pip/framed), modest
brightness/contrast/saturation changes, light sharpening and grain, shared faint
twinkles, speed 0.98–1.03, dynamic EQ and pitch ±0.20–0.25 semitones. Audio tempo
compensates pitch and follows video speed. No audio is synthesized for silent inputs;
the user's mute setting is respected. User background/title/brand/banner settings
remain applicable. Banner timing follows the transformed duration.

No blind crop/mirror, no claimed text/face detection. Source bytes determine the
profile, independent of filename or Python process. Repeating the source repeats the
profile. Profile values are server-owned; users cannot inject FFmpeg filters.
There is no claim of bypassing platform originality checks or improving reach.

Tests cover menu/action routing, reused media, handler-to-pipeline handoff, process
stable profile selection, four actual FFmpeg renders (portrait/landscape/square,
with/without audio), measured pitch and A/V duration, and full pipeline with banner.
These do not replace a live Telegram smoke test after Railway deployment.
