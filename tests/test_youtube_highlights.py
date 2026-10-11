"""Real local HLS transfer + FFmpeg, and mocked YouTube/Telegram integration.

Local network tests prove segment accounting only, not YouTube CDN access.
"""
import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

from app.services.youtube import highlights as h
from app.services.analysis.base import ClipSelectionRequest, TranscriptSegment, ClipCandidate, ClipSelection, ClipSelectorError
from app.services.analysis.openrouter import OpenRouterClipSelector, MAX_TRANSCRIPT_CHARS


URL='https://www.youtube.com/watch?v=aqz-KE-bpKQ'
MEDIA='https://test.googlevideo.com/'


def manifest(duration=600):
    return '#EXTM3U\n'+''.join(f'#EXTINF:10,\n{i}.ts\n' for i in range(duration//10))+'#EXT-X-ENDLIST\n'


@pytest.mark.parametrize('url', ['http://youtube.com/watch?v=aqz-KE-bpKQ',
    'https://youtube.com.evil.test/watch?v=aqz-KE-bpKQ','https://user@youtube.com/watch?v=aqz-KE-bpKQ',
    'https://youtube.com:444/watch?v=aqz-KE-bpKQ', 'https://youtube.com/playlist?list=xxx',
    'https://127.0.0.1/watch?v=aqz-KE-bpKQ'])
def test_input_validation(url):
    with pytest.raises(h.HighlightsError): h.youtube_url(url)


def test_short_url_canonicalized():
    assert h.youtube_url('https://youtu.be/aqz-KE-bpKQ?t=4')==URL


@pytest.mark.parametrize('url',['http://x.googlevideo.com/a','https://googlevideo.com.evil/a',
 'https://x.googlevideo.com:123/a','https://127.0.0.1/a','https://x@x.googlevideo.com/a',
 'https://www.youtube.com/redirect?x=abc'])
def test_media_ssrf_guards(url):
    assert not h.allowed_url(url,captions=True)


@pytest.mark.asyncio
async def test_dns_blocks_private(monkeypatch):
    monkeypatch.setattr(h.ThreadedResolver,'resolve',AsyncMock(return_value=[{'host':'127.0.0.1'}]))
    with pytest.raises(h.HighlightsError,match='UNSAFE_ADDRESS'):
        await h.PublicResolver().resolve('test.googlevideo.com')


@pytest.mark.parametrize('duration',[600,1800,3600])
def test_segments_select_only_requested_ranges(duration):
    parts=h.parse_playlist(manifest(duration),MEDIA+'manifest.m3u8',duration)
    selected=h.select_parts(parts,duration-65,duration-35,duration)
    assert len(selected)==4
    assert selected[0].start==duration-70
    assert sum(p.end-p.start for p in selected)==40
    assert len(selected)<len(parts)


@pytest.mark.parametrize('tag',['#EXT-X-KEY:METHOD=AES-128,URI="key"',
 '#EXT-X-MAP:URI="full.mp4"','#EXT-X-BYTERANGE:999999@0','#EXT-X-DISCONTINUITY',
 '#EXT-X-STREAM-INF:BANDWIDTH=1000','#EXT-X-GAP'])
def test_unsupported_formats_fail_closed(tag):
    with pytest.raises(h.HighlightsError,match='PARTIAL_UNSUPPORTED'):
        h.parse_playlist(manifest().replace('#EXTM3U','#EXTM3U\n'+tag),MEDIA,600)


@pytest.mark.parametrize('start,end',[(float('nan'),30),(0,float('inf')),(-1,30),(30,20),(590,620),(0,300)])
def test_timestamps_rejected(start,end):
    with pytest.raises(h.HighlightsError):
        h.select_parts(h.parse_playlist(manifest(),MEDIA,600),start,end,600)


@pytest.mark.parametrize('auto,lang',[(False,'ru'),(True,'ru'),(False,'en'),(True,'en')])
@pytest.mark.asyncio
async def test_captions_first_no_video_download(monkeypatch,auto,lang):
    info={'duration':600,'language':lang,'formats':[{'protocol':'m3u8_native','vcodec':'avc1.4',
        'acodec':'mp4a.40','height':480,'url':MEDIA+'manifest.m3u8'}],
        'automatic_captions' if auto else 'subtitles':{lang:[{'ext':'json3','url':'https://www.youtube.com/api/timedtext?v=abc'}]}}
    monkeypatch.setattr(h,'metadata',AsyncMock(return_value=info))
    source=h.YouTubeSource()
    source.fetch=AsyncMock(side_effect=[manifest().encode(),json.dumps({'events':[
        {'tStartMs':1000,'dDurationMs':3000,'segs':[{'utf8':'Привет' if lang=='ru' else 'Hello'}]}]}).encode()])
    result=await source.prepare(URL)
    assert result.language==lang and result.segments[0].start==1
    assert source.fetch.call_count==2
    assert not any(c.kwargs.get('media') for c in source.fetch.call_args_list)


@pytest.mark.asyncio
async def test_no_captions_fails_without_whisper(monkeypatch):
    monkeypatch.setattr(h,'metadata',AsyncMock(return_value={'duration':600,'formats':[
        {'protocol':'m3u8_native','vcodec':'avc1','acodec':'mp4a','height':480,'url':MEDIA+'m.m3u8'}]}))
    source=h.YouTubeSource()
    source.fetch=AsyncMock(return_value=manifest().encode())
    with pytest.raises(h.HighlightsError,match='CAPTIONS_UNAVAILABLE'):
        await source.prepare(URL)
    assert source.fetch.call_count==1


@pytest.mark.asyncio
async def test_http_only_source_never_downloaded(monkeypatch):
    monkeypatch.setattr(h,'metadata',AsyncMock(return_value={'duration':600,'formats':[
        {'protocol':'https','url':MEDIA+'full.mp4','vcodec':'avc1','acodec':'mp4a','height':480}]}))
    source=h.YouTubeSource(); source.fetch=AsyncMock()
    with pytest.raises(h.HighlightsError,match='PARTIAL_UNSUPPORTED'):
        await source.prepare(URL)
    source.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_every_chunk_analysed_and_ranked(monkeypatch):
    segments=tuple(TranscriptSegment(i*10,(i+1)*10,f'part{i} '+('x'*190)) for i in range(180))
    req=ClipSelectionRequest('',segments,1800,target_count=3)
    selector=OpenRouterClipSelector(api_key='x')
    observed=[]; candidates=[]
    async def once(request):
        if request.segments[0].text.startswith('CANDIDATE:'):
            return ClipSelection(tuple(candidates[:3]),'test')
        observed.extend(request.segments)
        c=ClipCandidate(request.segments[0].start,request.segments[0].start+30,'title','hook','reason')
        candidates.append(c)
        return ClipSelection((c,),'test')
    monkeypatch.setattr(selector,'_select_once',once)
    result=await selector.select_clips(req)
    assert observed==list(segments)
    assert len(result.clips)==3


@pytest.mark.asyncio
async def test_zero_clips_never_falls_back_to_full_source():
    selector=OpenRouterClipSelector(api_key='x',max_retries=0)
    response=SimpleNamespace(status_code=200,json=lambda:{'choices':[{'message':{'content':'{"clips":[]}'}}]})
    selector._client=SimpleNamespace(is_closed=False,post=AsyncMock(return_value=response))
    with pytest.raises(ClipSelectorError):
        await selector.select_clips(ClipSelectionRequest('',(TranscriptSegment(0,600,'text'),),600))


@pytest.mark.asyncio
async def test_real_hls_transfer_and_cut(tmp_path,monkeypatch):
    """Real sockets and real encoded TS: measure bytes and decoded output."""
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','120','-c:v','libx264',
        '-threads','2','-preset','ultrafast','-g','20','-sc_threshold','0','-c:a','aac',
        '-f','hls','-hls_time','2','-hls_list_size','0','-hls_segment_filename',str(tmp_path/'p%03d.ts'),
        str(tmp_path/'source.m3u8')],check=True,timeout=60)
    requests=[]; transferred=0
    async def serve(request):
        nonlocal transferred
        name=request.match_info['name']; requests.append(name)
        body=(tmp_path/name).read_bytes(); transferred+=len(body)
        return web.Response(body=body)
    app=web.Application();app.router.add_get('/{name}',serve)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    base=f'http://127.0.0.1:{port}/'
    # Test-only loopback transport. Production DNS/URL policies stay strict.
    monkeypatch.setattr(h,'allowed_url',lambda url,**kw:url.startswith(base))
    monkeypatch.setattr(h,'PublicResolver',h.ThreadedResolver)
    try:
        async with h.YouTubeSource() as source:
            source.duration=120
            source.parts=h.parse_playlist((tmp_path/'source.m3u8').read_text(),base+'source.m3u8',120)
            selected=h.select_parts(source.parts,41,51,120)
            output=await source.download_clip(41,51,tmp_path/'result')
            meta=await h.get_probe_service().probe(output)
            assert abs(meta.duration_seconds-10)<.2 and meta.has_audio
            assert requests==[Path(p.url).name for p in selected]
            assert source.media_bytes==transferred
            total=sum(p.stat().st_size for p in tmp_path.glob('p*.ts'))
            assert transferred<total*.2
            assert not (tmp_path/'result/selected.ts').exists()
            print(f'LOCAL_HLS bytes={transferred} full_source_bytes={total} duration={meta.duration_seconds}')
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_transfer_limit_real_http(tmp_path,monkeypatch):
    async def serve(request):
        response=web.StreamResponse();await response.prepare(request)
        await response.write(b'x'*32768);await response.write_eof();return response
    app=web.Application();app.router.add_get('/',serve)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    base=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'
    monkeypatch.setattr(h,'allowed_url',lambda url,**kw:url==base)
    monkeypatch.setattr(h,'PublicResolver',h.ThreadedResolver)
    try:
        async with h.YouTubeSource(max_bytes=20000) as source:
            with pytest.raises(h.HighlightsError,match='TRANSFER_LIMIT'):
                await source.fetch(base,100000,media=True)
            assert source.received_bytes<=20000+16384
    finally: await runner.cleanup()


