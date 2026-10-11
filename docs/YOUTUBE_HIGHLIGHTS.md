# YouTube highlights beta

Open `/start` → «✂️ Найти лучшие моменты» → send a YouTube link → confirm.
Old Telegram messages retain their old keyboards; `/start` creates the new menu.
The explicit mode intercepts links before the ordinary full-video downloader.
Ordinary short-video handling remains available through «✨ Сделать ролик».

## Scope and reuse

Public, completed videos from 2 to 60 minutes, with readable manual or automatic
JSON3 captions and a muxed AVC/AAC MPEG-TS HLS VOD rendition at up to 720p.
Manual captions are tried first. Automatic translated tracks are excluded.
No audio transcription fallback is enabled in this beta. Selection is based on
speech, not visual action. Videos without an eligible source fail gracefully.

All transcript segments enter bounded analysis chunks (up to 12); candidate
summaries are ranked in one further call if necessary. Up to three 20–60-second
clips are selected. Empty model output never substitutes the full source.
Existing Analyser, boundary adjustment, QuickPrep appearance, subtitles,
branding, TelegramSender, render queue and generation accounting are reused.
Saved subtitles use the supplied captions instead of running Whisper again.

Only HLS segments intersecting validated clip ranges are fetched. FFmpeg gets
local files only. Encryption, live/discontinuous playlists, byte ranges,
separate audio and full HTTP-resource fallbacks are deliberately unsupported.
Canonical YouTube intake, HTTPS host allowlists, public DNS answers and disabled
redirects protect the new media fetch path. yt-dlp is used for metadata only.

## Limits and rollback

`YOUTUBE_HIGHLIGHTS_ENABLED=false` disables new requests without schema changes.
Defaults: `YOUTUBE_HIGHLIGHTS_MAX_MINUTES=60`,
`YOUTUBE_HIGHLIGHTS_MAX_MB=96`, `YOUTUBE_HIGHLIGHTS_TIMEOUT_SECONDS=900`.
The existing global render queue serializes processing. Cancellation also works
while queued. Pending links expire after 30 minutes; stale buttons are rejected.
One successful request consumes one generation, including partial delivery;
failed requests with no delivery are refunded by the existing middleware.

Metadata subprocess: 60 seconds, 8 MiB stdout cap. Individual HTTP requests:
30 seconds; playlist 1 MiB, caption body 2 MiB, media segment 8 MiB.
Received HTTP body chunks are counted, including failed media transfers.
The aggregate body budget can overshoot by one 16 KiB read before aborting.
These counters exclude HTTP/TLS overhead, transport buffering, yt-dlp metadata
traffic and outgoing Telegram uploads; they are not total network billing.
512 MiB free disk is required at admission. Clips are rendered sequentially;
temporary job files are removed on success, failure and cancellation.
Cancelled/timed-out FFmpeg children are killed and reaped.

## Verification, 2026-10-11

Full suite: **495 passed**. Additional focused run after the final cancellation
and cleanup checks: **48 passed**. Syntax compilation and whitespace checks pass.
Coverage includes URL/DNS restrictions, malformed playlists, 10/30/60-minute
timelines, manual/automatic captions, whole-transcript analysis, invalid times,
no full-download fallback, transfer limits, cancellation, cleanup, duplicate
requests, stale callbacks and reuse of captions without Whisper.

A real local HTTP server served an FFmpeg-generated 120-second HLS video.
Requesting seconds 41–51 fetched only the selected segments: **371,112 bytes**
versus **3,722,024 bytes** for all segments (9.97%). Output: **10.0 seconds**, with
audio. Server request logs and client body counters agreed. This is a real
selective-transfer test, **not a successful live YouTube test**.

Live YouTube metadata was retrievable here, but the video CDN returned a
195-byte “Site Unavailable” response. End-to-end YouTube delivery and Railway
resource measurements remain unverified. The beta fails closed in that case.
GitHub push triggers the existing Railway integration; it does not by itself
prove deployment completion or Telegram delivery.

## Resource planning (estimates, not production measurements)

At an illustrative 700–1,200 transcript characters per minute:

| Source duration | Transcript text | Analysis work |
| --- | --- | --- |
| 10 min | 7–12k characters | roughly 1–2 chunks, possibly a ranking call |
| 30 min | 21–36k characters | roughly 3–5 chunks, possibly a ranking call |
| 60 min | 42–72k characters | roughly 6–10 chunks plus ranking |

Timestamp/segment overhead can increase these counts. The enforced maximum is
12 chunks plus one ranking call, with the existing retry limit per call.
OpenRouter logs actual prompt/completion tokens; monetary cost depends on the
configured model and its current rates. STT usage is zero in this path.

Three 30–60-second clips at an illustrative 1–3 Mbit/s require approximately
11–68 MB of media, plus HLS boundary overhead. Source duration does not multiply
the selected media duration. The 96 MiB body budget remains authoritative.
Actual FFmpeg CPU, peak RAM, Railway cost and wall time require production
measurements; the 15-minute timeout is a ceiling, not a speed estimate.
