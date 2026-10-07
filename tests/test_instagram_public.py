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


@pytest.mark.asyncio
async def test_homepage_follows_same_origin_redirect():
    from types import SimpleNamespace
    from app.pipeline.instagram_public import public_homepage
    calls = []
    class Session:
        async def get(self, url):
            calls.append(url)
            if len(calls) == 1:
                return SimpleNamespace(status_code=302, headers={'Location': '/accounts/login/'})
            return SimpleNamespace(status_code=200, text='public page')
    assert (await public_homepage(Session())).text == 'public page'
    assert calls == ['https://www.instagram.com/', 'https://www.instagram.com/accounts/login/']


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['https://evil.test/', 'http://www.instagram.com/',
                                      'https://127.0.0.1/', 'https://www.instagram.com:8443/'])
async def test_homepage_rejects_external_redirect(location):
    from types import SimpleNamespace
    from app.pipeline.instagram_public import public_homepage, PublicMetadataError
    class Session:
        async def get(self, url):
            assert url == 'https://www.instagram.com/'
            return SimpleNamespace(status_code=302, headers={'Location':location})
    with pytest.raises(PublicMetadataError, match='homepage_redirect_disallowed'):
        await public_homepage(Session())


@pytest.mark.asyncio
async def test_homepage_redirect_loop_is_bounded():
    from types import SimpleNamespace
    from app.pipeline.instagram_public import public_homepage, PublicMetadataError
    calls=[]
    class Session:
        async def get(self,url):
            calls.append(url)
            return SimpleNamespace(status_code=302, headers={'Location':'/'})
    with pytest.raises(PublicMetadataError, match='homepage_redirect_limit'):
        await public_homepage(Session())
    assert len(calls) == 3


def test_shortcode_response_is_supported():
    info=video_info({'data':{'xdt_shortcode_media':{
        'shortcode':'abc','owner':{'is_private':False},
        'video_url':'https://video.cdninstagram.com/a.mp4','video_duration':5}}}, 'abc')
    assert info['duration'] == 5


@pytest.mark.parametrize('response,reason', [
    ({'data':None,'errors':[{'code':123,'message':'PRIVATE_SECRET'}]}, 'graphql_errors_123'),
    ({'data':None}, 'graphql_data_missing'),
    ({'data':{'unexpected':'PRIVATE_SECRET'}}, 'metadata_schema_unknown'),
    ({'data':{'xdt_api__v1__media__shortcode__web_info':None}}, 'metadata_items_missing')])
def test_safe_error_diagnostics(response,reason):
    from app.pipeline.instagram_public import PublicMetadataError
    with pytest.raises(PublicMetadataError) as exc:
        video_info(response,'abc')
    assert str(exc.value) == reason


@pytest.mark.asyncio
async def test_anonymous_session_tokens_are_forwarded(monkeypatch):
    from types import SimpleNamespace
    from app.pipeline.instagram_public import extract_public_video
    class Session:
        cookies=SimpleNamespace(jar=[SimpleNamespace(name='csrftoken',domain='.instagram.com',value='test-csrf')])
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url):
            return SimpleNamespace(status_code=200,text='["LSD",[],{"token":"test-lsd"}] ["DTSGInitData",[],{"token":"test-dtsg"}]')
        async def post(self,url,headers,data):
            assert headers['X-CSRFToken'] == 'test-csrf'
            assert headers['X-FB-LSD'] == data['lsd'] == 'test-lsd'
            assert data['fb_dtsg'] == 'test-dtsg'
            return SimpleNamespace(status_code=200,json=lambda:payload())
    monkeypatch.setattr('app.pipeline.instagram_public.AsyncSession',lambda **kw:Session())
    assert (await extract_public_video('https://www.instagram.com/reel/abc/'))['id'] == 'abc'
