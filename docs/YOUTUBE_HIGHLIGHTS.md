# YouTube highlights beta

Metadata follow-up: the server image includes Node 22 and bundled yt-dlp EJS;
the metadata subprocess explicitly enables Node. See the upstream setup guide:
https://github.com/yt-dlp/yt-dlp/wiki/EJS . Bounded stderr is drained concurrently
and classified into fixed error codes (authentication, rate limit, runtime,
unavailable video, network, unsupported format). Raw stderr is never logged.
The uploaded production log for job 427 contained only `YOUTUBE_UNAVAILABLE`
and zero downloaded bytes; it cannot prove which upstream failure occurred.
The missing runtime is fixed, but that alone does not prove the source will
be accessible from Railway. A fresh request is needed to establish that.

## Indexed MP4 follow-up

When muxed HLS is absent, HTTP(S) AVC MP4 at up to 720p is supported through a
local range bridge. Separate AAC audio is supported; original-language audio
and captions are preferred over dubbed tracks. The MP4 header is checked before
handing the opaque loopback URL to FFmpeg. Remote URLs never reach FFmpeg.

FFmpeg seeks using the container index. Upstream reads use explicit ranges of
at most 64 KiB through the existing body counter. HTTP 200 instead of 206, wrong
Content-Range, changed total size or malformed data fails closed. Initial index
reads and bounded read-ahead are included; the method does not fetch exclusively
the exact compressed samples of the selected seconds. Unique byte coverage is
limited to 60% per source track across the whole job, in addition to the 96 MiB
received-body budget. Complete-resource fallback is impossible in this path.
512 range reads are allowed per clip. The bridge binds loopback only, uses an
unguessable token, disables access logs and stops handlers on disconnect.

Real local HTTP + FFmpeg tests on a 120-second MP4 produced a 10-second clip:
1,114,112 / 7,799,004 bytes for muxed media and 1,572,864 / 7,779,562 bytes for
separate tracks (14.3% / 20.2%). Byte totals vary with read-ahead scheduling.
These tests validate range seeking and accounting, not live YouTube access.
After this change the full suite passed: **516 tests**, with 9 existing warnings.

The concrete source `WTizYlHNJhQ` returned metadata locally: 1,220 seconds,
original Russian captions/audio, ordinary HTTPS MP4 renditions, no HLS.
The additional `web_embedded` client is explicitly enabled. A successful
metadata request here does not establish access from Railway. The supplied
Railway log for job 432 conclusively classified its request as
`YOUTUBE_AUTH_REQUIRED`; runtime installation did not fix that upstream gate.

Optional `YOUTUBE_PROXY_URL` accepts an operator-owned HTTP(S) proxy origin.
It is applied consistently to yt-dlp metadata, captions and all media ranges;
credentials are held as SecretStr and never printed. It defaults to empty.
No proxy was purchased or configured by this change. If anonymous clients remain
blocked from Railway, a working network exit must be supplied and tested before
claiming successful end-to-end YouTube processing. Do not set a random public
proxy or transfer signed media URLs between different exits.

Open `/start` → «✂️ Найти лучшие моменты» → send a YouTube link → confirm.
Old Telegram messages retain their old keyboards; `/start` creates the new menu.
The explicit mode intercepts links before the ordinary full-video downloader.
Ordinary short-video handling remains available through «✨ Сделать ролик».

## Scope and reuse

Public, completed videos from 2 to 60 minutes, with readable manual or automatic
JSON3 captions and AVC/AAC MPEG-TS HLS or indexed MP4 media at up to 720p.
Manual captions are tried first. Automatic translated tracks are excluded.
No audio transcription fallback is enabled in this beta. Selection is based on
speech, not visual action. Videos without an eligible source fail gracefully.

All transcript segments enter bounded analysis chunks (up to 12); candidate
summaries are ranked in one further call if necessary. Up to three 20–60-second
clips are selected. Empty model output never substitutes the full source.
Existing Analyser, boundary adjustment, QuickPrep appearance, subtitles,
branding, TelegramSender, render queue and generation accounting are reused.
Saved subtitles use the supplied captions instead of running Whisper again.

HLS fetches only segments intersecting validated clip ranges. The MP4 bridge
fetches indices and selected ranges with bounded read-ahead. FFmpeg gets local
files or an opaque loopback URL. Encryption and live/discontinuous playlists
are unsupported. Whole HTTP-resource fallback is deliberately unsupported.
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
