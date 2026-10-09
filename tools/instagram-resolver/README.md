# ReCut Instagram metadata resolver

Independent minimal implementation, in the existing ReCut repository.
No npm dependencies, account cookies, credentials, video proxy or storage.

## Live result (2026-10-09)

A limited diagnostic Worker was executed on the official Cloudflare Playground
against `https://www.instagram.com/reel/DXRR2sziOG_/`. It reported:

```json
{"source":"post_html","ok":true,"signed_url_received":true}
```

The diagnostic used the same public routes/parser approach and validated the
shortcode, public flag and HTTPS Instagram/Facebook CDN hostname. It did not
print the signed URL, transfer MP4, or test Railway/Telegram. Playground preview
is not a permanent production deployment, and does not guarantee identical
behavior at every Cloudflare location. No Cloudflare account/API token was
available for a permanent deployment. Production verification is still required.

## Publish (dashboard, no Railway infrastructure change)

1. Sign into https://dash.cloudflare.com/ . Open **Workers & Pages** and create
   a Worker named `recut-instagram-resolver` (Hello World template is sufficient).
2. Open **Edit code**. Replace the code with the complete `worker.mjs` file in
   this directory. Save/deploy. Before configuring its secret, it returns 401.
3. In the Worker's **Settings → Variables and Secrets**, create an encrypted
   secret named `INSTAGRAM_RESOLVER_SECRET`. Generate a random secret of at
   least 32 characters, for example locally with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Keep it in a password manager; do not send it in chat or commit it.
4. Copy the Worker's HTTPS `workers.dev` URL. The API path is `/instagram`.
   Keep Worker observability/logging disabled; no custom console logs are used.
5. Add two Railway service variables:
   - `INSTAGRAM_RESOLVER_URL=https://<worker>.<account>.workers.dev/instagram`
   - `INSTAGRAM_RESOLVER_SECRET=<same secret>`
6. Allow Railway to redeploy with those variables. No `railway up` is needed.
   `INSTAGRAM_PROXY_URL` is not needed for the Worker route.

Alternative from this directory (Wrangler is Cloudflare's official CLI):

```bash
npx wrangler login
npx wrangler deploy
npx wrangler secret put INSTAGRAM_RESOLVER_SECRET
```

The last command securely prompts for the secret. Never pass it in the command
line or put it in `wrangler.jsonc`. A free-plan account is sufficient to test;
no paid API, paid proxy or plan upgrade is part of this implementation.

## Independent test before using the bot

Run `python tools/instagram-resolver/probe.py` from the ReCut root. It prompts
for endpoint/secret if env variables are absent. Prefer running it inside the
existing Railway service's shell: this confirms the direct CDN transfer from
Railway, rather than only your laptop. It has no Telegram or database actions.
The output contains only status, source, byte count, audio and duration. The
test file is removed automatically. An HTTP 200 or signed URL alone is not
successful video delivery.

Then send exactly this to `@contentcutbot`:

`https://www.instagram.com/reel/DXRR2sziOG_/`

Expected job events:
`yt_dlp_metadata_failed` → `instagram_resolver_request_started` →
`instagram_resolver_success` → `url_download_file_created` → Telegram delivery.
If resolver fails, its fixed event is followed by the existing local extractor.
Send the runtime log for the same job; never send the secret or raw resolver JSON.

## Contract / bounds

`GET /instagram?url=<HTTPS instagram.com or www.instagram.com Reel/Post URL>`
with `X-ReCut-Resolver-Secret`. Only `/reel/{shortcode}/` and `/p/{shortcode}/`
are accepted. Source tracking query parameters are discarded. URL destinations
are constructed from the validated shortcode, never supplied generically.

Order: embed/captioned → public Googlebot HTML → existing public PostRoot
GraphQL 271. No new doc_id is guessed. Up to four requests, six seconds each,
two MB per response. Redirects are not followed, including login redirects.
The returned media URL must be HTTPS on a subdomain of cdninstagram.com or
fbcdn.net, with no userinfo or nonstandard port. Selected dimensions are at
most 4096. Privacy and shortcode must match. Responses use `Cache-Control:
no-store`. No signed URLs, tokens or arbitrary exception messages are logged.

The Python client has a 28-second wall-clock bound, 32 KB response limit, no
redirects, and independent CDN validation. It accepts only `*.workers.dev`
resolver destinations; custom domains require an explicit validation change.
Absent resolver env variables leave the previous downloader behavior intact.

Pipeline: yt-dlp metadata → optional resolver → existing local public metadata
fallback → existing owner-only `--load-info-json` file → direct CDN download.
TikTok, Telegram, database and uploads do not use the resolver.

## Proxy alternative

Existing `INSTAGRAM_PROXY_URL` affects Instagram extraction requests only.
This change sends Instagram media transfer directly, with `--proxy ""`, even
when a metadata proxy is configured. It never enables a global proxy or retries
MP4 through a paid proxy automatically. If CDN URLs are bound to the extraction
IP, direct transfer may fail; inspect the production result before authorizing
video proxy traffic. No guarantee of IP-independent URLs is assumed.

If the production Worker fails, first test an operator-owned ISP/static
residential endpoint; another datacenter IP may reproduce the public gating.
Residential/ISP is a candidate, not a guaranteed fix. Get a small trial or the
smallest bandwidth allowance and test one Reel before subscribing. Metadata
traffic depends on HTML/GraphQL bodies: often hundreds of KB to a few MB, not
a fixed per-Reel amount. This Worker's hard cap is 8 MB (four 2 MB responses),
but the existing Python proxy extraction has different bounds. Measure provider
traffic over a few requests before choosing a tariff. MP4 traffic is excluded
by the direct-transfer policy.

## Tests

`node --test tools/instagram-resolver/worker.test.mjs`

`python -m pytest -q` (normal project test configuration required)

Protocol references, implementation independently written:
- https://github.com/inkitori/igembed/blob/main/worker.js (no declared license;
  no implementation copied)
- https://github.com/imputnet/cobalt/blob/main/api/src/processing/services/instagram.js
  (AGPL; no implementation copied)
- https://github.com/instaloader/instaloader/blob/master/instaloader/structures.py (MIT)
- https://developers.cloudflare.com/workers/playground/
- https://developers.cloudflare.com/workers/get-started/dashboard/
- https://developers.cloudflare.com/workers/configuration/secrets/
