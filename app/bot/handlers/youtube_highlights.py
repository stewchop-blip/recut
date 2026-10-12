"""Explicit YouTube highlights intake before any media download.

The render callback uses the existing render queue, quota middleware, sender,
settings and QuickPrep engine. A stale button cannot process a newer URL.
"""
import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import F, types
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import JobStatus
from app.database.repositories import JobRepository, UserSettingsRepository
from app.database.session import db_manager
from app.pipeline.analyser import Analyser
from app.pipeline.clip_cutter import ClipCutter
from app.pipeline.quick_prep import QuickPrepPipeline
from app.pipeline.final_renderer import FinalClip, FinalJob
from app.services.appearance import download_asset, effective_branding, output_geometry, validate_image
from app.services.sender import TelegramSender
from app.services.transcription.base import Segment
from app.services.youtube.highlights import YouTubeSource, HighlightsError, youtube_url, select_parts
from app.utils.temp import get_temp_manager

logger = get_logger(__name__)


@dataclass
class Pending:
    url: str
    status_id: int
    chat_id: int
    created: float


_pending: dict[int, Pending] = {}
_running: dict[int, tuple[int, asyncio.Task]] = {}


def cancel_menu():
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text='❌ Отмена', callback_data='youtube:cancel')]])


async def accept_url(message, url):
    if not get_settings().youtube_highlights_enabled:
        await message.answer('Поиск моментов временно отключён. Попробуй позже.')
        return
    try:
        url = youtube_url(url)
    except HighlightsError:
        await message.answer('Пришли ссылку на одно публичное YouTube-видео.')
        return
    now = time.monotonic()
    for key in list(_pending):
        if now-_pending[key].created > 1800:
            _pending.pop(key, None)
    if len(_pending) >= 256 and message.from_user.id not in _pending:
        await message.answer('Сейчас много запросов. Попробуй чуть позже.')
        return
    status = await message.answer(
        '✂️ Найду до 3 моментов по речи и применю твоё оформление.\n\n'
        'Бета: YouTube-видео от 2 до 60 минут с доступными субтитрами. '
        'Некоторые видео пока не поддерживаются.',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text='✂️ Найти лучшие моменты', callback_data='action:youtube_highlights')],
            [types.InlineKeyboardButton(text='❌ Отмена',callback_data='youtube:cancel')]]))
    _pending[message.from_user.id] = Pending(url,status.message_id,message.chat.id,now)


async def cancel(call):
    user_id = call.from_user.id
    running = _running.get(user_id)
    if running and running[0] == call.message.message_id:
        running[1].cancel()
        await call.answer('Останавливаю…')
        await call.message.edit_text('Обработка отменена.')
        return
    pending = _pending.get(user_id)
    if pending and pending.status_id == call.message.message_id:
        _pending.pop(user_id,None)
        await call.message.edit_text('Отменено. Можно отправить другую ссылку.')
    await call.answer()


async def render_options(bot, user_id, directory):
    settings = get_settings()
    async with db_manager.session() as session:
        s = await UserSettingsRepository(session).get_or_create(user_id)
    width,height,bitrate = output_geometry(s,settings)
    banner = logo = None
    if s.cta_enabled and s.cta_telegram_file_id:
        ext = s.overlay_type if s.overlay_type in {'png','webp','gif','mp4'} else 'png'
        banner = await download_asset(bot,s.cta_telegram_file_id,directory/f'banner.{ext}')
    elif s.cta_enabled and s.cta_asset_path:
        asset = Path(s.cta_asset_path)
        if asset.is_file():
            banner = asset
    if s.logo_enabled and s.logo_telegram_file_id:
        logo = await download_asset(bot,s.logo_telegram_file_id,directory/'logo.png')
        validate_image(logo)
    from app.bot.handlers.video import _resolve_title_text
    return dict(target_width=width,target_height=height,target_fps=settings.output_fps,
        video_bitrate=bitrate,audio_bitrate=settings.output_audio_bitrate,
        output_width=width,output_height=height, cta_asset=banner,
        cta_position=s.cta_position,cta_mode=s.cta_mode,
        cta_duration_seconds=s.cta_duration_seconds,cta_start_seconds=s.cta_start_seconds,
        cta_min_margin_px=settings.cta_min_margin_px,cta_size_preset=s.cta_size or 'medium',
        cta_overlay_type=s.overlay_type or 'png',overlay_is_animated=s.overlay_is_animated,
        background_id=s.background_id or 'blur',title_text=_resolve_title_text(s),
        brand_corner=s.brand_corner,decoration_id='mascot' if s.decoration_enabled else '',
        audio_preset=s.audio_preset or 'original',transformation_preset=s.style_id or 'custom',
        maximum_transform=s.processing_style=='maximum',subtitles_enabled=s.subtitles_enabled,
        subtitle_style=s.subtitle_style,subtitle_language=s.subtitle_language,
        logo_asset=logo,recut_branding=effective_branding(s))


