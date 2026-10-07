import asyncio
import pytest
from app.pipeline.instagram_public import video_info
from app.pipeline.url_downloader import DownloaderService, URLDownloadError, StructuredDownloadError


def payload(url='https://video.cdninstagram.com/v.mp4', private=False, code='abc'):
    return {'data': {'xdt_api__v1__media__shortcode__web_info': {'items': [
        {'code': code, 'user': {'is_private': private}, 'video_duration': 4,
         'video_versions': [{'url': url, 'width': 720, 'height': 1280}]}]}}}


def test_public_video_metadata():
    info = video_info(payload(), 'abc')
    assert info['duration'] == 4
    assert info['formats'][0]['height'] == 1280


@pytest.mark.parametrize('changes', [{'url':'http://video.cdninstagram.com/a'},
    {'url':'https://127.0.0.1/a'}, {'url':'https://cdninstagram.com.evil.test/a'},
    {'private':True}, {'code':'another'}])
def test_reject_untrusted_or_private_media(changes):
    with pytest.raises(ValueError):
        video_info(payload(**changes), 'abc')


@pytest.mark.asyncio
async def test_fallback_reuses_normal_download_pipeline(tmp_path, monkeypatch):
    monkeypatch.delenv('INSTAGRAM_COOKIES_B64', raising=False)
    calls=[]
    async def extract(url):
        calls.append(url)
        return video_info(payload(), 'abc')
    monkeypatch.setattr('app.pipeline.instagram_public.extract_public_video', extract)
    async def run(args, **kwargs):
        if '--dump-json' in args:
            raise URLDownloadError('The webpage request was redirected to the login page')
        assert '--load-info-json' in args
        (tmp_path/'download.mp4').write_bytes(b'video')
    svc=DownloaderService()
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    result=await svc.download('https://www.instagram.com/reel/abc/', tmp_path)
    assert result.duration_seconds == 4
    assert len(calls) == 1
    assert not list(tmp_path.glob('instagram_info_*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['HTTP Error 429', 'Restricted Video'])
async def test_no_fallback_for_explicit_restrictions(tmp_path, monkeypatch, error):
    async def extract(url):
        pytest.fail('Must not retry explicit restriction')
    monkeypatch.setattr('app.pipeline.instagram_public.extract_public_video', extract)
    async def run(*args, **kwargs):
        raise URLDownloadError(error)
    svc=DownloaderService()
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    with pytest.raises(StructuredDownloadError):
        await svc.download('https://www.instagram.com/reel/abc/', tmp_path)