@pytest.mark.asyncio
async def test_cancel_reaps_real_ffmpeg(tmp_path):
    from app.services.media.ffmpeg_runner import FFmpegRunner
    runner=FFmpegRunner()
    task=asyncio.create_task(runner.run(['-re','-f','lavfi','-i','testsrc=size=64x64:rate=1',
                                       '-t','60','-f','null','-']))
    for _ in range(100):
        if runner._proc: break
        await asyncio.sleep(.01)
    proc=runner._proc
    assert proc is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_youtube_intake_never_calls_normal_downloader(monkeypatch):
    from app.bot.handlers import video, youtube_highlights as yh
    monkeypatch.setattr(video,'_mode_state',{912:'moments'})
    monkeypatch.setattr(yh,'accept_url',AsyncMock())
    monkeypatch.setattr(video.DownloaderService,'download',AsyncMock(side_effect=AssertionError('full download')))
    message=SimpleNamespace(from_user=SimpleNamespace(id=912),text=URL,answer=AsyncMock())
    await video.on_url_message(message,SimpleNamespace())
    yh.accept_url.assert_awaited_once_with(message,URL)
    video.DownloaderService.download.assert_not_called()


@pytest.mark.asyncio
async def test_failed_job_cleanup_and_status(monkeypatch,tmp_path):
    from app.bot.handlers import youtube_highlights as yh
    repo=SimpleNamespace(has_active_job=AsyncMock(return_value=False),
        create=AsyncMock(return_value=SimpleNamespace(id=7)),set_status=AsyncMock(),
        mark_failed=AsyncMock(),mark_completed=AsyncMock())
    @asynccontextmanager
    async def session(): yield None
    monkeypatch.setattr(yh.db_manager,'session',session)
    monkeypatch.setattr(yh,'JobRepository',lambda _:repo)
    monkeypatch.setattr(yh,'get_temp_manager',lambda:SimpleNamespace(_job_dir=lambda _:tmp_path/'job'))
    monkeypatch.setattr(yh.YouTubeSource,'prepare',AsyncMock(side_effect=h.HighlightsError('CAPTIONS_UNAVAILABLE')))
    yh._pending[11]=yh.Pending(URL,22,33,__import__('time').monotonic())
    call=SimpleNamespace(from_user=SimpleNamespace(id=11),answer=AsyncMock(),bot=None,
        message=SimpleNamespace(message_id=22,chat=SimpleNamespace(id=33),edit_text=AsyncMock()))
    await yh.run(call)
    repo.mark_failed.assert_awaited_once()
    repo.mark_completed.assert_not_called()
    assert not (tmp_path/'job').exists() and 11 not in yh._running


