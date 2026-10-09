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

### Follow-up: Railway job 358 (2026-10-08)

The deployed staged code ran for DeJRGZmsFN5. Actual reasons:
`oembed_http_401` -> mobile info skipped -> `embed_http_302` ->
`graphql_errors_1675004`. The previous log did not record the embed redirect
category, so its destination is unknown. This does not establish an IP block.
A startup Telegram getUpdates conflict also appeared; that is separate from the
Instagram request, which reached all logged metadata stages.

Fix two concrete limitations of the adapter:
- When oEmbed fails, derive the numeric media PK from the shortcode and make the
  independent mobile info attempt once. This is the standard Instagram ID codec,
  cross-checked against instagrapi `media_pk_from_code` and its documented examples:
  https://github.com/subzeroid/instagrapi/blob/master/instagrapi/mixins/media.py
  In the local live response, decoding DeJRGZmsFN5 gave 4001804950495581049,
  matching oEmbed's numeric PK. No owner ID or authentication is needed to compute it.
- Follow at most two embed redirects within HTTPS www.instagram.com, restricted
  to the same post's canonical/embed paths. Reject external origins, userinfo,
  custom ports, unrelated post IDs and paths. Keep the existing 12-second total
  embed-stage bound. A login redirect ends immediately with `embed_redirect_login`;
  challenge/suspension gives `embed_redirect_challenge`. Allowed transitions log
  only HTTP status and `target_kind=post|embed`, never Location or its query.
  `embed_redirect_disallowed`, `embed_redirect_path_disallowed` and
  `embed_redirect_limit` distinguish unsafe targets and loops.

GraphQL request shape/doc_id, headers, CDN validation, transfer limits and
infrastructure are unchanged. Login redirects are not treated as downloadable
media or followed to an account sign-in page. These changes enable previously
skipped attempts; they do not guarantee anonymous access from Railway.

Local anonymous verification of DeJRGZmsFN5 returned oEmbed HTTP 200, mobile info
HTTP 403, and embed HTTP 200 with usable metadata (duration 98.52 seconds). Thus
this Reel was available from the verification environment while Railway reported
401/302. The next production attempt must establish whether its redirect is a
canonical transition or a login wall and whether derived-ID mobile info works.

The full local fallback transfer also succeeded for DeJRGZmsFN5: 16,493,650 bytes,
720x1280 video with audio, ffprobe duration 98.635510 seconds. As before, only the
verification command used the system CA via `--compat-options no-certifi`; TLS
verification remained enabled and no production certificate settings changed.

Follow-up validation: 314 tests passed; syntax/import checks and diff whitespace
checks passed. No account cookies, secrets or infrastructure files were added.

### Follow-up: Railway job 360 — confirmed embed login wall

The last deployed version did run. For DeJRGZmsFN5 it returned:
`oembed_http_401` -> ID derived from shortcode -> `mobile_info_http_401` ->
`embed_redirect_login` -> `graphql_errors_1675004`. The embed 302 is now known
to be a login redirect, not an unhandled canonical transition. This establishes
that those anonymous paths did not expose this Reel from that Railway request;
it does not establish a permanent block of the entire IP or every public path.

Add a separate public logged-out post HTML stage **after embed, before GraphQL**.
GET `https://www.instagram.com/p/{shortcode}/` using a search-crawler user-agent
and consume only its inline `xig_polaris_media.if_not_gated_logged_out` media
object (or the equivalent direct media node). This is not another GraphQL header
or doc_id variant. The response contains actual video_versions, not just an
OpenGraph preview image. Keep existing shortcode/privacy/CDN checks and format
quality selection. A null gated node is rejected; no account gate is crossed.
No redirect is followed, no network request is made for a returned manifest.
The stage has the same 10-second request / 12-second wall-clock limits as other
public stages. Total fallback bound is now 90 seconds for all five public stages.

Some HTML media objects omit video_duration. Read the finite ISO time duration
attribute from the inline video_dash_manifest so the existing pre-transfer
length check still applies. For the reported Reel this is PT98.520813S. An absent
or invalid duration stays unknown, as in the existing metadata mechanisms; file
size and transfer timeout limits always remain active.

New diagnostics: `post_html_http_*`, `post_html_redirect_login`,
`post_html_media_missing`, `post_html_parse_failed`, `post_html_invalid_media`,
`post_html_gated`, `post_html_no_video`. Stage/HTTP classification is logged,
never arbitrary response text, signed URLs, tracking values or manifest contents.
Existing GraphQL remains the last attempt, unchanged.

Source review: https://github.com/inkitori/igembed/blob/main/worker.js uses this
public HTML schema and request route. GitHub reported no declared repository
license on review. No source code, parser, dependencies or Worker infrastructure
from that repository were copied. ReCut independently reads the observed JSON
schema with the standard-library JSON decoder and its existing media adapter.
Do not adopt the source comments' claims of stability or universal anonymous access.

Live local verification: the post HTML returned HTTP 200, a matching public
shortcode and three video_versions. With the prior oEmbed/mobile/embed failures
injected, the real HTML fallback and existing --load-info-json transfer downloaded
16,493,650 bytes; ffprobe confirmed 720x1280 video, audio and 98.635510 seconds.
Only the verification command used system CA via --compat-options no-certifi;
TLS validation stayed enabled, with no production trust changes. Production
behavior of this new stage still requires a Railway bot request after auto-deploy.

Validation of the post HTML addition: 338 tests passed, including staged failure
transitions, identity/privacy/CDN rejection, gated HTML, and DASH-duration limits.
Syntax/import and diff checks passed; no secrets or infrastructure changes added.

