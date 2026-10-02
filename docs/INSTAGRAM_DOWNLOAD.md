# Instagram downloader repair (2026-10-02)

## Confirmed code issues

- Dependencies only requested yt-dlp's `default` extra. Instagram's 2026.08.19
  extractor enables the logged-out GraphQL path only when a supported browser
  transport is available. Install the documented `curl-cffi` extra and pin the
  tested stable version. Changing pyproject also invalidates Docker's dependency
  layer. Docker now propagates pip failure instead of masking it with `tail`.
- The bot extracted the same post twice in separate processes: once for limits
  and metadata, once to download. Instagram now reuses the extracted media info
  via yt-dlp's documented `--load-info-json`. This avoids another post extraction
  and retains the successful authenticated cookie context when one was needed.
- Generic yt-dlp cookie advice was incorrectly labelled AUTH_REQUIRED. Distinguish
  timeout, explicit 429/rate limiting, explicit login/restriction and ambiguous
  extractor/access failure. The user-facing message no longer guesses that every
  failed public Reel is audience-restricted.
- Remove `stkn` tracking parameters. Select only finished `download.*` video files,
  not partial downloads, auxiliary JSON or unrelated assets.

The extracted JSON can contain signed URLs: keep it in an owner-only temporary
file and remove it, along with temporary cookies, on success, failure and cancellation.
Retries use only cookies already configured by the operator, at most once for
metadata and never repeat the same authenticated download blindly. No account,
proxy, external download service or production secret is added.

## Sources

- https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/instagram.py
- https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/README.md
- https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19

An Instagram-side restriction tied to the Railway IP or session can still require
operator action. Local successful downloads do not establish that every Reel or
every server address will work. User screenshots alone do not reveal the server's
raw error, package version or cookie configuration.

## Verification performed

218 tests passed. A real anonymous download of the reported Reel
`DaoaZyPOCBG` succeeded: 11,421,116 bytes, 1440×2560, 11.17 seconds, video and audio
confirmed with ffprobe. This workspace's local test used `--compat-options
no-certifi` to honour its existing system CA configuration after the default
certifi bundle did not trust the workspace certificate. TLS verification stayed
on. That workspace-only option is not part of the production change. The live
source/asset test used no Instagram cookies; Railway still needs runtime verification.
