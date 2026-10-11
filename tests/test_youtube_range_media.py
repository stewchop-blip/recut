import asyncio
import re
import subprocess

from aiohttp import web
import pytest

from app.services.youtube import highlights as h
from app.services.youtube.range_media import RangeBridge, RangeTrack
from unittest.mock import AsyncMock


@pytest.mark.asyncio
@pytest.mark.parametrize('separate', [False, True])
async def test_real_indexed_mp4_seeking(tmp_path, monkeypatch, separate):
    source_path = tmp_path/'source.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=15',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','120',
        '-c:v','libx264','-threads','2','-preset','ultrafast','-g','30',
        '-c:a','aac','-movflags','+faststart',str(source_path)],check=True,timeout=60)
    files={'video':source_path.read_bytes()}
    if separate:
        for name,flag in [('video','-an'),('audio','-vn')]:
            output=tmp_path/f'{name}.mp4'
            subprocess.run(['ffmpeg','-v','error','-i',str(source_path),flag,
                '-c','copy','-movflags','+faststart',str(output)],check=True,timeout=30)
            files[name]=output.read_bytes()
    received=[]
    async def serve(request):
        name=request.match_info['name']; data=files[name]
        match=re.fullmatch(r'bytes=(\d+)-(\d+)',request.headers['Range'])
        a,b=map(int,match.groups())
        received.append((name,a,b+1))
        return web.Response(status=206,body=data[a:b+1],headers={
            'Content-Range':f'bytes {a}-{b}/{len(data)}'})
    app=web.Application();app.router.add_get('/{name}',serve)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    base=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'
    monkeypatch.setattr(h,'allowed_url',lambda url,**kwargs:url.startswith(base))
    monkeypatch.setattr(h,'PublicResolver',h.ThreadedResolver)
    # Short fixtures use smaller windows; production windows are 256 KiB.
    monkeypatch.setattr(RangeBridge,'chunk_size',64*1024)
    try:
        async with h.YouTubeSource() as source:
            source.duration=120
            source.range_tracks=[RangeTrack(base+name,len(data)) for name,data in files.items()]
            output=await asyncio.wait_for(source.download_clip(71,81,tmp_path/'result'),45)
            meta=await h.get_probe_service().probe(output)
            assert abs(meta.duration_seconds-10)<.2 and meta.has_audio
            assert source.media_bytes==sum(b-a for _,a,b in received)
            total=sum(map(len,files.values()))
            assert source.media_bytes<total*.3
            assert any(a>len(files[name])*.4 for name,a,b in received)
            print(f'LOCAL_RANGE separate={separate} bytes={source.media_bytes} total={total} duration={meta.duration_seconds}')
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,header',[(200,''),(206,'bytes 0-1/100'),(206,'bytes 0-0/*')])
async def test_range_rejects_full_or_wrong_response(monkeypatch,status,header):
    async def serve(request):
        return web.Response(status=status,body=b'x',headers={'Content-Range':header})
    app=web.Application();app.router.add_get('/',serve)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/'
    monkeypatch.setattr(h,'allowed_url',lambda *args,**kw:True)
    monkeypatch.setattr(h,'PublicResolver',h.ThreadedResolver)
    try:
        async with h.YouTubeSource() as source:
            with pytest.raises(h.HighlightsError):
                await source.fetch_range(url,0,0)
            assert source.media_bytes==0
    finally:
        await runner.cleanup()


def test_range_cannot_cover_entire_source():
    track=RangeTrack('unused',1000)
    track.admit(0,100);track.admit(70,200)
    assert track.intervals==[(0,200)]
    with pytest.raises(h.HighlightsError):
        track.admit(200,801)
    assert track.intervals==[(0,200)]


@pytest.mark.asyncio
async def test_selects_original_audio_and_captions(monkeypatch):
    import json
    base='https://test.googlevideo.com/'
    info={'id':'WTizYlHNJhQ','duration':1220,'language':'ru','formats':[
        {'url':base+'dubbed','ext':'mp4','protocol':'https','height':360,
         'vcodec':'avc1','acodec':'mp4a','language_preference':-1},
        {'url':base+'video','ext':'mp4','protocol':'https','height':480,
         'vcodec':'avc1','acodec':'none'},
        {'url':base+'english','protocol':'https','vcodec':'none',
         'acodec':'mp4a','abr':128,'language_preference':-1},
        {'url':base+'russian','protocol':'https','vcodec':'none',
         'acodec':'mp4a','abr':128,'language_preference':10}],
        'automatic_captions':{
            'en-orig':[{'ext':'json3','url':'https://www.youtube.com/api/timedtext?lang=en'}],
            'ru-orig':[{'ext':'json3','url':'https://www.youtube.com/api/timedtext?lang=ru'}]}}
    monkeypatch.setattr(h,'metadata',AsyncMock(return_value=info))
    source=h.YouTubeSource()
    source.fetch_range=AsyncMock(return_value=(b'\x00\x00\x00\x20ftyp'+b'x'*24,1000000))
    source.fetch=AsyncMock(return_value=json.dumps({'events':[
        {'tStartMs':0,'dDurationMs':10000,'segs':[{'utf8':'Русская речь'}]}]}).encode())
    transcript=await source.prepare('https://youtu.be/WTizYlHNJhQ')
    assert [t.url for t in source.range_tracks]==[base+'video',base+'russian']
    assert transcript.language=='ru-orig'
    assert source.fetch.call_args.args[0].endswith('lang=ru')
