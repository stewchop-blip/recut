import asyncio
import json
from types import SimpleNamespace

import pytest

from app.pipeline import instagram_public as public
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
        async def get(self, url, **kwargs):
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
        async def get(self, url, **kwargs):
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
        async def get(self,url, **kwargs):
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
        async def get(self,url, **kwargs):
            return SimpleNamespace(status_code=200,text='["LSD",[],{"token":"test-lsd"}] ["DTSGInitData",[],{"token":"test-dtsg"}]')
        async def post(self,url,headers,data, **kwargs):
            assert headers['X-CSRFToken'] == 'test-csrf'
            assert headers['X-FB-LSD'] == data['lsd'] == 'test-lsd'
            assert data['fb_dtsg'] == 'test-dtsg'
            return SimpleNamespace(status_code=200,json=lambda:payload())
    monkeypatch.setattr('app.pipeline.instagram_public.AsyncSession',lambda **kw:Session())
    assert (await extract_public_video('https://www.instagram.com/reel/abc/'))['id'] == 'abc'


def response(data=None, status=200, text=''):
    return SimpleNamespace(status_code=status, text=text, headers={}, json=lambda: data)


def embed_html(context):
    return '<script>["init",[],[' + json.dumps({'contextJSON': json.dumps(context)}) + ']],</script>'


class PublicSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.cookies = SimpleNamespace(jar=[])

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def get(self, url, **kwargs):
        self.calls.append(url)
        assert 0 < kwargs['timeout'] <= 10
        assert 'Cookie' not in kwargs.get('headers', {})
        assert 'Authorization' not in kwargs.get('headers', {})
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    async def post(self, url, **kwargs):
        return await self.get(url, **kwargs)


@pytest.mark.parametrize('media_id', ['123', '123_456', 123])
async def test_oembed_media_id(media_id):
    session = PublicSession([response({'media_id': media_id, 'html': 'unused'})])
    assert await public.public_oembed(session, 'abc') == str(media_id)
    assert session.calls == ['https://i.instagram.com/api/v1/oembed/']


@pytest.mark.parametrize('data,reason', [({}, 'oembed_no_media_id'),
    ({'media_id': '../secret'}, 'oembed_no_media_id'),
    ({'media_id': True}, 'oembed_no_media_id'), ([], 'oembed_invalid_payload')])
async def test_oembed_bad_payload(data, reason):
    with pytest.raises(public.PublicMetadataError, match=reason):
        await public.public_oembed(PublicSession([response(data)]), 'abc')


@pytest.mark.parametrize('stage,status', [('oembed', 403), ('mobile_info', 401), ('embed', 403)])
async def test_stage_http_reason(stage, status):
    session = PublicSession([response(status=status)])
    with pytest.raises(public.PublicMetadataError, match=f'{stage}_http_{status}'):
        if stage == 'oembed': await public.public_oembed(session, 'abc')
        elif stage == 'mobile_info': await public.mobile_info(session, '123', 'abc')
        else: await public.public_embed(session, 'abc')


@pytest.mark.parametrize('data', [{}, {'items': []}, {'items': [None]}, {'items': {}}])
async def test_mobile_empty(data):
    with pytest.raises(public.PublicMetadataError, match='mobile_info_empty'):
        await public.mobile_info(PublicSession([response(data)]), '123', 'abc')


