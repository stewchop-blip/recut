# ReCut targeted security review — 2026-09-27

Scope: repository source at a1db908 and this patch. No live server, environment
secrets, database contents or Railway network settings were accessed. This is a
focused source review, not a penetration test or an assurance of public readiness.

## Fixed

- Cookie temporary files leaked on metadata/download failures: cleanup now runs in
  finally, including cancellation. Files retain owner-only permissions.
- Download dedup shared user-specific file paths across users: keys now include
  user ID. A cancelled caller cannot remove the still-running dedup entry.
- Queue admission now reserves user capacity before scheduling; overflow gets a
  friendly response instead of dereferencing a failed result as a video.
- `/cancel` could match job 1 against folders for job 11; matching is now exact or
  `job_1_` prefixed, under configured temp root. Missing Path import repaired.
- Source URLs now reject credentials and unusual ports as well as unsupported hosts.
- Downloader uses configured duration/size limits; checks downloaded size and passes
  max-filesize to yt-dlp. Unknown-size streams still need a hard runtime disk quota.
- yt-dlp ignores local configs/plugins; timeout/cancellation kills and reaps its process.
- Known configured secrets are masked in nested log strings/lists, not only `event`.
- Pending Telegram updates no longer discarded on restart (financial receipts).
- Webhook entrypoint was an unawaited async function; now callable by main and
  requires a webhook secret in every environment.
- Sender-less ordinary events are denied. Financial receipts remain processable
  independently of sales/beta flags; payment support stays accessible.

## Remaining before unrestricted public access

- URL hostname checks cover the initial URL, NOT every redirect, resolved address
  or media request performed by yt-dlp/FFmpeg. Enforce outbound private-address
  blocking at network/container level; verify redirects and DNS resolution. No
  statement of complete SSRF protection is warranted.
- Queue and beta state are process-local. Download concurrency is bounded, but
  render callbacks are not uniformly governed by the same persistent queue.
  Multiple replicas and restart-safe execution require further work.
- Paid ledger is prepared; actual per-job credit reservation/debit/release and
  automated receipt reconciliation are launch gates (see PAYMENTS.md).
- Media parsing runs in the application container; resource/process isolation,
  disk quotas, runtime patching and bounded subprocess output need validation.
- API exceptions from dependencies may include information beyond known configured
  secrets. Log access and retention should be limited; redaction is not exhaustive.
- Database backup/restore, secret rotation, network policy, dependency vulnerability
  status and load capacity have NOT been verified against the deployed environment.

No new video/audio transformations were introduced.

## Validation

Full local test suite: 161 passed. Includes SQLite transaction/replay tests,
concurrent duplicate payment, invalid ownership/currency/amount, refund ordering,
queue cancellation/isolation, cookie cleanup and exact cancellation directory match.
Dispatcher import and registered payment update types checked; payments disabled
by default. An old cancellation test was updated to the existing `(count, ids)` API.
No end-to-end payment, live PostgreSQL concurrency, or deployed load test was run.


## 2026-10-01 follow-up: referrals and generation allowance

Optional per-user render reservations and credit debit/release now exist
(`GENERATION_LIMITS_ENABLED=false` by default). Referral attribution is first-touch,
opaque-token based, and excludes existing users; rewards require successful delivery,
are idempotent and have a configurable lifetime cap. This does not prevent multi-account
abuse. SQLite concurrency and transaction tests are included; staging PostgreSQL and
live Telegram delivery/restart reconciliation still require verification. Abrupt process
termination can leave reserved runs: use the explicit operator procedure in GROWTH.md.
No whitelist, payment enablement, or public-access setting was changed.