ERRORS = {
    'YOUTUBE_AUTH_REQUIRED': 'YouTube отклонил запрос сервера и требует проверку входа. Обработка по этой ссылке сейчас недоступна.',
    'YOUTUBE_RATE_LIMITED': 'YouTube временно ограничил запросы. Попробуй позже.',
    'YOUTUBE_JS_FAILED': 'Не удалось пройти техническую проверку YouTube. Попробуй позже.',
    'YOUTUBE_VIDEO_UNAVAILABLE': 'YouTube сообщает, что видео недоступно. Проверь ссылку и доступ к видео.',
    'YOUTUBE_NETWORK_FAILED': 'Не удалось соединиться с YouTube. Попробуй позже.',
    'YOUTUBE_UNAVAILABLE': 'Не удалось получить данные видео с YouTube. Попробуй позже.',
    'PARTIAL_UNSUPPORTED': 'Для этого видео YouTube не отдал поток для загрузки фрагментов. Попробуй другое видео.',
    'CAPTIONS_UNAVAILABLE': 'Не удалось получить субтитры. Для поиска моментов пришли YouTube-видео с доступными субтитрами.',
    'SOURCE_UNSUPPORTED': 'Нужно публичное завершённое YouTube-видео от 2 до 60 минут.',
    'TRANSFER_LIMIT': 'Фрагменты оказались слишком большими. Попробуй другое видео.',
    'INVALID_TIMESTAMPS': 'Не удалось подобрать законченные короткие моменты. Попробуй другое видео.',
    'DISK_LIMIT': 'Сейчас недостаточно ресурсов для обработки. Попробуй позже.',
}