@pytest.mark.asyncio
async def test_stale_button_rejected(monkeypatch):
    from app.bot.handlers import youtube_highlights as yh
    yh._pending[12]=yh.Pending(URL,99,33,__import__('time').monotonic())
    call=SimpleNamespace(from_user=SimpleNamespace(id=12),answer=AsyncMock(),
        message=SimpleNamespace(message_id=22,chat=SimpleNamespace(id=33)))
    await yh.run(call)
    assert yh._pending[12].status_id==99
    call.answer.assert_awaited_once()


@pytest.mark.parametrize('error',[aiohttp.ClientConnectionError(), h.HighlightsError('TRANSFER_LIMIT')])
@pytest.mark.asyncio
async def test_network_failure_removes_partial_file(tmp_path,error):
    source=h.YouTubeSource();source.duration=600
    source.parts=h.parse_playlist(manifest(),MEDIA,600)
    source.fetch=AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await source.download_clip(35,65,tmp_path/'clip')
    assert not (tmp_path/'clip/selected.ts').exists()


@pytest.mark.asyncio
async def test_ffmpeg_failure_is_not_delivered(tmp_path,monkeypatch):
    source=h.YouTubeSource();source.duration=600
    source.parts=h.parse_playlist(manifest(),MEDIA,600)
    packet=bytes([0x47])+b'\0'*187
    source.fetch=AsyncMock(return_value=packet*3)
    monkeypatch.setattr(h.FFmpegRunner,'run',AsyncMock(return_value=SimpleNamespace(success=False)))
    with pytest.raises(h.HighlightsError,match='CLIP_FAILED'):
        await source.download_clip(35,65,tmp_path/'clip')
    assert not (tmp_path/'clip/selected.ts').exists()


