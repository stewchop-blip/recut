# Video engine verification — 20 October 2026

## Scope and method

Three previously supplied real exports were available for this check: one
informational video and two narrative videos. All supplied copies are H.264/AAC,
512×910, 30 FPS. They are already encoded Telegram copies, not original scene
assets. No media, prompts, account data or downloaded helpers are committed.

Each timed variant ran three times, sequentially on the same local host.
Tables show medians. Child CPU is user + system time from `RUSAGE_CHILDREN`;
peak RSS is the maximum child-process RSS, not total application memory.
These are isolated-stage local measurements, not full job or Railway metrics.
There were no paid API calls, model changes or new dependencies.

The ordinary uploaded-video case remains covered by synthetic integration
fixtures. A suitable older real clip could not be downloaded in this run.
The three real samples therefore cover two content categories, not all three
distinct categories requested in the original brief.

## Source normalization: preserve a clean existing encode

Baseline: `255fcaf` normalizer, which re-encodes every source. Candidate:
the stream-copy path shipped in `bd9336a`, with the same black-bar detector.
All three sources passed the no-crop, square-pixel, no-rotation checks.

| Sample | Duration | Wall, old → new | Child CPU, old → new | Peak RSS, old → new |
| --- | ---: | ---: | ---: | ---: |
| Informational | 32.87 s | 2.76 → 0.95 s | 9.59 → 2.65 s | 186.9 → 68.6 MiB |
| Narrative A | 39.20 s | 3.33 → 1.38 s | 11.92 → 3.76 s | 183.0 → 69.2 MiB |
| Narrative B | 40.93 s | 3.77 → 1.30 s | 13.85 → 3.94 s | 183.0 → 68.3 MiB |

Encoded video and audio stream hashes match the source for every new output.
Frame counts and 30 FPS are preserved. Baseline video SSIM against the input
was 0.993194, 0.985087 and 0.990496; the new path introduces no video encode.
Remux timebase rounding changed one audio duration by less than 1 ms.

This has a disk tradeoff. The intermediate retains the source bitrate rather
than compressing it again:

| Sample | Old normalized file | Stream-copy normalized file |
| --- | ---: | ---: |
| Informational | 2.66 MB | 17.15 MB |
| Narrative A | 4.20 MB | 22.28 MB |
| Narrative B | 4.09 MB | 24.09 MB |

Do not generalize the earlier synthetic fixture's file-size reduction to real
inputs. This intermediate is an extra source-sized copy; final delivery size
is determined by later render settings.

## Audio finalization: analyze audio only and encode at 48 kHz

The old loudness-measurement pass sent both decoded video and audio to the
null muxer. The candidate disables video, subtitle and data output during
measurement; the final video remains stream-copied as before.

Real exports also exposed 96 kHz AAC after dynamic `loudnorm`. FFmpeg documents
that dynamic normalization upsamples internally and recommends explicitly
setting the output sample rate:
[loudnorm documentation](https://ffmpeg.org/ffmpeg-filters.html#loudnorm).
New audio encodes use the shared 48 kHz constant in `AudioProcessor` and the
existing finalizer. Already compliant AAC that takes the stream-copy path is
preserved, including its original sample rate.

Baseline for this table is `bd9336a` finalization. Candidate combines
audio-only analysis and 48 kHz encoding. Both use the same loudness target,
AAC bitrate, source and video-copy path.

| Sample | Wall, old → new | Child CPU, old → new | Peak RSS, old → new |
| --- | ---: | ---: | ---: |
| Informational | 2.40 → 1.74 s | 4.55 → 1.75 s | 129.1 → 94.4 MiB |
| Narrative A | 2.83 → 2.01 s | 5.61 → 2.03 s | 129.3 → 93.8 MiB |
| Narrative B | 3.00 → 2.08 s | 6.31 → 2.10 s | 128.0 → 91.3 MiB |

The video stream hash is unchanged in all cases. New audio is AAC at 48 kHz.
Final format durations match the baseline (32.90, 39.30 and 41.00 seconds),
so this change adds no timing delta. File-size changes are below 0.05%.

| Sample | Integrated loudness, old → new | True peak, old → new |
| --- | ---: | ---: |
| Informational | -16.23 → -16.22 LUFS | +1.21 → -1.40 dBTP |
| Narrative A | -16.32 → -16.30 LUFS | -1.28 → -1.02 dBTP |
| Narrative B | -16.25 → -16.24 LUFS | -0.79 → -1.31 dBTP |

The new outputs were all below 0 dBTP on these samples. This is measured
sample evidence, not a guarantee of a hard post-AAC peak ceiling for all audio.

## Script Video output gate

The regular QuickPrep flow already verifies its final media with the shared
FFprobeService. Script Video now reuses that service after FFmpeg succeeds,
instead of accepting a nonempty file alone. It checks a readable video,
expected canvas, H.264 + AAC, audio presence, square pixels, no rotation,
expected FPS and duration within 0.5 seconds of narration.

This is one metadata probe, not another full decode or transcription. The
regression test simulates a successful encoder that leaves a nonempty invalid
MP4 and confirms that it is rejected before delivery. The existing real FFmpeg
assembler test checks the successful 1080×1920 path and 48 kHz AAC.

## Regression checks

The full local suite passed: **582 tests**, with 9 existing datetime
deprecation warnings. The local environment lacks `faster-whisper`; its import
surface was supplied by a temporary test-only stub for wiring tests. No actual
Whisper model was loaded, and no dependency/stub was added to the project.
Python compilation and `git diff --check` also passed.

## Boundaries

- No changes to Telegram UX, PRO rights, limits, queue admission or providers.
- No new music/SFX, automatic silence cutting, public HyperFrames rollout or
  cover generator. No licensed audio library is bundled, and content intent
  cannot be inferred safely enough to remove pauses or add story graphics.
- No new source video was generated. Planner relevance, character consistency,
  fresh TTS/model output and real network downloader availability were not
  revalidated by this local media comparison.
- The previous Script Video intermediate-CRF benchmark remains synthetic;
  the supplied finished MP4s cannot reconstruct its original image assets.