async def run(call):
    user_id = call.from_user.id
    from app.bot.handlers.video import _subscription_gate_blocked
    if call.message and await _subscription_gate_blocked(
            call.bot, user_id, call.message.answer):
        await call.answer()
        return
    pending = _pending.get(user_id)
    if (pending is None or pending.status_id != call.message.message_id
            or pending.chat_id != call.message.chat.id or time.monotonic()-pending.created>1800):
        await call.answer('Пришли YouTube-ссылку заново.',show_alert=True)
        return
    if not get_settings().youtube_highlights_enabled:
        await call.answer('Поиск моментов временно отключён.',show_alert=True)
        return
    await call.answer('🔍 Анализирую видео…')
    _pending.pop(user_id,None)
    _running[user_id] = (call.message.message_id,asyncio.current_task())
    job_id = None
    directory = get_temp_manager()._job_dir('youtube_'+uuid.uuid4().hex)
    sent = 0
    started = time.monotonic()
    result_text = 'Не удалось обработать видео. Попробуй другую ссылку.'
    async def status(text):
        try:
            await call.message.edit_text(text,reply_markup=cancel_menu())
        except Exception:
            pass
    try:
        settings = get_settings()
        directory.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(directory).free < 512*1024*1024:
            raise HighlightsError('DISK_LIMIT')
        async with db_manager.session() as session:
            repo = JobRepository(session)
            if await repo.has_active_job(user_id):
                raise HighlightsError('BUSY')
            job = await repo.create(telegram_user_id=user_id,telegram_chat_id=pending.chat_id,
                source_message_id=pending.status_id,source_filename='youtube-highlights',source_bytes=None)
            job_id = job.id
            await repo.set_status(job_id,JobStatus.ANALYZING)
        async with asyncio.timeout(settings.youtube_highlights_timeout_seconds):
            await status('🔍 Анализирую видео…')
            async with YouTubeSource(max_bytes=settings.youtube_highlights_max_mb*1024*1024,
                    max_duration=settings.youtube_highlights_max_minutes*60) as source:
                transcript = await source.prepare(pending.url)
                await status('🧠 Ищу интересные моменты…')
                analysed = await Analyser(target_count=3,min_seconds=20,max_seconds=60).analyse(transcript)
                cutter = ClipCutter(min_duration_seconds=20,max_duration_seconds=60)
                ranges = [cutter._adjust_boundaries(c,transcript.segments,source.duration)
                          for c in analysed.clips[:3]]
                if not getattr(source,'range_tracks',None):
                    selected = [select_parts(source.parts,a,b,source.duration) for a,b in ranges]
                    union = {p for group in selected for p in group}
                    if len(union) >= len(source.parts):
                        raise HighlightsError('PARTIAL_UNSUPPORTED')
                options = await render_options(call.bot,user_id,directory)
                for index,(start,end) in enumerate(ranges,1):
                    clip_dir = directory/f'clip_{index}'
                    await status(f'✂️ Подготавливаю фрагменты… {index}/{len(ranges)}')
                    path = await source.download_clip(start,end,clip_dir)
                    segments = tuple(Segment(start=max(0,s.start-start),end=min(end,s.end)-start,text=s.text)
                        for s in transcript.segments if s.end>start and s.start<end)
                    result = await QuickPrepPipeline().run(input_video=path,job_dir=clip_dir/'render',
                        subtitle_segments=segments,**options)
                    await status(f'📤 Отправляю ролики… {index}/{len(ranges)}')
                    final = FinalClip(index=index,final_path=result.final_path,
                        has_subtitles=result.has_subtitles,has_cta=result.has_cta,size_bytes=result.size_bytes)
                    delivery = await TelegramSender(call.bot).send(FinalJob(clips=(final,)),pending.chat_id)
                    if not delivery.sent:
                        raise HighlightsError('DELIVERY_FAILED')
                    sent += 1
                    shutil.rmtree(clip_dir)
                result_text = f'✅ Готово: {sent} ролика. Отправь следующую YouTube-ссылку.'
    except asyncio.CancelledError:
        result_text = 'Обработка отменена.'
    except TimeoutError:
        result_text = 'Обработка заняла слишком много времени. Попробуй другое видео.'
    except Exception as exc:
        code = str(exc) if isinstance(exc,HighlightsError) else type(exc).__name__
        logger.warning('youtube_highlights_failed',job_id=job_id,error_code=code)
        result_text = ERRORS.get(code,'Не удалось обработать видео. Попробуй другую ссылку.')
    finally:
        if sent:
            if not result_text.startswith('✅'):
                result_text = f'Отправлено роликов: {sent}. '+result_text
        if job_id is not None:
            try:
                async with db_manager.session() as session:
                    repo = JobRepository(session)
                    if sent:
                        await repo.mark_completed(job_id,clips_generated=sent)
                    else:
                        await repo.mark_failed(job_id,'YOUTUBE_HIGHLIGHTS_FAILED','')
            except Exception:
                logger.warning('youtube_highlights_status_failed',job_id=job_id)
        if sent:
            from app.bot.handlers.video import _offer_subscription_after_first_result
            await _offer_subscription_after_first_result(call.bot, user_id, call.message)
        shutil.rmtree(directory,ignore_errors=True)
        _running.pop(user_id,None)
        from app.bot.handlers.video import _mode_state
        _mode_state[user_id] = 'moments'
        try:
            await call.message.edit_text(result_text)
        except Exception:
            pass
        logger.info('youtube_highlights_finished',job_id=job_id,sent=sent,
                    elapsed_seconds=round(time.monotonic()-started,2))


def register(router):
    router.callback_query.register(run,F.data=='action:youtube_highlights')
    router.callback_query.register(cancel,F.data=='youtube:cancel')
