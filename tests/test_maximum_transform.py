"""Maximum mode: route to a real render, preserve geometry and A/V timing."""
import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from app.bot.keyboards.inline import MORE_MENU, mode_input_menu
from app.services.auto_edit_planner import MaximumTransformProfile, _MAX_PROFILES
from app.services.media.ffmpeg import MediaService


def run(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def metadata(path):
    return json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-show_format',
                          '-of', 'json', str(path)))


def source_file(path, size='180x320', audio=True, duration=4):
    args = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            f'testsrc2=s={size}:r=24:d={duration}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}']
    run(*args, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path))


def test_mode_is_reachable_and_carries_its_own_action():
    assert any(b.callback_data == 'mode:maximum_transform'
               for row in MORE_MENU.inline_keyboard for b in row)
    assert mode_input_menu('maximum_transform').inline_keyboard[0][0].callback_data == 'action:maximum_transform'
    assert mode_input_menu('prepare').inline_keyboard[0][0].callback_data == 'action:quick_prep'


def test_profile_stable_across_processes(tmp_path):
    path = tmp_path / 'source.bin'
    path.write_bytes(b'same video bytes')
    expected = MaximumTransformProfile.for_source(path).name
    code = ('from pathlib import Path; from app.services.auto_edit_planner import MaximumTransformProfile; '
            'import sys; print(MaximumTransformProfile.for_source(Path(sys.argv[1])).name)')
    for seed in ('1', '123'):
        result = subprocess.run(['python', '-c', code, str(path)],
            env={**os.environ, 'PYTHONHASHSEED': seed}, capture_output=True, check=True, text=True)
        assert result.stdout.strip().endswith(expected)


@pytest.mark.parametrize('plan,bg,size,audio', [
    (_MAX_PROFILES[0], 'blur', '180x320', True),
    (_MAX_PROFILES[1], 'dark', '320x180', True),
    (_MAX_PROFILES[2], 'accent', '240x240', False),
    (_MAX_PROFILES[3], 'blur', '180x320', True),
])
async def test_real_render_timing_and_pitch(tmp_path, plan, bg, size, audio):
    source, output = tmp_path / 'source.mp4', tmp_path / 'out.mp4'
    source_file(source, size, audio)
    await MediaService().make_vertical(source, output,
        target_width=360, target_height=640, target_fps=24,
        maximum_plan=plan, speed=plan.speed, layout_id=plan.layout_id,
        audio_preset='dynamic', background_id=bg)
    data = metadata(output)
    video = next(s for s in data['streams'] if s['codec_type'] == 'video')
    assert (video['width'], video['height']) == (360, 640)
    assert video['sample_aspect_ratio'] == '1:1'
    assert abs(float(video['duration']) - 4 / plan.speed) < 0.13
    streams = [s for s in data['streams'] if s['codec_type'] == 'audio']
    assert bool(streams) == audio
    if audio:
        assert abs(float(streams[0]['duration']) - float(video['duration'])) < 0.13
        samples = np.frombuffer(run('ffmpeg', '-v', 'error', '-i', str(output), '-vn',
            '-ac', '1', '-ar', '8000', '-f', 'f32le', '-'), dtype=np.float32)
        freq = np.fft.rfftfreq(len(samples), 1 / 8000)[np.argmax(abs(np.fft.rfft(samples)))]
        assert abs(freq - 440 * 2 ** (plan.pitch_semitones / 12)) < 2


async def test_full_pipeline_with_banner(tmp_path):
    from PIL import Image
    from app.pipeline.quick_prep import QuickPrepPipeline
    source, banner = tmp_path / 'source.mp4', tmp_path / 'banner.png'
    source_file(source, duration=4)
    Image.new('RGBA', (300, 60), (255, 40, 40, 255)).save(banner)
    result = await QuickPrepPipeline().run(source, tmp_path / 'job',
        maximum_transform=True, decoration_id="mascot", target_width=360, target_height=640, target_fps=24,
        video_bitrate='1M', audio_bitrate='128k', cta_asset=banner,
        cta_position='bottom', cta_mode='end', cta_duration_seconds=1,
        cta_start_seconds=0, cta_min_margin_px=12, output_width=360, output_height=640)
    assert result.has_cta
    assert result.final_path.is_file()
    assert (result.width, result.height) == (360, 640)
    plan = MaximumTransformProfile.for_source(source)
    assert abs(result.duration_seconds - 4 / plan.speed) < 0.2
    # Banner really reaches the final pixels (not just a has_cta flag).
    raw = run('ffmpeg', '-v', 'error', '-ss', str(result.duration_seconds - 0.4),
        '-i', str(result.final_path), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-')
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(640, 360, 3)
    red = (pixels[480:, :, 0] > 200) & (pixels[480:, :, 1] < 80) & (pixels[480:, :, 2] < 80)
    assert red.sum() > 1500


async def test_existing_source_can_switch_to_maximum(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.bot.handlers import video
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'pending')
    user_id = 987654
    pending = video._PendingJob(job_id=7, chat_id=1, input_path=str(source),
        job_dir=str(tmp_path), status_message_id=1)
    monkeypatch.setitem(video._pending_jobs, user_id, pending)
    call = SimpleNamespace(from_user=SimpleNamespace(id=user_id), data='mode:maximum_transform',
        message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    try:
        await video.on_mode_selected(call)
        keyboard = call.message.edit_text.call_args.kwargs['reply_markup']
        assert keyboard.inline_keyboard[0][0].callback_data == 'action:maximum_transform'
    finally:
        video._mode_state.pop(user_id, None)


@pytest.mark.parametrize('action,enabled', [('action:maximum_transform', True), ('action:quick_prep', False)])
async def test_callback_passes_mode_to_pipeline(tmp_path, monkeypatch, action, enabled):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.bot.handlers import video

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(video.db_manager, 'session', session)
    monkeypatch.setattr(video, 'UserSettingsRepository', lambda _: SimpleNamespace(get=AsyncMock(return_value=None)))
    pipeline = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError('stop after handoff')))
    monkeypatch.setattr(video, 'QuickPrepPipeline', lambda: pipeline)
    monkeypatch.setattr(video, '_edit_status', AsyncMock())
    monkeypatch.setattr(video, '_fail_job', AsyncMock())
    monkeypatch.setattr(video, 'get_temp_manager', lambda: SimpleNamespace(cleanup_job=Mock()))
    user_id = 987655
    monkeypatch.setitem(video._pending_jobs, user_id, video._PendingJob(
        job_id=7, chat_id=1, input_path=str(tmp_path / 'source.mp4'),
        job_dir=str(tmp_path), status_message_id=1))
    # No in-memory mode: upload already consumed it. Callback must suffice.
    video._mode_state.pop(user_id, None)
    call = SimpleNamespace(from_user=SimpleNamespace(id=user_id), data=action,
        message=object(), answer=AsyncMock())
    await video.on_quick_prep(call)
    assert pipeline.run.await_count == 1
    assert pipeline.run.call_args.kwargs['maximum_transform'] is enabled
