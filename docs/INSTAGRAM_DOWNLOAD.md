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

## 2026-10-03: login redirects versus rate limits

The upstream extractor calls a redirect to Instagram login an anonymous rate limit.
This is not evidence of HTTP 429. Classify that redirect as AUTH_REQUIRED and reserve
RATE_LIMITED for explicit 429 / Too Many Requests responses. Configured Instagram
cookies now accompany the first request and are reused for the media transfer;
failed authenticated requests are not immediately repeated with identical cookies.
Instagram metadata requests use a one-second inter-request pause.

The reported reel Dd332nVRB_3 downloaded completely (7,460,325 bytes) from the
verification environment without cookies. This does NOT verify the Railway IP or
its cookie configuration. If Railway continues receiving a login redirect, an
operator must check server logs and configure/refresh INSTAGRAM_COOKIES_B64 securely
in Railway; never send session cookies in chat or commit them. A true server-side
429 cannot be removed by this code change. Existing limits and private-cookie
cleanup remain enforced. Targeted download/security suite: 52 tests passed.

## 2026-10-07: alternative public web query

Railway job 339 still received a login redirect without an account session.
Added one anonymous `PolarisPostRootQuery` request using the public web query
shape currently used by instagrapi (`MEDIA_INFO_DOC_ID=27830990013244856`).
Sources: https://github.com/subzeroid/instagrapi/blob/master/instagrapi/mixins/media.py
and https://github.com/subzeroid/instagrapi/blob/master/instagrapi/mixins/public.py .
The implementation is a small independent adapter, not a copied client library.
It runs only after anonymous yt-dlp auth/access/extraction failure, never after
explicit 429, audience restriction or authenticated failure. No account cookies,
external service, or new dependency is required. Requests have time limits;
redirects are disabled; returned post identity, private flag and HTTPS CDN hosts
are checked. Existing yt-dlp file-size/duration limits and temporary cleanup remain.
Only success/error type is logged, not response bodies or signed media URLs.
This undocumented query can also stop working or be unavailable from Railway.

Validation: 22 targeted tests passed. The new public query downloaded the user's
DXRR2sziOG_ reel in full: 3,451,476 bytes, 1276x720, 16.55 seconds with audio.
The local verification used the workspace's system CA (TLS verification stayed
on); no CA override is shipped. Railway behavior still needs a user request.
