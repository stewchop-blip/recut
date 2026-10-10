"""Regression coverage for one-click saved appearance and server-side entitlements."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database.models import Base, UserSettings
from app.database.repositories import UserSettingsRepository
from app.services.appearance import effective_branding, is_premium, validate_image, output_geometry


@pytest.fixture
async def database(tmp_path, monkeypatch):
    from app.database.session import db_manager
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/appearance.db')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with factory.begin() as s:
            yield s
    monkeypatch.setattr(db_manager, 'session', session)
    yield factory
    await engine.dispose()


@pytest.mark.parametrize('expiry,preference,expected', [
    (None, False, True), (-1, False, True), (1, False, False), (1, True, True)])
def test_branding_is_checked_on_backend(expiry, preference, expected):
    s = SimpleNamespace(premium_until=None if expiry is None else datetime.now(timezone.utc)+timedelta(days=expiry),
                        recut_branding=preference)
    assert effective_branding(s) is expected


async def test_defaults_and_preferences_are_user_scoped(database):
    async with database.begin() as session:
        repo = UserSettingsRepository(session)
        s = await repo.get_or_create(10)
        assert s.style_id == 'clean' and not s.subtitles_enabled
        assert not s.logo_enabled and effective_branding(s)
        await repo.update_fields(10, logo_telegram_file_id='logo-owner-10', logo_enabled=True,
                                output_mode='square', subtitle_language='en')
        other = await repo.get_or_create(20)
        assert other.logo_telegram_file_id is None and other.output_mode == 'universal_9_16'
    async with database.begin() as session:
        saved = await UserSettingsRepository(session).get(10)
        assert saved.logo_enabled and saved.logo_telegram_file_id == 'logo-owner-10'
        assert saved.subtitle_language == 'en'


@pytest.mark.parametrize('mode', ['prepare', 'custom'])
def test_primary_actions_are_exactly_three(mode):
    from app.bot.keyboards.inline import mode_input_menu
    buttons = [button for row in mode_input_menu(mode).inline_keyboard for button in row]
    assert [button.text for button in buttons] == ['✨ Сделать ролик', '📥 Оригинал', '⚙️ Оформление']


@pytest.mark.parametrize('mode,expected', [('square',(1080,1080)), ('portrait_4_5',(1080,1350)), ('universal_9_16',(1080,1920))])
def test_saved_geometry(mode, expected):
    defaults = SimpleNamespace(output_width=1080, output_video_bitrate='6M')
    assert output_geometry(SimpleNamespace(output_mode=mode, output_quality='standard'), defaults)[:2] == expected


def test_untrusted_image_validation(tmp_path):
    image = tmp_path/'not-an-extension.bin'
    Image.new('RGBA', (100,50)).save(image, format='PNG')
    assert validate_image(image) == ('png',False)
    image.write_bytes(b'not an image')
    with pytest.raises(Exception):
        validate_image(image)
    Image.new('RGB',(4097,10)).save(image,format='PNG')
    with pytest.raises(ValueError, match='ASSET_DIMENSIONS'):
        validate_image(image)


async def test_free_toggle_cannot_disable_branding(database):
    from app.bot.handlers.appearance import toggle_branding
    call = SimpleNamespace(from_user=SimpleNamespace(id=10),answer=AsyncMock(),
                           message=SimpleNamespace(edit_text=AsyncMock()))
    await toggle_branding(call)
    assert 'Premium' in call.message.edit_text.call_args.args[0]
    async with database.begin() as session:
        assert (await UserSettingsRepository(session).get(10)).recut_branding


async def test_premium_toggle_persists(database):
    from app.bot.handlers.appearance import toggle_branding
    async with database.begin() as session:
        await UserSettingsRepository(session).update_fields(10,
            premium_until=datetime.now(timezone.utc)+timedelta(days=1))
    call = SimpleNamespace(from_user=SimpleNamespace(id=10),answer=AsyncMock(),
                           message=SimpleNamespace(edit_text=AsyncMock()))
    await toggle_branding(call)
    async with database.begin() as session:
        s = await UserSettingsRepository(session).get(10)
        assert not s.recut_branding and not effective_branding(s)


async def test_queue_blocks_double_clicks_and_restores_policy(database):
    from app.bot.middlewares.render_queue import RenderQueueMiddleware, active_render_users
    from app.services.appearance import branding_required
    queue = RenderQueueMiddleware()
    event = SimpleNamespace(from_user=SimpleNamespace(id=10), data='action:quick_prep',answer=AsyncMock(),message=None)
    started, release = asyncio.Event(), asyncio.Event()
    async def work(event, data):
        assert branding_required.get() is True
        started.set()
        await release.wait()
        raise RuntimeError('failure')
    task = asyncio.create_task(queue(work,event,{}))
    await started.wait()
    duplicate = AsyncMock()
    await queue(duplicate,event,{})
    duplicate.assert_not_called()
    release.set()
    with pytest.raises(RuntimeError):
        await task
    assert 10 not in active_render_users and branding_required.get() is None


async def test_single_action_applies_saved_settings(database,tmp_path,monkeypatch):
    from app.bot.handlers import video
    from app.pipeline.quick_prep import QuickPrepResult
    async with database.begin() as session:
        await UserSettingsRepository(session).update_fields(10,subtitles_enabled=True,
            subtitle_style='large',output_mode='square',output_quality='compact',subtitle_language='en',
            processing_style='maximum',audio_preset='none')
    source=tmp_path/'original.mp4'
    source.write_bytes(b'source')
    video._pending_jobs[10]=video._PendingJob(0,10,str(source),str(tmp_path),1)
    call=SimpleNamespace(from_user=SimpleNamespace(id=10),data='action:quick_prep',answer=AsyncMock(),
        bot=SimpleNamespace(),message=SimpleNamespace(edit_text=AsyncMock()))
    output=tmp_path/'final.mp4';output.write_bytes(b'output')
    result=QuickPrepResult(output,6,720,720,1,False,True)
    run=AsyncMock(return_value=result)
    monkeypatch.setattr(video.QuickPrepPipeline,'run',run)
    monkeypatch.setattr(video.TelegramSender,'send',AsyncMock(return_value=SimpleNamespace(sent=1)))
    await video.on_quick_prep(call)
    kwargs=run.call_args.kwargs
    assert kwargs['subtitles_enabled'] and kwargs['recut_branding'] and kwargs['maximum_transform']
    assert (kwargs['target_width'],kwargs['target_height']) == (720,720)
    assert kwargs['subtitle_language'] == 'en' and kwargs['audio_preset'] == 'none'
    assert kwargs['job_dir'] != source.parent and source.exists()


@pytest.mark.parametrize('subtitles', [False,True])
async def test_real_export_with_logo_and_branding(tmp_path,subtitles):
    from tests.test_quick_prep import _make_test_video
    from app.pipeline.quick_prep import QuickPrepPipeline
    source=tmp_path/'source.mp4';_make_test_video(source,duration=1,width=320,height=240)
    logo=tmp_path/'logo.png';Image.new('RGBA',(100,100),(255,0,0,255)).save(logo)
    with patch('app.pipeline.appearance_stages.add_subtitles',new=AsyncMock(side_effect=RuntimeError('speech unavailable'))) as speech:
        result=await QuickPrepPipeline().run(source,tmp_path/'job',target_width=320,target_height=400,
            target_fps=30,video_bitrate='1M',audio_bitrate='96k',cta_asset=None,cta_position='bottom',
            cta_mode='end',cta_duration_seconds=4,cta_start_seconds=0,cta_min_margin_px=20,
            output_width=320,output_height=400,transformation_preset='clean',recut_branding=True,
            logo_asset=logo,subtitles_enabled=subtitles)
    assert result.final_path.is_file() and (result.width,result.height)==(320,400)
    assert bool(result.warnings) is subtitles
    assert speech.await_count == int(subtitles)


async def test_subtitle_stage_uses_final_timing_and_cleans_ass(tmp_path,monkeypatch):
    from app.pipeline.appearance_stages import add_subtitles
    from app.pipeline.transcriber import Transcriber
    from app.services.transcription.base import Segment
    from app.services.subtitles.ass import AssSubtitleBuilder
    source=tmp_path/'video.mp4';source.touch()
    async def burn(video,ass,output):
        text=ass.read_text()
        assert 'PlayResX: 720' in text and 'Arial,47,' in text
        assert '（\\\\pos' not in text
        output.touch()
    media=SimpleNamespace(extract_audio=AsyncMock(),probe=AsyncMock(return_value={
        'streams':[{'codec_type':'audio'},{'codec_type':'video','width':720,'height':900}]}),burn_subtitles=burn)
    monkeypatch.setattr(Transcriber,'transcribe',AsyncMock(return_value=SimpleNamespace(
        duration_seconds=2,segments=(Segment(0,2,'Hello world'),))))
    output=await add_subtitles(media,source,tmp_path,style='large',language='en')
    assert output.exists() and (tmp_path/'subtitles.ass').exists()


async def test_original_failure_preserves_source_without_processing(database,tmp_path,monkeypatch):
    from app.bot.handlers import video
    source=tmp_path/'source.mp4';source.write_bytes(b'original bytes')
    user_id=123
    video._pending_jobs[user_id]=video._PendingJob(0,user_id,str(source),str(tmp_path),1)
    pipeline=AsyncMock()
    monkeypatch.setattr(video.QuickPrepPipeline,'run',pipeline)
    call=SimpleNamespace(from_user=SimpleNamespace(id=user_id),answer=AsyncMock(),message=SimpleNamespace(
        answer_video=AsyncMock(side_effect=RuntimeError('upload failed')),
        answer_document=AsyncMock(side_effect=RuntimeError('upload failed')), edit_text=AsyncMock()))
    try:
        await video.on_url_original(call)
        assert source.read_bytes()==b'original bytes'
        assert video._get_pending(user_id) is not None
        pipeline.assert_not_called()
    finally:
        video._pop_pending(user_id)
