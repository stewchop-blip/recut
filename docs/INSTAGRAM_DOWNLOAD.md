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

### Follow-up: Railway jobs 342–343

The alternate path started, but both attempts ended with ValueError in ~0.15s.
The old log omitted the fixed error reason, so the failing stage is unknown.
Allow at most two HTTPS redirects within www.instagram.com for the initial
public page (the original adapter rejected every redirect). External origins,
custom ports and HTTP are rejected; GraphQL POST still never follows redirects.
Fixed diagnostic codes now distinguish homepage HTTP/redirect/token failures,
GraphQL HTTP/JSON failure and empty/mismatched/non-video metadata, without logging
response bodies, session values or signed URLs. This is a bounded redirect
handling correction plus diagnostic improvement, not confirmed Railway recovery.

### Follow-up: job 346 returned metadata_empty

The query completed but the expected v1 item was missing. That alone cannot
establish IP blocking. Match anonymous CSRF cookies with X-CSRFToken, include
fb_dtsg only when supplied by the public page, and support the shortcode-media
response shapes also handled by instagrapi. No personal session is added.
Differentiate GraphQL errors (numeric codes only), missing data, missing items
and unknown schema; never log arbitrary error text or response bodies.
Local full download of DXRR2sziOG_ still succeeds after the request changes;
production recovery remains unconfirmed until a Railway request succeeds.

## 2026-10-08: staged anonymous metadata fallback

Railway returned `graphql_errors_1675004` after anonymous yt-dlp login redirects.
That does not prove an IP block. Replace the single alternate-query attempt with:

1. Existing yt-dlp metadata extraction.
2. GET `https://i.instagram.com/api/v1/oembed/?url=https://www.instagram.com/p/{shortcode}/`
   to obtain a validated numeric `media_id` (optionally suffixed with `_owner_id`).
3. If an ID exists, GET `https://i.instagram.com/api/v1/media/{media_id}/info/`;
   consume `items[0].video_versions`. Without an ID, skip this step.
4. GET `https://www.instagram.com/p/{shortcode}/embed/captioned/`; decode the JSON
   init argument and its `contextJSON`, accepting `gql_data.shortcode_media`,
   `gql_data.xdt_shortcode_media`, the existing v1 item wrapper, or a mobile item.
5. Existing anonymous homepage/session setup and `PolarisPostRootQuery` POST to
   `https://www.instagram.com/api/graphql` with unchanged doc_id.
6. If all metadata paths fail, preserve the structured Instagram error.

This changes only Instagram's existing anonymous fallback eligibility. Explicit
429/restriction failures and configured account-session failures retain their
previous behavior. TikTok and uploaded-file pipelines are unchanged.

Each public request has a 10-second timeout (homepage redirects: 5 seconds each,
three requests maximum). Each oEmbed/mobile/embed stage has a 12-second wall-clock
bound; the GraphQL stage has 30 seconds. The entire public fallback has 75 seconds.
No stage retries. Redirects are disabled except the existing bounded same-origin
homepage logic. Cancellation propagates. No credentials, bearer, user cookies,
proxy, service, dependency or infrastructure changes are introduced.

Mobile requests send the Instagram Android user-agent, Accept/Accept-Language and
locale headers. Embed uses browser impersonation with HTML Accept/Accept-Language.
We do not add Cobalt's transport/client-IP headers or pretend they are necessary.
Only anonymous cookies issued by Instagram itself can accumulate in the request
session. Existing GraphQL CSRF/LSD/fb_dtsg handling stays unchanged and runs last.

Metadata validates post identity/privacy and HTTPS media URLs on subdomains of
cdninstagram.com or fbcdn.net, without userinfo or custom ports. Valid video
versions are ordered by pixel area, retaining the highest available reasonable
quality (each dimension at most 4096); existing yt-dlp selection and transfer
limits remain in effect. Returned metadata uses the existing owner-only temporary
`--load-info-json` file and cleanup. This host check validates returned media URLs;
it does not add a new redirect validator to yt-dlp's existing CDN transport.

Stage logs: `instagram_public_stage_start`, `instagram_public_stage_ok`,
`instagram_public_stage_failed`, and `instagram_public_stage_skipped`, with stage
and fixed reason only. Reasons include `oembed_http_*`, `oembed_no_media_id`,
`mobile_info_http_*`, `mobile_info_empty`, `embed_http_*`, `embed_parse_failed`,
`embed_no_video`, `graphql_errors_*`, and stage-specific timeout/transport reasons.
Body contents, signed URLs and session tokens are never added to these logs.
Instagram subprocess failures now retain only their fixed structured code,
including in exceptions subsequently consumed by job logs.

### Source review and licensing

- Cobalt Instagram service (inspected on 2026-10-08):
  https://github.com/imputnet/cobalt/blob/main/api/src/processing/services/instagram.js
  Latest commit affecting that file in the source history reviewed:
  a6240d0192053c8fef2e2642a14017862bdcaa7f (2025-04-02).
- Cobalt license: https://github.com/imputnet/cobalt/blob/main/LICENSE (AGPL-3.0).
  ReCut independently implements the endpoint sequence and response schema;
  no Cobalt source code, client library or dependencies are incorporated.
- Current instagrapi documentation still describes oEmbed media IDs and labels
  mobile `media_info_v1` as private API:
  https://github.com/subzeroid/instagrapi/blob/master/docs/usage-guide/media.md
  License: https://github.com/subzeroid/instagrapi/blob/master/LICENSE (MIT).
  Its existing public-query implementation remains the reference for the final
  GraphQL stage. Source review did not establish a newer open-source guarantee
  that anonymous mobile info works on every server; it is a best-effort stage.

### Live verification (workspace, not Railway)

For DXRR2sziOG_, without an account or personal cookies:
- oEmbed HTTP 200 returned a rich JSON object with a string `media_id`, `html`,
  author/provider/thumbnail fields, dimensions, title, type and version.
- Mobile info returned HTTP 403. Do not claim this private mobile endpoint is
  generally accessible anonymously.
- Embed HTTP 200 contained `contextJSON` with `context` and
  `gql_data.shortcode_media`. It returned usable video metadata; GraphQL was skipped.
- With yt-dlp metadata failure deliberately injected, the real fallback and
  `--load-info-json` transfer downloaded 3,451,476 bytes. ffprobe confirmed
  1276x720 video, audio, and 16.552971-second duration.
- Default yt-dlp transfer first encountered the workspace's certificate trust
  issue. Only the local verification command used `--compat-options no-certifi`
  to use the system CA with TLS verification enabled; no production CA override
  is shipped.

After GitHub auto-deploy, submit that Reel to the bot and inspect the same job:
`yt_dlp_metadata_failed` -> public stage events -> `instagram_public_fallback_ok`
-> `url_download_file_created` -> successful Telegram delivery. An intermediate
`mobile_info_http_403` is acceptable if embed succeeds. If all methods fail,
collect stage/reason codes and the final structured error. Local success is not
Railway runtime or Telegram delivery verification.

Validation: 293 tests passed, including new fallback and diagnostic cases. Syntax compilation and
focused undefined/unused import checks passed. The suite emitted existing datetime
deprecations and an unrelated SQLite test-thread cleanup warning; no failed tests.
