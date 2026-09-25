# Donor Reference Map — ReCut Stabilization

Reference use ONLY. No full bot copied. MIT / Apache-2.0 licenses preserved where substantial adaptation applied.

## Donor A — reels-downloader-bot (MelDxKviel)
- `src/services/download_jobs.py` → `CurrentMediaService` (READY state, PostgreSQL persistence)
- `src/services/download_worker.py` → `DownloadService` lifecycle (semaphore, job isolation, finally cleanup)
- `src/services/download_cache.py` → cache reserve/release (reference only, minimal adapter)
- `src/services/url_utils.py` → URL validation / normalization (`extract_supported_url`)
- `src/bot/handlers/download.py` → router priority (specific before generic URL)
- `src/services/database.py` → `DownloadResult` / `JobStatus` model shape

## Donor B — ffmpeg-video-bot (shohan-001)
- `bot/ffmpeg/core.py` → `FFmpegRunner` primitive layer (spawn, stderr capture, progress, cancel, structured result)
- `bot/ffmpeg/effects.py` → overlay / watermark / PIP patterns (`add_video_overlay` reference)
- `bot/ffmpeg/encode.py` → speed (`setpts`/`atempo`), rotation (`transpose`), compression (`compress_video` reference)
- `bot/ffmpeg/trim.py` → trim / segment patterns (`trim` functions reference)

## Donor C — reclip-telegram-bot (gth-ai)
- `bot/reclip_client.py` → `poll_status()` / download lifecycle reference (not copied)
- `bot/cleanup.py` → cleanup by age / disk limit reference
- `reclip/app.py` → service separation architecture reference (Telegram → download → result)
- `docker-compose.yml` → self-hosted Bot API reference (future, not now)

## Donor D — Downy (ismailkkaaa)
- `downloaders/router.py` → `SourceRouter` / `DownloaderRouter` pattern (`can_handle` reference)
- `downloaders/base.py` → `BaseDownloader` interface shape reference
- `utils/ytdlp.py` → yt-dlp helper / progress hook reference
- `utils/validators.py` → URL validation patterns reference
- Compression fallback: `duration` → `target bitrate` → `resolution reduction` (reference only)

## What was copied / adapted (substantial)
- `DownloadService` lifecycle: `run_download_worker()` pattern (semaphore + finally cleanup) — adapted to QuickPrep + Smart Clips
- `FFmpegRunner`: process spawn + stderr + progress parsing — adapted (not full `core.py`)
- `normalize_square_pixels()` / `burn_cta()` / `detect_black_bars()`: adapted from `ffmpeg-video-bot` patterns; kept in `ffmpeg.py`

## What was reference only (not copied)
- `reclip/app.py` architecture diagram (service separation)
- `clean.py` (cleanup logic shape)
- `Dashboard` (not implemented)
- `Self-hosted Telegram Bot API` (future option)
- `Downy` full application structure (not adopted)
- `reels-downloader-bot` full Telegram bot (Pyrogram not adopted, aiogram kept)

## License preservation
- MIT (`ffmpeg-video-bot`): preserved in `Dockerfile` comments + `app/services/media/ffmpeg.py` header
- Apache-2.0 (`reels-downloader-bot`): preserved references; no substantial blocks copied verbatim
- `reclip-telegram-bot`: license unclear — only patterns/ideas referenced; no substantial blocks copied
- `Downy`: license unclear — only interface/reference used; no substantial code copied
