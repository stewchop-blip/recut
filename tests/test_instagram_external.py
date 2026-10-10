import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.pipeline import instagram_external as external
from app.pipeline.url_downloader import DownloaderService, StructuredDownloadError, VideoTooLongError

URL = 'https://www.instagram.com/reel/abc/'
SIGNED = 'https://video.cdninstagram.com/video.mp4?signature=PRIVATE'


def payload():
    return {'success': True, 'data': {'canonical_url': URL, 'content_kind': 'reel',
        'items': [{'id': 'abc', 'media_type': 'video', 'duration_seconds': 4,
                   'variants': [{'url': SIGNED, 'ext': 'mp4', 'hasAudio': True,
                                 'width': 720, 'height': 1280}]}]}}


def test_metadata_preserves_audio():
    p = payload()
    p['data']['items'][0]['variants'].append({'url': SIGNED, 'ext': 'mp4',
        'hasAudio': False, 'width': 1920, 'height': 1080})
    assert external.external_metadata(p, 'abc')['formats'] == [
        {'url': SIGNED, 'ext': 'mp4', 'width': 720, 'height': 1280}]


@pytest.mark.parametrize('url', ['http://instagram.com/reel/abc/',
    'https://instagram.com.evil.test/reel/abc/', 'https://localhost/reel/abc/',
    'https://u:p@instagram.com/reel/abc/', 'https://instagram.com:8443/reel/abc/',
    'https://instagram.com/accounts/login/', None])
async def test_invalid_input_does_not_call_network(monkeypatch, url):
    network = AsyncMock()
    monkeypatch.setattr(external, 'AsyncSession', network)
    assert await external.extract_external_video(url) is None
    network.assert_not_called()


@pytest.mark.parametrize('change', ['identity', 'canonical', 'ssrf', 'audio', 'dimensions',
                                    'duration', 'carousel', 'failure'])
def test_untrusted_metadata_rejected(change):
    p = payload()
    item = p['data']['items'][0]
    if change == 'identity': item['id'] = 'other'
    if change == 'canonical': p['data']['canonical_url'] = 'https://evil.test/reel/abc/'
    if change == 'ssrf': item['variants'][0]['url'] = 'https://127.0.0.1/video'
    if change == 'audio': item['variants'][0]['hasAudio'] = False
    if change == 'dimensions': item['variants'][0]['height'] = True
    if change == 'duration': item['duration_seconds'] = float('nan')
    if change == 'carousel': p['data']['items'].append(item)
    if change == 'failure': p['success'] = False
    assert external.external_metadata(p, 'abc') is None


def fake_session(monkeypatch, body, status=200, error=None):
    class Response:
        status_code = status
        async def aiter_content(self):
            if error: raise error
            yield body
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            assert method == 'POST' and url == external.ENDPOINT
            assert kwargs['json'] == {'instagram_url': URL}
            yield Response()
    monkeypatch.setattr(external, 'AsyncSession', lambda **kwargs: Session())


async def test_stream_success_strips_tracking(monkeypatch):
    fake_session(monkeypatch, json.dumps(payload()).encode())
    assert (await external.extract_external_video(URL + '?igsh=private'))['id'] == 'abc'


@pytest.mark.parametrize('status,body', [(302, b'private'), (403, b'private'),
    (200, b'not JSON'), (200, b'x' * (external.MAX_RESPONSE_BYTES + 1))])
async def test_bounded_safe_failure(monkeypatch, status, body):
    fake_session(monkeypatch, body, status)
    assert await external.extract_external_video(URL) is None


async def test_cancellation_propagates(monkeypatch):
    fake_session(monkeypatch, b'', error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await external.extract_external_video(URL)


async def test_disabled_never_calls_network(monkeypatch):
    monkeypatch.setenv('INSTAGRAM_EXTERNAL_FALLBACK', 'false')
    network = AsyncMock()
    monkeypatch.setattr(external, 'AsyncSession', network)
    assert await external.extract_external_video(URL) is None
    network.assert_not_called()


@pytest.mark.parametrize('available,limit', [(True, False), (False, False), (True, True)])
async def test_pipeline_recovers_after_local_exhaustion(tmp_path, monkeypatch, available, limit):
    monkeypatch.delenv('INSTAGRAM_COOKIES_B64', raising=False)
    monkeypatch.setattr('app.pipeline.instagram_resolver.extract_resolved_video', AsyncMock(return_value=None))
    monkeypatch.setattr('app.pipeline.instagram_public.extract_public_video', AsyncMock(side_effect=ValueError('gated')))
    fallback = AsyncMock(return_value=external.external_metadata(payload(), 'abc') if available else None)
    monkeypatch.setattr(external, 'extract_external_video', fallback)
    service = DownloaderService()
    calls = []
    async def run(args, **kwargs):
        calls.append(args)
        if '--dump-json' in args:
            raise StructuredDownloadError('INSTAGRAM_AUTH_REQUIRED', 'Instagram request failed')
        info = json.loads(open(args[args.index('--load-info-json') + 1]).read())
        assert info['formats'][0]['url'] == SIGNED
        (tmp_path / 'download.mp4').write_bytes(b'video')
        return []
    monkeypatch.setattr(service, '_run_ytdlp', run)
    if limit or not available:
        with pytest.raises(VideoTooLongError if limit else StructuredDownloadError):
            await service.download(URL, tmp_path, max_duration_seconds=1 if limit else 600)
        assert len(calls) == 1
    else:
        assert (await service.download(URL, tmp_path)).duration_seconds == 4
    fallback.assert_awaited_once_with(URL)


async def test_provider_diagnostics_do_not_log_content(monkeypatch):
    events = []
    class Logger:
        def info(self, event, **fields): events.append((event, fields))
        warning = info
    monkeypatch.setattr(external, 'logger', Logger())
    fake_session(monkeypatch, json.dumps(payload()).encode())
    assert await external.extract_external_video(URL)
    fake_session(monkeypatch, json.dumps({'success': False, 'message': SIGNED}).encode())
    assert await external.extract_external_video(URL) is None
    assert SIGNED not in str(events)


@pytest.mark.parametrize('url', [URL, 'https://www.tiktok.com/@test/video/123',
                                'https://www.youtube.com/shorts/abc'])
async def test_successful_download_never_calls_external(tmp_path, monkeypatch, url):
    network = AsyncMock()
    monkeypatch.setattr(external, 'extract_external_video', network)
    service = DownloaderService()
    async def run(args, **kwargs):
        if '--dump-json' in args:
            return [{'title': 'video', 'duration': 4}]
        (tmp_path / 'download.mp4').write_bytes(b'video')
        return []
    monkeypatch.setattr(service, '_run_ytdlp', run)
    await service.download(url, tmp_path)
    network.assert_not_called()
