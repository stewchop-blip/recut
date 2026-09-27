---
name: recut-stabilization
description: Stabilization, security fixes, and UX cleanup for ReCut Telegram video bot — to the user's spec (no new product features until basic flow is stable, brief responses, confirm before edits, manual Git upload allowed but auto-push now permitted per AGENTS.md).
---

Always-on rules (apply to every stabilization pass):
- Confirm file edits with the user before applying (user explicitly expressed frustration at unconfirmed changes: "подожди, я только залил файлы на гитхаб, а ты уже все меняешь").
- Do NOT add new product features (new presets, new AI, new platforms) until basic flow (video upload → render → result → navigation reuse) is stable and verified by real tests.
- Respond briefly ("короче пожалуйста") — no motivational intros, no restating the request, no replay of process steps already shown.
- Git: commit with meaningful message, push origin/main; Railway auto-deploys. Do NOT use `railway up`. Do NOT commit secrets (cookies, tokens, .env contents); check `.gitignore` before push.
- Before any serious edit: read AGENTS.md, check `git status`, inspect existing imports/references; do not rewrite working modules from scratch.

Procedure (stabilization pass in order):
1. Read AGENTS.md and pasted spec; restore context from git log.
2. Audit existing code without changing it: list callbacks/handlers (expect 0 dead), verify `setsar=1` (not `reset_sar`), check `CurrentMedia.get()` returns real duration/width/height (not hardcoded 0).
3. Verify real FFmpeg smoke: vertical source → 1080x1920, SAR 1:1, H264/AAC, returncode 0.
4. Fix multi-user `/cancel` bug (CRITICAL, BLOCKING): change `cancel_active_jobs` to return `(count, [job_ids])`; update `cmd_cancel` to delete ONLY directories whose names contain the cancelled job IDs; add namespace isolation (`/tmp/recut/jobs/`, `current/`, `assets/`, `cache/`).
5. Fix banner sizing (Phase 1-2): apply different `max_h` per preset (`small=0.12`, `medium=0.18`, `large=0.24` of frame height); verify `SMALL < MEDIUM < LARGE` invariant after clamp; log `cta_size_requested`, `target_w`, `max_h`, `final_scale`, `output_w`, `output_h`.
6. Make style presets independent of banner size (Phase 3): remove `cta_size` from `_preset_to_fields()`; banner size = independent user preference (`cta_size` from DB settings only).
7. Ensure preview == production (Phase 4): preview handler uses the same `ms.burn_cta()` with same `size_preset` and timing; no separate preview sizing logic.
8. Add `overlay_is_animated` through QuickPrep pipeline (Phase 5): add parameter `overlay_is_animated: bool = False` to `QuickPrepPipeline.run()`; pass it to `media.burn_cta()`; read from DB (`s.overlay_is_animated`) in both quick_prep and URL recut handlers.
9. Fix structured download errors end-to-end (Phase 9): import `StructuredDownloadError`; catch it before generic `URLDownloadError`; preserve `.code`; use `DB error_code = e.code`; provide user-facing messages per platform (`TIKTOK_AUTH_REQUIRED`, `INSTAGRAM_RESTRICTED`, etc.).
10. Complete `CurrentMedia` metadata (Phase 6): add `_probe_path()` helper using `get_probe_service().probe()`; use it in `get()` and `set_ready()` to return real `duration`, `width`, `height`.
11. Fix misleading result-menu labels (Phase 5): `RESULT_MENU_PREPARE` uses `"🔄 Сделать иначе"` (`mode:prepare`), `"🎨 Изменить оформление"` (`appearance:menu`), `"📎 Другое видео"` (`replace_media`), `"🏠 Главное меню"` (`home:open`).
12. Add `replace_media` handler: clears `CurrentMedia` (`get_current_media_service().clear()`), restores last selected mode, asks for new source; label clearly distinguishes reuse (`Сделать иначе`) from new source (`Другое видео`).
13. Fix cookie security (Phase 13): use `tempfile.mkstemp(prefix=f"{platform}_cookies_", suffix=".txt")` for random filenames; set `chmod(0o600)`; never log cookie contents; clean with `finally` (`cookie_file.unlink(missing_ok=True)`) after yt-dlp attempt/retry.
14. Add cookie validation (Phase 14): if Base64 decode fails or cookie file appears invalid, return/log `COOKIE_CONFIG_INVALID` (no secret content logged).
15. Check webhook security (Phase 15): if `WEBHOOK_MODE=true` and `ENVIRONMENT=production`, require `WEBHOOK_SECRET` non-empty; fail startup clearly otherwise.
16. Check URL log privacy (Phase 16): log only `platform`, `hostname`, `url_hash` (not full URL); never log cookies/auth/Telegram token/OpenRouter key.
17. Unify download limits (Phase 17): `DownloaderService.download()` should receive `max_size_mb` from `settings.max_video_size_mb` and `max_duration_seconds` from `settings.max_video_duration_minutes * 60`.
18. Check resource safety (Phase 18): validate duration/file size/video stream before starting Whisper/heavy FFmpeg; keep `MAX_CONCURRENT_JOBS` small; do NOT add Redis/Celery now.
19. Add dependency lock (Phase 19): create `uv.lock` via `uv lock --python 3.14`; fix `requires-python` if needed; never commit `types-python-dotenv` errors as a skill lesson.
20. Add `.dockerignore` (Phase 20): exclude `.git`, `.env`, `.env.*`, `venv`, `__pycache__`, `.pytest_cache`, `tests/`, `*.log`, `tmp/`, `cache/`, `downloads/`.
21. Button audit (Phase 12/23): enumerate all `callback_data` strings; verify 0 dead callbacks; add automated audit if possible; inspect semantics (every visible button performs action or navigates).
22. Fix handler-to-handler direct calls when touching those flows (Phase 13/21): replace direct `await on_appearance_menu(call)` with pure render helpers (`render_appearance(...)`) so `call.answer()` is called exactly once.
23. Remove only proven legacy code (Phase 17): check imports/references before deleting `video_legacy.py`, `vertical_renderer.py`, old CTA helpers, legacy `ACTION_MENU` / `URL_ACTION_MENU`. Only delete if genuinely unused.
24. Check multiple bot instances (Phase 18): verify `bot_instance_id`, `pid`, `hostname` logging present; if `TelegramConflictError` appears in Railway logs, tell user to check Dashboard (one service, one replica, no second bot with same token).
25. Final deploy: `git status` → review diff (no secrets/cookies) → `pytest` / syntax/import check → real FFmpeg smoke → commit (`STABILIZE Phase ...`) → `git push origin main`. Stop there; do NOT run `railway up`. Report which concrete Railway runtime events to check in Dashboard.
26. Final report format: current SHA before work (`git log --oneline -1`), root causes found, files changed, local video test result (`returncode`, `SAR`, `resolution`), aspect ratio tests (`portrait 9:16`, `landscape 16:9`, `4:3`, `1:1`, `rotation 90°`), `CurrentMedia` reuse test (`re-render x3` without re-upload), banner size results (`Small < Medium < Large` pixel measurements), Instagram/TikTok/YouTube URL test results (`platform`, `yt-dlp version`, `cookie retry yes/no`, `bytes`, `duration`, `error_code`), cookie env configuration status, button audit result (`39 callbacks`, `0 dead`), remaining known limitations (`Phase 14-28` not fully verified by hand), final pushed SHA.

