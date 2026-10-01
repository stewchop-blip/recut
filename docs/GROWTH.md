# Onboarding, mascot and referrals

## Shipped behaviour

- First private `/start` displays capabilities and three steps to the first MP4.
  A persistent onboarding flag is set only after Telegram accepts the welcome.
  Later starts keep the existing resume/home behaviour. Help is available from
  the home menu and `/help`. There is no promise of recommendation eligibility.
- `Оформление → Персонаж снизу` is an opt-in, persistent switch, initially OFF.
  The single bundled transparent robot gently bobs in the lower-left corner.
  It is wired to QuickPrep, Maximum Transform, three versions and long clips;
  original downloads stay original. For bottom banners it moves upward.
- `Пригласить друга` and `/referral` show a personal deep link and bonus statistics
  when enabled. Link opening alone earns nothing. A new invitee's first delivered
  processed result earns the inviter +3 credits by default. Downloads do not count.
  One inviter per user, first touch only, no self-referrals or existing-user referrals.
  Ledger key uniqueness + conditional updates prevent duplicate awards.
- Limits, referrals and sales stay disabled in the current closed beta. Access still
  uses the existing allowlist; these flags do NOT open the bot to everyone.

## Launch settings (not applied to Railway)

| Variable | Default | Meaning |
|---|---|---|
| REFERRALS_ENABLED | false | Issue links and accept new referral attribution |
| REFERRAL_REWARD_CREDITS | 3 | Inviter credits, snapshotted at invitee registration |
| REFERRAL_MAX_REWARDS | 50 | Lifetime rewarded friends per inviter |
| GENERATION_LIMITS_ENABLED | false | Enable daily quota and bonus/paid ledger spending |
| DAILY_FREE_GENERATIONS | 3 | Free successful render actions per UTC day |
| UNLIMITED_TELEGRAM_IDS | empty | Comma-separated admin/tester IDs exempt from quotas |

The UTC day resets at 03:00 Minsk. One render action costs one generation, including
three-version batches and long-video batches. At least one delivered clip constitutes
success. Downloads and failed attempts are free. Free daily allowance is used first,
then existing credit-ledger balance. Credits accumulate during unlimited beta if
referrals are explicitly enabled; this is stated in the referral screen.

Test in staging with allowed test accounts before enabling. Set owner/brother IDs
in UNLIMITED_TELEGRAM_IDS before turning on quotas. Keep sales disabled separately.
No production environment variables are changed by this commit.

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

`app/assets/mascot.png`: generated with built-in imagegen, actual RGBA alpha; original
file preserved in the project. Prompt: one compact full-body ivory 3D robot, dark visor,
warm eyes, subtle blue accents, one hand pointing up; transparent background; no text,
logos, extra objects or ground shadow. Runtime scales and animates it in FFmpeg.
