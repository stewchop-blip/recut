# Traffic and retention in ReCut

## Operator commands

- `/links`: four links using the bot's real Telegram username.
- `/links tiktok_video_01`: an additional campaign label (lowercase Latin letters,
  digits, underscores and hyphens; 1–48 characters). No external shortener needed.
- `/stats`: 30 calendar days including today, UTC.
- `/stats 7` or `/stats 90`: alternative periods.
- `/stats 30 tiktok_video_01`: one exact source/campaign. Not a prefix match.

Initial placement links for @contentcutbot:

| Placement | URL |
|---|---|
| TikTok | https://t.me/contentcutbot?start=src_tiktok |
| Instagram | https://t.me/contentcutbot?start=src_instagram |
| YouTube | https://t.me/contentcutbot?start=src_youtube |
| Telegram | https://t.me/contentcutbot?start=src_telegram |

Use different labels for different accounts, ads and videos. Reusing one label
combines their results. The original untagged link remains valid and counts as
`direct`. Referral links keep their existing `ref_...` payload and bonuses, and
appear under `referral`. Marketing links never grant referral rewards.

Telegram delivers the `start` payload, not a web click event:
https://core.telegram.org/bots/features#deep-linking
A person must send/start the conversation for the bot to observe them. A copied
link retains the same label, so the label identifies the distributed link and
not verified platform identity. Labels are public, can be edited by visitors,
and must not contain secrets or personal information.

## Definitions (source of truth)

| Metric | Definition | Caveat |
|---|---|---|
| New users | First observed private-chat interaction in period, with no pre-existing BotProfile/User/UserSettings/Job | Begins with this release; earlier users are not relabelled as new |
| First result / activation | New users with at least one delivered processed video, divided by new users in the same cohort | Cumulative as of report time, not a fixed 24-hour conversion window; original downloads excluded |
| Active users | Distinct non-internal users with a private message or callback in period | Includes returning legacy users and commands/menu interactions; not a completed-render metric |
| D1 return | New users active on the UTC calendar day after arrival / new users whose entire D1 has elapsed | Does not mean exactly 24 hours later; immature cohorts excluded |
| D7 return | Same, on the seventh UTC calendar day after arrival | This is exact-day retention, not any return during the first week |

An empty denominator is shown as “ещё нет данных”, not 0% retention. Reports group
by immutable first source and show the top 20 sources; filtered reports inspect
any additional source. Totals still include all sources. A new link does not
reassign existing users or increase acquisition counts.

## Access and data minimization

Private admin commands only. `ADMIN_TELEGRAM_IDS` is the explicit admin list.
If unset, current `ALLOWED_TELEGRAM_USER_IDS` beta operators retain access. Tester
IDs alone never grant report access. Reports also pass the existing access gate.
Before opening public access, explicitly set admin IDs and keep public users out
of the beta allowlist. This release does NOT open access.

Beta allowlist, explicit admins, testers and unlimited IDs are excluded from
metrics. Internal status is snapshotted at the first interaction and current
internal IDs are also excluded at query time. This keeps historical tests out
even after removing test access. No admin role is inferred from a username.

Two additive tables are created through existing startup `create_all`:
`analytics_acquisitions` (one row/user) and `analytics_activity_days` (one row/user/UTC day).
No messages, names, source video links, transcripts or media are copied into
analytics. No third-party analytics service receives data. Day rows are indexed
by day and user; conflict-safe inserts deduplicate repeat updates and concurrent
replicas. Activation uses a conditional first-result update, so rerenders and
replayed completion hooks do not multiply conversions.

Measurement happens after access checks: rejected closed-beta visitors are not
counted as arrivals. Best-effort observation has a two-second timeout and logs
`analytics_observation_failed` on a gap; bot functionality continues. Delayed or
failed observation can undercount acquisitions/retention, so investigate that
log event before relying on the report. There is no fabricated historical
backfill. These counters do not measure blocking/unsubscribing, impressions,
web clicks, ad spend, revenue or profitable growth.

## Validation and deployment

Tests exercise first touch, duplicate updates, legacy users, referral
compatibility, delivered-result hooks, mature D1/D7 denominators, exclusions,
admin access, real dispatcher routing and failure isolation against SQLite.
PostgreSQL dialect uses native ON CONFLICT; production PostgreSQL and live
Telegram end-to-end validation require checking /links and /stats after deploy.
Compare placement conversion and D7 only after comparable cohorts mature; no
performance benchmarks or traffic targets are asserted without actual data.