async def test_mobile_missing_code_requires_matching_pk():
    item = payload()['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]
    item.pop('code')
    item['pk'] = '123'
    assert (await public.mobile_info(PublicSession([response({'items': [item]})]), '123_456', 'abc'))['id'] == 'abc'
    item['pk'] = '456'
    with pytest.raises(public.PublicMetadataError, match='mobile_info_identity'):
        await public.mobile_info(PublicSession([response({'items': [item]})]), '123', 'abc')


@pytest.mark.parametrize('context', [
    {'gql_data': {'shortcode_media': {'shortcode': 'abc', 'video_url': 'https://video.fbcdn.net/a.mp4'}}},
    payload()['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]])
def test_embed_context_schemas(context):
    assert public.embed_info(embed_html(context), 'abc')['id'] == 'abc'


@pytest.mark.parametrize('html,reason', [('<html>login</html>', 'embed_parse_failed'),
    ('"init",[],[{broken', 'embed_parse_failed'),
    (embed_html({'gql_data': {}}), 'embed_no_video')])
def test_embed_bad_payload(html, reason):
    with pytest.raises(public.PublicMetadataError, match=reason):
        public.embed_info(html, 'abc')


def test_best_reasonable_quality_and_cdn_validation():
    data = payload()
    versions = data['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]['video_versions']
    versions.extend([{'url': 'https://video.fbcdn.net/hd.mp4', 'width': 1080, 'height': 1920},
                     {'url': 'https://video.fbcdn.net/huge.mp4', 'width': 9000, 'height': 9000},
                     {'url': 'https://video.fbcdn.net:8443/evil.mp4'}, None])
    info = video_info(data, 'abc')
    assert len(info['formats']) == 2
    assert info['formats'][-1]['height'] == 1920


@pytest.mark.parametrize('url', ['https://video.fbcdn.net:8443/x',
    'https://secret@video.fbcdn.net/x', 'https://video.fbcdn.net:bad/x',
    'https://video.fbcdn.net.evil.test/x', 'http://video.fbcdn.net/x', None])
def test_disallowed_media_url(url):
    assert not public.allowed_media_url(url)


@pytest.mark.parametrize('path', ['post_html', 'embed', 'graphql_278', 'graphql_271', 'mobile', 'exhausted'])
@pytest.mark.parametrize('proxy', [None, 'http://test-user:test-pass@proxy.example:8000'])
async def test_pipeline_order(monkeypatch, path, proxy):
    if proxy:
        monkeypatch.setenv('INSTAGRAM_PROXY_URL', proxy)
    else:
        monkeypatch.delenv('INSTAGRAM_PROXY_URL', raising=False)
    item = payload()['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]
    responses = [response(text=post_html(item)) if path == 'post_html'
                 else redirect('/accounts/login/?SECRET')]
    expected = ['https://www.instagram.com/p/abc/']
    if path != 'post_html':
        responses.append(response(text=embed_html(item)) if path == 'embed'
                         else redirect('/accounts/login/?SECRET'))
        expected.append('https://www.instagram.com/p/abc/embed/captioned/')
    if path not in {'post_html', 'embed'}:
        for name, strategy in zip(('graphql_278', 'graphql_271'), public.GRAPHQL_STRATEGIES):
            responses.extend([response(text='["LSD",[],{"token":"test"}]'),
                response(payload() if path == name else
                         {'errors': [{'code': 1675004, 'message': 'SECRET'}]})])
            expected.extend(['https://www.instagram.com/', strategy.endpoint])
            if path == name:
                break
    if path in {'mobile', 'exhausted'}:
        responses.append(response({'items': [item]}) if path == 'mobile' else response(status=401))
        expected.append('https://i.instagram.com/api/v1/media/108252/info/')
    session = PublicSession(responses)
    def factory(**kwargs):
        assert kwargs['allow_redirects'] is False
        assert kwargs.get('proxy') == proxy
        return session
    monkeypatch.setattr(public, 'AsyncSession', factory)
    if path == 'exhausted':
        with pytest.raises(public.PublicMetadataError, match='^public_fallback_exhausted$'):
            await public.extract_public_video('https://www.instagram.com/reel/abc/')
    else:
        assert (await public.extract_public_video('https://www.instagram.com/reel/abc/'))['id'] == 'abc'
    assert session.calls == expected


@pytest.mark.parametrize('error,reason', [(RuntimeError('SECRET signed-url'), 'embed_transport_error'),
                                        (asyncio.TimeoutError(), 'embed_timeout')])
async def test_transport_diagnostics_safe(error, reason, monkeypatch):
    events = []
    monkeypatch.setattr(public, 'logger', SimpleNamespace(info=lambda *a, **kw: events.append(kw),
        warning=lambda *a, **kw: events.append(kw)))
    async def operation(): raise error
    with pytest.raises(public.PublicMetadataError, match=reason):
        await public.public_stage('embed', operation)
    assert 'SECRET' not in str(events)


async def test_stage_cancellation_propagates():
    async def operation(): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await public.public_stage('oembed', operation)


async def test_wall_clock_timeout_is_enforced(monkeypatch):
    async def operation():
        await asyncio.Event().wait()
    original = asyncio.wait_for
    async def short_wait(awaitable, timeout):
        assert timeout == 12
        return await original(awaitable, timeout=0.001)
    monkeypatch.setattr(public.asyncio, 'wait_for', short_wait)
    with pytest.raises(public.PublicMetadataError, match='oembed_timeout'):
        await public.public_stage('oembed', operation)


async def test_non_json_oembed_is_safe():
    class Session(PublicSession):
        async def get(self, *args, **kwargs):
            def invalid(): raise ValueError('SECRET response body')
            return SimpleNamespace(status_code=200, json=invalid)
    with pytest.raises(public.PublicMetadataError, match='^oembed_not_json$'):
        await public.public_oembed(Session([]), 'abc')


@pytest.mark.parametrize('code,pk', [('B1LbfVPlwIA', '2110901750722920960'),
    ('B-fKL9qpeab', '2278584739065882267'),
    ('CCQQsCXjOaBfS3I2PppqsNkxElV', '2346448800803776129')])
def test_shortcode_media_id_known_examples(code, pk):
    assert public.shortcode_media_id(code) == pk


@pytest.mark.parametrize('code', ['', 'AAAA', '../bad', 'abc?secret'])
def test_invalid_shortcode_media_id(code):
    with pytest.raises(public.PublicMetadataError, match='shortcode_id_invalid'):
        public.shortcode_media_id(code)


def redirect(location):
    return SimpleNamespace(status_code=302, headers={'Location': location})


async def test_embed_follows_bounded_canonical_redirect():
    session = PublicSession([redirect('/reel/abc/embed/captioned/?secret=unused'),
        response(text=embed_html({'gql_data': payload()['data']}))])
    assert (await public.public_embed(session, 'abc'))['id'] == 'abc'
    assert session.calls == ['https://www.instagram.com/p/abc/embed/captioned/',
                            'https://www.instagram.com/reel/abc/embed/captioned/?secret=unused']


@pytest.mark.parametrize('location,reason', [
    ('https://evil.test/path?SECRET', 'embed_redirect_disallowed'),
    ('http://www.instagram.com/p/abc/embed/', 'embed_redirect_disallowed'),
    ('https://www.instagram.com:8443/p/abc/embed/', 'embed_redirect_disallowed'),
    ('https://www.instagram.com:bad/p/abc/embed/', 'embed_redirect_disallowed'),
    ('https://secret@www.instagram.com/p/abc/embed/', 'embed_redirect_disallowed'),
    ('', 'embed_redirect_disallowed'),
    ('/p/another/embed/', 'embed_redirect_path_disallowed'),
    ('/unrelated/?SECRET', 'embed_redirect_path_disallowed'),
    ('/accounts/login/?next=SECRET', 'embed_redirect_login'),
    ('/challenge/SECRET', 'embed_redirect_challenge')])
async def test_embed_redirect_diagnostics_safe(location, reason):
    session = PublicSession([redirect(location)])
    with pytest.raises(public.PublicMetadataError, match='^' + reason + '$'):
        await public.public_embed(session, 'abc')
    assert len(session.calls) == 1


async def test_embed_redirect_loop_is_bounded():
    session = PublicSession([redirect('/p/abc/embed/')] * 3)
    with pytest.raises(public.PublicMetadataError, match='embed_redirect_limit'):
        await public.public_embed(session, 'abc')
    assert len(session.calls) == 3


async def test_railway_failure_summary_is_complete_and_safe(monkeypatch):
    events = []
    monkeypatch.setattr(public, 'logger', SimpleNamespace(
        info=lambda *a, **kw: events.append((a, kw)),
        warning=lambda *a, **kw: events.append((a, kw))))
    session = PublicSession([redirect('/accounts/login/?SECRET'),
        redirect('/accounts/login/?SECRET'),
        response(text='["LSD",[],{"token":"test"}]'),
        response({'errors': [{'code': 1675004, 'message': 'SECRET'}]}),
        response(text='["LSD",[],{"token":"test"}]'),
        response({'data': None}), response(status=401)])
    monkeypatch.setattr(public, 'AsyncSession', lambda **kwargs: session)
    with pytest.raises(public.PublicMetadataError, match='^public_fallback_exhausted$'):
        await public.extract_public_video('https://www.instagram.com/reel/abc/')
    assert events[-1] == (('instagram_public_fallback_exhausted',), {'attempts': {
        'post_html': 'post_html_redirect_login', 'embed': 'embed_redirect_login',
        'graphql_media_info_278': 'graphql_media_info_278_errors_1675004',
        'graphql_post_root_271': 'graphql_post_root_271_data_missing',
        'mobile_info': 'mobile_info_http_401'}})
    assert 'SECRET' not in str(events)
    assert len(session.calls) == 7
    assert not any('/accounts/login' in url or '/oembed/' in url for url in session.calls)



def post_html(item):
    return '<script>{"xig_polaris_media":' + json.dumps(item) + '}</script>'


@pytest.mark.parametrize('wrapped', [True, False])
def test_post_html_public_metadata(wrapped):
    item = payload()['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]
    item['caption'] = {'text': 'A caption with } braces and "quotes"'}
    data = {'if_not_gated_logged_out': item} if wrapped else item
    info = public.post_html_info(post_html(data), 'abc')
    assert info['id'] == 'abc'
    assert info['duration'] == 4
    assert info['title'] == item['caption']['text']


@pytest.mark.parametrize('html,reason', [
    ('<html>login</html>', 'post_html_media_missing'),
    ('"xig_polaris_media": {bad', 'post_html_parse_failed'),
    (post_html([]), 'post_html_invalid_media'),
    (post_html({'if_not_gated_logged_out': None}), 'post_html_gated'),
    (post_html({'code': 'abc', 'image_versions2': {'url': 'SECRET'}}), 'post_html_no_video')])
def test_post_html_rejects_unavailable_media(html, reason):
    with pytest.raises(public.PublicMetadataError, match='^' + reason + '$'):
        public.post_html_info(html, 'abc')


@pytest.mark.parametrize('changes', [{'code': 'another'}, {'private': True},
    {'url': 'https://evil.test/secret'}, {'url': 'https://video.fbcdn.net:8443/secret'}])
def test_post_html_preserves_identity_privacy_and_cdn_validation(changes):
    item = payload(**changes)['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]
    with pytest.raises(public.PublicMetadataError):
        public.post_html_info(post_html({'if_not_gated_logged_out': item}), 'abc')


@pytest.mark.parametrize('status,location,reason', [(403, '', 'post_html_http_403'),
    (302, '/accounts/login/?SECRET', 'post_html_redirect_login'),
    (302, 'https://evil.test/?SECRET', 'post_html_http_302'),
    (302, 'https://www.instagram.com:bad/', 'post_html_http_302')])
async def test_post_html_http_diagnostics(status, location, reason):
    session = PublicSession([SimpleNamespace(status_code=status, headers={'Location': location})])
    with pytest.raises(public.PublicMetadataError, match='^' + reason + '$'):
        await public.public_post_html(session, 'abc')
    assert len(session.calls) == 1


@pytest.mark.parametrize('attribute,seconds', [('PT98.520813S', 98.520813),
    ('PT1H2M3.5S', 3723.5), ('PT2M', 120), ('PT', None), ('PT0S', None),
    ('PTnanS', None), ('PT-1S', None)])
def test_dash_duration(attribute, seconds):
    assert public.dash_duration('<MPD mediaPresentationDuration="' + attribute + '"/>') == seconds


async def test_post_html_dash_duration_enforces_download_limit(tmp_path, monkeypatch):
    item = payload()['data']['xdt_api__v1__media__shortcode__web_info']['items'][0]
    item.pop('video_duration')
    item['video_dash_manifest'] = '<MPD mediaPresentationDuration="PT98.520813S"/>'
    info = public.post_html_info(post_html({'if_not_gated_logged_out': item}), 'abc')
    assert info['duration'] == 98.520813
    svc = DownloaderService()
    async def extract(url): return info
    async def run(args, **kwargs):
        assert '--dump-json' in args, 'Oversized-duration media must not be transferred'
        raise URLDownloadError('Login required')
    monkeypatch.setattr(public, 'extract_public_video', extract)
    monkeypatch.setattr(svc, '_run_ytdlp', run)
    from app.pipeline.url_downloader import VideoTooLongError
    with pytest.raises(VideoTooLongError):
        await svc.download('https://www.instagram.com/reel/abc/', tmp_path, max_duration_seconds=90)


@pytest.mark.parametrize('strategy', public.GRAPHQL_STRATEGIES)
async def test_graphql_request_shape(strategy):
    class Session(PublicSession):
        async def post(self, url, headers, data, **kwargs):
            assert url == strategy.endpoint
            assert data['doc_id'] == strategy.doc_id
            expected = {'shortcode': 'abc'}
            if strategy.name == 'graphql_media_info_278':
                expected.update({
                    '__relay_internal__pv__PolarisShortDramaEnabledrelayprovider': False,
                    '__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider': True})
            else:
                expected['__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider'] = False
            assert json.loads(data['variables']) == expected
            assert headers['X-FB-Friendly-Name'] == 'PolarisPostRootQuery'
            return await super().post(url, headers=headers, data=data, **kwargs)
    session = Session([response(text='["LSD",[],{"token":"test"}]'), response(payload())])
    assert (await public.public_graphql(session, 'abc', strategy))['id'] == 'abc'


@pytest.mark.parametrize('data,reason', [
    ({'data': None}, 'data_missing'),
    ({'data': None, 'errors': [{'message': 'SECRET'}]}, 'errors'),
    ({'errors': [{'code': 1675004, 'message': 'SECRET'}]}, 'errors_1675004'),
    (payload(code='another'), 'metadata_identity_or_privacy'),
    (payload(url='https://video.fbcdn.net.evil.test/SECRET'), 'metadata_no_video')])
async def test_second_graphql_failure_is_named_and_safe(data, reason):
    strategy = public.GRAPHQL_STRATEGIES[1]
    session = PublicSession([response(text='["LSD",[],{"token":"test"}]'), response(data)])
    with pytest.raises(public.PublicMetadataError, match='^graphql_post_root_271_' + reason + '$'):
        await public.public_stage(strategy.name, lambda: public.public_graphql(session, 'abc', strategy))
    assert session.calls.count(strategy.endpoint) == 1


async def test_arbitrary_public_exception_is_not_logged(monkeypatch):
    events = []
    monkeypatch.setattr(public, 'logger', SimpleNamespace(info=lambda *a, **kw: None,
        warning=lambda *a, **kw: events.append(kw)))
    async def operation():
        raise public.PublicMetadataError('SECRET signed-url token body')
    with pytest.raises(public.PublicMetadataError, match='^unexpected_error$'):
        await public.public_stage('embed', operation)
    assert 'SECRET' not in str(events)


async def test_second_graphql_cancellation_stops_pipeline(monkeypatch):
    calls = []
    async def stage(name, operation):
        calls.append(name)
        if name == 'graphql_post_root_271':
            raise asyncio.CancelledError()
        raise public.PublicMetadataError('metadata_no_video')
    monkeypatch.setattr(public, 'public_stage', stage)
    monkeypatch.setattr(public, 'AsyncSession', lambda **kwargs: PublicSession([]))
    with pytest.raises(asyncio.CancelledError):
        await public.extract_public_video('https://www.instagram.com/reel/abc/')
    assert calls == ['post_html', 'embed', 'graphql_media_info_278', 'graphql_post_root_271']


async def test_all_stages_have_bounded_wall_clock(monkeypatch):
    budgets = []
    original = asyncio.wait_for
    async def bounded(awaitable, timeout):
        budgets.append(timeout)
        return await original(awaitable, timeout=0.001)
    class HangingSession(PublicSession):
        async def get(self, *args, **kwargs):
            await asyncio.Event().wait()
    monkeypatch.setattr(public.asyncio, 'wait_for', bounded)
    monkeypatch.setattr(public, 'AsyncSession', lambda **kwargs: HangingSession([]))
    with pytest.raises(public.PublicMetadataError, match='public_fallback_exhausted'):
        await public.extract_public_video('https://www.instagram.com/reel/abc/')
    assert budgets == [12] * 5
    assert sum(budgets) < 90