Pitfalls (generalizable rules, imperative + mechanism):
- Always read `AGENTS.md` before editing anything in this repo (always-on rule from AGENTS.md line 1).
- Confirm file edits with the user before applying (`"подтверждай изменения до правки"` — confirmed by user's explicit correction when files were edited after a GitHub push without confirmation).
- Do NOT add new features (`Clean`, `Meme`, `Brand`, `Custom` only; no new presets/styles/backgrounds until basic flow stable). Confirmed by user's repeated instruction: `"Не добавлять новые product features"` / `"До этого никаких новых функций"`.
- Keep responses brief (`"короче пожалуйста"`) — no filler, no restatement, no process replay.
- Never commit `.env` secrets; `.env` is in `.gitignore`. Confirmed by user's instruction (`"НЕ заливать .env на GitHub"`) and by `.gitignore` content.
- Use `setsar=1` (not `reset_sar=1`) in FFmpeg filters; remove second black-bar crop for vertical sources. Confirmed by commit `40c9ce1` and verified in current `app/services/media/ffmpeg.py`.
- Never replace `CurrentMedia` with fake `duration=0` / `width=0` / `height=0`. Use `_probe_path()` to probe the actual source file. Confirmed by Phase 6 fix.
- `StructuredDownloadError.code` must be preserved through Telegram handler to user-facing message. Confirmed by Phase 9 fix (`except StructuredDownloadError as e:` before generic `URLDownloadError`).
- `ALLOWED_TELEGRAM_USER_IDS` is comma-separated in `.env`; update it (e.g., `458918996,845086533`) but never commit `.env`. Confirmed by `.env` edit and `.gitignore`.
- Banner preview (`banner:preview`) must use the exact same `ms.burn_cta()` call as production (`same size_preset, same position, same timing, same overlay_type`). Confirmed by `preview_keyboard()` using `ms.burn_cta()` with `size_preset`.
- Cookie files must never have predictable global names (`/tmp/instagram_cookies.txt`); always use random filenames with `tempfile.mkstemp()` and `chmod(0o600)`. Confirmed by Phase 13 fix.
- `/cancel` must never delete workspace directories for other users (`/tmp/recut/*` scan was unsafe). Confirmed by Phase 6 fix (`any(str(job_id) in entry.name for job_id in cancelled_ids)`).
- Before pushing, verify `git status`, `git diff --stat` (confirm no `.env` / cookie files / secrets), syntax check (`py_compile`), import check (`python -c`), and brief FFmpeg smoke (`1080x1920 SAR 1:1`). Confirmed by all pushes (`2763c32`, `8515941`, `01edc56`, `c6de171`, `40c9ce1`, `0b870d3`, `f3b7148` etc.).

References (references/stabilization.md is not needed because the rules above cover the class): the skill itself carries the workflow. If needed, a future session can add `references/` files (e.g., `references/banner-geometry.md` for the exact pixel measurement protocol) — but don't create one now since the measurement protocol isn't fully executed (only code fix applied, no manual pixel measurement completed). The existing `docs/DONOR_MAP.md` covers reference sources; the `README.md` covers quick start; `AGENTS.md` covers agent rules — none of these are session artifacts and should not be duplicated.
