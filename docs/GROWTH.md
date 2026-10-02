# Onboarding, mascot and referrals

## Shipped behaviour

- Telegram's pre-Start description explains capabilities and asks visitors to press
  Start. Startup registers default and Russian descriptions with Telegram.
- First private `/start` displays capabilities and three steps to the first MP4.
  A persistent onboarding flag is set only after Telegram accepts the welcome.
  Every /start clears the selected source in DB and memory and shows the guide.
  /cancel also clears the selection even without an active job. Help is available from
  the home menu and `/help`. There is no promise of recommendation eligibility.
- `Оформление → Персонаж снизу` is an opt-in, persistent switch, initially OFF.
  The user-supplied transparent dancing-cat GIF loops in the lower-left corner.
  It is wired to QuickPrep, Maximum Transform, three versions and long clips;
  original downloads stay original. For bottom banners it moves upward.
- `Пригласить друга` and `/referral` show a personal deep link and bonus statistics
  when enabled. Link opening alone earns nothing. A new invitee's first delivered
  processed result earns the inviter +3 credits by default. Downloads do not count.
  One inviter per user, first touch only, no self-referrals or existing-user referrals.
  Ledger key uniqueness + conditional updates prevent duplicate awards.
- Public access, limits and referrals are enabled by default; sales stay disabled.
  Set PUBLIC_ACCESS_ENABLED=false to restore closed-beta access. Keep the existing
  allowlist for operator permissions and tester exemptions; do not clear it at launch.
- Current allowlisted beta testers remain unlimited by default. Do not add ordinary
  public users to that tester list. Set BETA_TESTERS_UNLIMITED=false to stop this exemption.

## Launch defaults (environment overrides take precedence)

| Variable | Default | Meaning |
|---|---|---|
| PUBLIC_ACCESS_ENABLED | true | Allow all users; false restores the beta allowlist |
| REFERRALS_ENABLED | true | Issue links and accept new referral attribution |
| REFERRAL_REWARD_CREDITS | 3 | Inviter credits, snapshotted at invitee registration |
| REFERRAL_MAX_REWARDS | 50 | Lifetime rewarded friends per inviter |
| GENERATION_LIMITS_ENABLED | true | Enable daily quota and bonus/paid ledger spending |
| DAILY_FREE_GENERATIONS | 3 | Free successful render actions per UTC day |
| UNLIMITED_TELEGRAM_IDS | empty | Comma-separated admin/tester IDs exempt from quotas |
| BETA_TESTERS_UNLIMITED | true | Existing access allowlist also exempts beta testers |

The UTC day resets at 03:00 Minsk. One render action costs one generation, including
three-version batches and long-video batches. At least one delivered clip constitutes
success. Downloads and failed attempts are free. Free daily allowance is used first,
then existing credit-ledger balance. Credits accumulate during unlimited beta if
referrals are explicitly enabled; this is stated in the referral screen.

To test quotas with an allowlisted test account, set BETA_TESTERS_UNLIMITED=false
and keep only owner/brother IDs in UNLIMITED_TELEGRAM_IDS. Keep sales disabled.
No production environment variables are changed by this commit: if Railway already
sets REFERRALS_ENABLED=false or GENERATION_LIMITS_ENABLED=false, those values still
override the new defaults. Runtime enablement must be verified with /referral and /balance.

## Transactions, restart and reconciliation

Startup creates `bot_profiles` and `generation_runs`; the existing idempotent schema
migration adds `user_settings.decoration_enabled` with default false. Data is not removed.

Reservations take a per-user DB write lock before checking free allowance/balance;
concurrent renders for that user are denied while a reservation is pending. Duplicate
callback IDs cannot spend twice. Completion of the job, referral credit and generation
completion are committed in the same transaction after Telegram delivery. Normal
failure/cancellation releases the free slot or credits once.

A hard process/container kill, DB outage during release, or delivery followed by a DB
commit failure cannot be reconciled solely from local state. Pending reservations are
preserved, never guessed successful. Operator procedure:

1. `python -m app.maintenance.generations` lists pending reservation IDs.
2. Check logs and confirm the corresponding render worker has stopped. Never refund
   a still-running request. Check whether Telegram delivery already succeeded.
3. For an abandoned/failed request run
   `python -m app.maintenance.generations --refund REQUEST_ID`.
   This is idempotent and restores the consumed credit or free slot.

Tests cover SQLite transactions/replays/concurrency, not production PostgreSQL,
Telegram networks or multi-replica load. Referral caps do not prevent multi-account
farming; review abuse and operating costs before opening unrestricted access.

## Mascot asset

`app/assets/mascot.gif`: user-supplied transparent dancing cat, copied byte-for-byte
from the provided GIF (40 frames, four seconds per cycle). FFmpeg repeats it for the
full source duration, preserves its original timing and adds no artificial bobbing.
The opt-in switch and stored preference are unchanged.

## Brand corner

The existing opt-in switch now renders a rounded dark card with ReCut,
«Клип за пару кликов» and t.me/contentcutbot. It scales with the canvas, moves below
a title when present, and shares the input registry with the animated mascot.
The address is visible video text, not a clickable video link.
