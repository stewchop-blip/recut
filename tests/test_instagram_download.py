import asyncio
import base64
import json
from pathlib import Path

import pytest

from app.pipeline.url_downloader import (DownloaderService, URLDownloadError,
    StructuredDownloadError, VideoTooLongError, classify_ytdlp_error)


@pytest.mark.parametrize('message,code', [
    ('Requested content is not available, rate-limit reached or login required. Use cookies', 'INSTAGRAM_ACCESS_FAILED'),
    ('Instagram sent an empty media response. Use --cookies-from-browser', 'INSTAGRAM_EXTRACTOR_FAILED'),
    ('HTTP Error 429: Too Many Requests', 'INSTAGRAM_RATE_LIMITED'),
    ('The webpage request was redirected to the login page. You have exceeded the rate-limit for accessing posts anonymously', 'INSTAGRAM_AUTH_REQUIRED'),
    ('Login required', 'INSTAGRAM_AUTH_REQUIRED'),
    ('Restricted Video', 'INSTAGRAM_RESTRICTED'),
    ('Download timed out. See cookies help', 'DOWNLOAD_TIMEOUT'),
])
def test_error_is_not_inferred_from_generic_cookie_advice(message, code):
    assert classify_ytdlp_error(message, 'instagram') == code


@pytest.mark.parametrize('cookie_retry', [False, True])
async def test_instagram_reuses_metadata_and_cookie_context(tmp_path, monkeypatch, cookie_retry):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    if cookie_retry:
        monkeypatch.setenv('INSTAGRAM_COOKIES_B64', base64.b64encode(b'# Netscape HTTP Cookie File\n').decode())
    else:
        monkeypatch.delenv('INSTAGRAM_COOKIES_B64', raising=False)
    svc = DownloaderService()
    calls = []
    cached = []
    meta = {'id':'test', 'title':'clip', 'duration':3, 'formats':[{'url':'https://example.com/video.mp4'}]}
    async def run(args, **kwargs):
        calls.append(args)
        if '--dump-json' in args:
            assert args[-1] == 'https://www.instagram.com/reel/abc/'
            if cookie_retry and '--cookies' not in args:
                raise URLDownloadError('Instagram sent an empty media response. Use cookies')
            return [meta]
        assert '--load-info-json' in args
        assert 'https://www.instagram.com/reel/abc/' not in args
        assert ('--cookies' in args) == cookie_retry
        path = Path(args[args.index('--load-info-json')+1])
        cached.append(path)
        assert json.loads(path.read_text()) == meta
        assert path.stat().st_mode & 0o077 == 0
        (tmp_path/'download.mp4').write_bytes(b'video')
        (tmp_path/'download.mp4.part').write_bytes(b'partial')
        return []
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    result = await svc.download('https://www.instagram.com/reel/abc/?stkn=tracking', tmp_path)
    assert result.path.name == 'download.mp4'
    assert len(calls) == 2
    assert ('--cookies' in calls[0]) == cookie_retry
    assert '--sleep-requests' in calls[0]
    assert all(not p.exists() for p in cached)
    assert not list(tmp_path.glob('*cookies*'))


@pytest.mark.parametrize('cancel', [False, True])
async def test_info_file_removed_on_failure_or_cancellation(tmp_path, monkeypatch, cancel):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    svc = DownloaderService()
    async def run(args, **kwargs):
        if '--dump-json' in args:
            return [{'duration':2}]
        raise asyncio.CancelledError() if cancel else URLDownloadError('HTTP Error 429')
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    with pytest.raises(asyncio.CancelledError if cancel else StructuredDownloadError):
        await svc.download('https://www.instagram.com/reel/abc/', tmp_path)
    assert not list(tmp_path.glob('instagram_info_*'))


async def test_metadata_limit_still_prevents_transfer(tmp_path, monkeypatch):
    svc = DownloaderService()
    calls = []
    async def run(args, **kwargs):
        calls.append(args)
        return [{'duration':100}]
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    with pytest.raises(VideoTooLongError):
        await svc.download('https://www.instagram.com/reel/abc/', tmp_path, max_duration_seconds=10)
    assert len(calls) == 1


async def test_failed_cookie_retry_stays_structured_and_cleans_cookie(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setenv('INSTAGRAM_COOKIES_B64', base64.b64encode(b'placeholder').decode())
    svc = DownloaderService()
    calls = []
    async def run(args, **kwargs):
        calls.append(args)
        raise URLDownloadError('Login required')
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    with pytest.raises(StructuredDownloadError, match='INSTAGRAM_AUTH_REQUIRED'):
        await svc.download('https://www.instagram.com/reel/abc/', tmp_path)
    assert len(calls) == 1
    assert not list(tmp_path.glob('*cookies*'))


@pytest.mark.parametrize('source', [['https://www.instagram.com/reel/abc/'],
                                   ['--load-info-json', '/tmp/instagram_info_test.json']])
async def test_instagram_subprocess_error_never_exposes_signed_url(source, monkeypatch):
    from types import SimpleNamespace
    from app.pipeline import url_downloader
    events = []
    monkeypatch.setattr(url_downloader, 'logger', SimpleNamespace(
        warning=lambda *args, **kwargs: events.append(kwargs)))
    class Process:
        returncode = 1
        async def communicate(self):
            return b'', b'ERROR: HTTP Error 403 https://video.fbcdn.net/video.mp4?token=SECRET'
    async def create(*args, **kwargs): return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    with pytest.raises(StructuredDownloadError) as error:
        await DownloaderService()._run_ytdlp(['yt-dlp', *source])
    assert error.value.code == 'INSTAGRAM_EXTRACTOR_FAILED'
    assert 'SECRET' not in str(error.value)
    assert 'fbcdn' not in str(events)