@pytest.mark.asyncio
async def test_existing_render_queue_rejects_duplicate_highlights():
    from app.bot.middlewares.render_queue import RenderQueueMiddleware,active_render_users
    queue=RenderQueueMiddleware();handler=AsyncMock()
    active_render_users.add(544)
    event=SimpleNamespace(from_user=SimpleNamespace(id=544),data='action:youtube_highlights',answer=AsyncMock())
    try:
        await queue(handler,event,{})
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
    finally: active_render_users.discard(544)


@pytest.mark.asyncio
async def test_saved_captions_do_not_transcribe_and_follow_speed(tmp_path,monkeypatch):
    from app.pipeline import appearance_stages as ap
    from app.pipeline.transcriber import Transcriber
    from app.services.subtitles.ass import AssSubtitleBuilder
    captured=[]
    real=AssSubtitleBuilder.build
    def build(self,request):
        captured.extend(request.segments)
        return real(self,request)
    monkeypatch.setattr(AssSubtitleBuilder,'build',build)
    monkeypatch.setattr(Transcriber,'transcribe',AsyncMock(side_effect=AssertionError('must not use Whisper')))
    media=SimpleNamespace(extract_audio=AsyncMock(),probe=AsyncMock(return_value={
        'streams':[{'codec_type':'video','width':720,'height':1280}]}),burn_subtitles=AsyncMock())
    await ap.add_subtitles(media,tmp_path/'video.mp4',tmp_path,style='standard',language='en',
                          segments=[h.Segment(start=2,end=4,text='Hello there')],speed=2)
    media.extract_audio.assert_not_called()
    assert captured[0].start==1 and captured[0].end==2
