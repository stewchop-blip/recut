# Saved appearance and one-click processing

Primary intake menu: **✨ Сделать ролик / 📥 Оригинал / ⚙️ Оформление**.
All supported URL sources and Telegram file intake use it. Default intake always uses
QuickPrep; explicit long-video clipping and three-version actions remain available under
Appearance → processing options. Existing callback identifiers remain supported.

`handlers/appearance.py` owns the unified settings screen, validated pickers, logo uploads
and branding toggle. Existing banner handlers stay in `video.py`; the duplicated URL render
implementation has been replaced with delegation to the same QuickPrep handler.

Saved settings include subtitles, subtitle style/language, banner and logo, 9:16/4:5/1:1
format, output quality, processing style and audio. The initial clean preset preserves audio,
uses a full layout, disables speech recognition, and produces a readable small ReCut signature.
A branding-only export puts the signature into the existing layout encode; with user assets,
the final signature follows those assets, so they cannot hide it accidentally.

## Persistence and compatibility

Eight new fields are added through the existing idempotent PostgreSQL/SQLite startup migration.
Old settings and source identities are retained. No existing database field or enum is removed.
Assets use per-user Telegram file IDs, so they can be downloaded again after redeployment.
Render workspaces are isolated from the stable current source. Original delivery performs
no normalization or rendering, keeps the source available and handles failed delivery.

The processing queue is bounded to eight admitted users and one active render per process.
Duplicate clicks and replacing video during an admitted render are rejected even for unlimited
testers. Existing daily credit reservations/refunds still wrap paid render actions; downloading
an original does not consume a render allowance. The existing download queue is unchanged.
Production continues to assume a single bot worker process; this is not a distributed queue.

## Premium

`premium_until` is an optional server-managed entitlement expiry, checked against UTC.
An absent or expired entitlement forces branding ON, independently of saved preference or style.
A valid entitlement allows toggling `recut_branding`. No user callback accepts entitlement values.
Legacy render actions inherit this policy through the render middleware and media service.

Current payments sell processing credits, not subscriptions. Premium checkout is intentionally
not offered until commercial terms and entitlement fulfillment are implemented. The
«Получить Premium» button states this clearly and points to existing support. For an already
approved Premium account, the operator can set `premium_until` through the trusted database;
credit purchases and quota-exempt tester IDs never imply Premium.

## Safety and failure behavior

Images are content-verified and decoded with Pillow: at most 10 MB, 4096 pixels per side,
16 million pixels per image, 300 frames, 100 million total decoded pixels and 30 seconds.
Logos are static PNG/JPEG/WebP. Existing banner animation support remains, with short MP4
validated by ffprobe and bounded to 30 seconds. Filenames are never executable shell strings.
Upload scratch files are removed in TemporaryDirectory on success and failure.

Subtitles transcribe the rendered video's audio, keeping timing aligned after speed changes.
The existing ASS builder is reused; temporary ASS files outside the job are removed in finally.
Unavailable speech recognition or no speech returns the video and a visible subtitle warning.
Required branding/geometry failures fail the render rather than silently deliver an unbranded
or distorted result. Render files are cleaned in finally; retained source/preview/cache files
use the existing TTL cleanup service. Downloader behavior and Instagram fallback ordering
are unchanged.

## Validation

`tests/test_appearance.py` covers persisted user isolation, backend Free/Premium policy,
queue admission and cleanup, saved settings reaching the action handler, bounded images,
real FFmpeg exports with logo/branding and subtitle fallback. Existing migration tests check
idempotence and preservation of an old row. Existing download, geometry, payments, quota,
referral and processing regression suites must also pass.
