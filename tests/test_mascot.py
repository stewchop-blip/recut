"""Actual alpha composition, motion and persisted opt-in, with shared rendering."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import subprocess

import numpy as np
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database.models import Base
from app.database.repositories import UserSettingsRepository
from app.services.media.ffmpeg import MediaService
from app.services.overlays.templates import DECORATIONS


def run(*args):
    return subprocess.run(args, capture_output=True, check=True).stdout


def frame(path, t):
    raw = run('ffmpeg', '-v', 'error', '-ss', str(t), '-i', str(path),
        '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-')
    return np.frombuffer(raw, dtype=np.uint8).reshape(640, 360, 3)


def test_asset_has_real_alpha():
    asset = Image.open(DECORATIONS['mascot'].path)
    assert asset.mode == 'RGBA'
    assert asset.getchannel('A').getextrema() == (0, 255)
    assert asset.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize('banner', [False, True])
async def test_mascot_real_render_with_title_audio_and_motion(tmp_path, banner):
    source, output, baseline = [tmp_path / name for name in ('source.mp4','mascot.mp4','off.mp4')]
    run('ffmpeg','-v','error','-f','lavfi','-i','color=gray:s=180x320:r=24:d=3',
        '-f','lavfi','-i','sine=frequency=440:duration=3',
        '-c:v','libx264','-pix_fmt','yuv420p',str(source))
    kwargs = dict(target_width=360, target_height=640, target_fps=24,
        title_text='Тест', brand_corner=True, subtle_particles=False,
        background_id='dark', layout_id='full')
    media = MediaService()
    await media.make_vertical(source, output, decoration_id='mascot',
        decoration_avoid_bottom_banner=banner, **kwargs)
    await media.make_vertical(source, baseline, **kwargs)
    before, after = frame(baseline, 0.2), frame(output, 0.2)
    delta = np.abs(after.astype(float)-before.astype(float)).mean(axis=2)
    assert np.count_nonzero(delta > 20) > 700
    assert delta[:300].mean() < 1, 'Insert must stay in the lower region'
    assert delta[:,150:].mean() < 1, 'No opaque full-frame background'
    if banner:
        assert delta[470:].mean() < 1, 'Leave lower banner region free'
    assert np.mean(np.abs(frame(output,1.0).astype(float)-frame(output,2.8))) > 0.05
    data=json.loads(run('ffprobe','-v','error','-show_streams','-of','json',str(output)))
    assert any(s['codec_type']=='audio' for s in data['streams'])
    assert abs(float(data['streams'][0]['duration'])-3)<0.1


async def test_decoration_toggle_persists(tmp_path, monkeypatch):
    from app.database.session import db_manager
    from app.bot.handlers import video
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/prefs.db')
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with sessions.begin() as s:
            yield s
    monkeypatch.setattr(db_manager,'session',session)
    monkeypatch.setattr(video,'on_appearance_menu',AsyncMock())
    call=SimpleNamespace(from_user=SimpleNamespace(id=44))
    try:
        async with sessions.begin() as s:
            assert not (await UserSettingsRepository(s).get_or_create(44)).decoration_enabled
        await video.toggle_decoration(call)
        async with sessions.begin() as s:
            assert (await UserSettingsRepository(s).get(44)).decoration_enabled
        await video.toggle_decoration(call)
        async with sessions.begin() as s:
            assert not (await UserSettingsRepository(s).get(44)).decoration_enabled
    finally:
        await engine.dispose()


async def test_three_versions_reaches_real_shared_renderer(tmp_path):
    from app.pipeline.three_versions import ThreeVersionsPipeline
    class SmallMedia(MediaService):
        async def make_vertical(self, *args, **kwargs):
            return await super().make_vertical(*args, target_width=360,target_height=640,
                target_fps=24,**kwargs)
    source=tmp_path/'source.mp4'
    run('ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=180x320:r=24:d=3',
        '-c:v','libx264','-pix_fmt','yuv420p',str(source))
    results=await ThreeVersionsPipeline(media=SmallMedia()).run(source,tmp_path/'versions',
        cta_asset=None,cta_position='bottom',cta_margin_px=10,decoration_id='mascot')
    assert len(results)==3
    for result in results:
        assert result.final_path.stat().st_size>0
        data=json.loads(run('ffprobe','-v','error','-show_streams','-of','json',str(result.final_path)))
        assert (data['streams'][0]['width'],data['streams'][0]['height'])==(360,640)