### Optional Instagram-only proxy test

Railway job 362 also returned `post_html_redirect_login`; all public routes for
that request failed. Another network egress is a test, not proof of an IP block
or a guaranteed fix. The bot remains hosted on Railway; no account cookies are needed.

Set `INSTAGRAM_PROXY_URL` in Railway Variables to an operator-owned proxy URL,
for example `http://USERNAME:PASSWORD@HOST:PORT` (percent-encode credentials).
HTTP, HTTPS, SOCKS5 and SOCKS5H are accepted. Invalid configuration fails with
`INSTAGRAM_PROXY_CONFIG_INVALID`, without logging the value. Empty/unset keeps
the existing route. Never set global HTTP_PROXY/HTTPS_PROXY for this test.

Only Instagram yt-dlp metadata, all public fallback requests, and its CDN media
transfer use the proxy. TikTok, file uploads, Telegram and OpenRouter retain
their existing behavior. Existing request and transfer timeouts remain in force;
no extra retry or automatic proxy rotation is added. Use a provider sticky
session so extraction and transfer share an egress IP. Full video traffic is
billed by a bandwidth-based provider, not just metadata requests.

Candidate for a small operator-run test: Decodo residential proxy, sticky session.
Official trial page advertises 3 days and requires a payment card; it does not
state an exact trial bandwidth allowance. Confirm the allowance and renewal
conditions in the dashboard before activation. No subscription was purchased.
Sources: https://decodo.com/proxies/free-trial and
https://decodo.com/proxies/residential-proxies/pricing .
Implementation uses the installed libraries' documented proxy support, without
copying provider code: https://curl-cffi.readthedocs.io/en/latest/quick_start.html
and https://github.com/yt-dlp/yt-dlp#network-options .

After setting the variable and deployment, send DXRR2sziOG_ and DeJRGZmsFN5 to
the bot once each. Check `instagram_network_route route=proxy`, metadata success
or `instagram_public_stage_ok`, followed by media download success and actual
Telegram delivery. HTTP 407 / `INSTAGRAM_PROXY_AUTH_REQUIRED` means proxy
authentication failed; stage `_transport_error` / `_timeout` requires checking
proxy connectivity. A valid proxy can still return Instagram login/error codes.
Do not regard unit tests or proxy configuration alone as production success.
Remove the variable to stop proxy traffic and restore the prior route.

Validation: 366 tests passed; syntax/import, focused lint and diff checks passed.
No real proxy credentials were available, so residential egress and Railway
delivery remain unverified. Decodo's listed PAYG price is $4/GB plus applicable
VAT, bought through Wallet in 1GB increments; full media traffic must be budgeted.

### Independent PostRoot GraphQL strategy (October 2026)

Current fallback order after anonymous yt-dlp failure:

1. Googlebot GET `https://www.instagram.com/p/{shortcode}/`.
2. GET `https://www.instagram.com/p/{shortcode}/embed/captioned/`.
3. POST `https://www.instagram.com/api/graphql`, doc_id `27830990013244856`,
   ShortDrama=false and MultiCaptionCarousel=true relay variables.
4. POST `https://www.instagram.com/graphql/query`, doc_id `27128499623469141`,
   AIGMMediaWebLabel=false relay variable.
5. GET `https://i.instagram.com/api/v1/media/{derived_media_id}/info/`.

The new strategy is independently confirmed by Instaloader's current
`instaloader/structures.py` (`Post._obtain_metadata`), under the MIT license:
https://github.com/instaloader/instaloader/blob/master/instaloader/structures.py
https://github.com/instaloader/instaloader/blob/master/LICENSE
Only the endpoint/query protocol was used; no implementation was copied.
Existing query 278 remains available. Experimental ActionLoad IDs are not added.

Numeric media ID is derived locally from shortcode; oEmbed is no longer in the
critical path. Its adapter remains covered for reference. Each GraphQL strategy
bootstraps anonymous web tokens in the existing session and submits its query
at most once. Each stage has a 12-second wall-clock bound (including redirects,
bootstrap and parsing), requests have at most 10 seconds, and the downloader's
existing total 90-second bound remains. There are no error retries.

GraphQL diagnostics now identify the query, for example
`graphql_media_info_278_errors_1675004`,
`graphql_post_root_271_errors_1675004`, or
`graphql_post_root_271_data_missing`. Exhaustion logs
`instagram_public_fallback_exhausted` with an `attempts` map of all five stages.
Only approved fixed reasons, HTTP statuses and numeric error codes enter it;
arbitrary exceptions, response text, tokens and signed URLs are excluded.
CDN validation, quality selection and `--load-info-json` transfer are retained.

After automatic Railway deployment, verify its commit SHA, then submit
`https://www.instagram.com/reel/DXRR2sziOG_/` once. Look for
`instagram_public_stage_ok stage=graphql_post_root_271` if earlier stages fail,
then `instagram_public_fallback_ok`, `url_download_file_created`, and actual
Telegram delivery with audio. If it fails, obtain the complete `attempts` map.
Unit tests and local metadata access do not establish a Railway fix.

Local live probe of the new query on this run ended in curl timeout (code 28),
without a usable response; it does not confirm endpoint success or an Instagram
rejection. A system CA bundle was used for the probe with TLS verification on;
no production TLS configuration changed. Railway validation remains required.

Validation: 376 tests passed (7 existing datetime deprecation warnings).
Syntax/import, focused lint, diff and changed-file secret-pattern checks passed.
