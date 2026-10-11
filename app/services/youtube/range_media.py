"""Local, bounded HTTP range bridge for FFmpeg's indexed MP4 seeking.

Remote servers must honor exact byte ranges. FFmpeg sees only opaque loopback
URLs; every upstream read goes through the source byte budget. No full-resource
response or complete source coverage is accepted.
"""
import re
import secrets
import socket
from dataclasses import dataclass, field

from aiohttp import web


@dataclass
class RangeTrack:
    url: str
    total: int
    intervals: list[tuple[int, int]] = field(default_factory=list)

    def admit(self, start, end):
        intervals = sorted(self.intervals + [(start, end)])
        merged = []
        for a, b in intervals:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        if sum(b-a for a,b in merged) > self.total * .6:
            from .highlights import HighlightsError
            raise HighlightsError('PARTIAL_UNSUPPORTED')
        self.intervals = merged


class RangeBridge:
    chunk_size = 64 * 1024

    def __init__(self, source, tracks):
        self.source = source
        self.tracks = tracks
        self.token = secrets.token_urlsafe(24)
        self.requests = 0
        self.error = None

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get('/{token}/{index}', self.handle)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=2)
        await self.runner.setup()
        try:
            site = web.TCPSite(self.runner, '127.0.0.1', 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
        except BaseException:
            await self.runner.cleanup()
            raise
        return self

    async def __aexit__(self, *args):
        await self.runner.cleanup()

    def url(self, index):
        return f'http://127.0.0.1:{self.port}/{self.token}/{index}'

    async def handle(self, request):
        if request.match_info['token'] != self.token:
            raise web.HTTPNotFound()
        try:
            index = int(request.match_info['index'])
            if index < 0:
                raise ValueError
            track = self.tracks[index]
        except (ValueError, IndexError):
            raise web.HTTPNotFound()
        if request.method == 'HEAD':
            return web.Response(headers={'Content-Length': str(track.total), 'Accept-Ranges':'bytes'})
        match = re.fullmatch(r'bytes=(\d+)-(\d*)', request.headers.get('Range', ''))
        if match is None:
            raise web.HTTPRequestRangeNotSatisfiable()
        start = int(match[1])
        end = min(int(match[2])+1 if match[2] else track.total, track.total)
        if not 0 <= start < end <= track.total:
            raise web.HTTPRequestRangeNotSatisfiable()
        response = web.StreamResponse(status=206, headers={
            'Content-Type':'video/mp4', 'Accept-Ranges':'bytes',
            'Content-Length':str(end-start),
            'Content-Range':f'bytes {start}-{end-1}/{track.total}'})
        await response.prepare(request)
        request.transport.set_write_buffer_limits(high=16384, low=4096)
        request.transport.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,16384)
        try:
            while start < end and request.transport and not request.transport.is_closing():
                stop = min(start+self.chunk_size,end)
                self.requests += 1
                if self.requests > 512:
                    from .highlights import HighlightsError
                    raise HighlightsError('TRANSFER_LIMIT')
                track.admit(start,stop)
                body,total = await self.source.fetch_range(track.url,start,stop-1)
                if total != track.total:
                    from .highlights import HighlightsError
                    raise HighlightsError('INVALID_MEDIA')
                for offset in range(0,len(body),16384):
                    await response.write(body[offset:offset+16384])
                start = stop
        except ConnectionError:
            # FFmpeg closes the old request when seeking or reaching clip end.
            pass
        except Exception as exc:
            self.error = exc
            response.force_close()
            if request.transport:
                request.transport.close()
        return response


async def render_ranges(source, start, end, directory):
    from .highlights import HighlightsError
    from app.services.media.ffmpeg_runner import FFmpegRunner
    from app.services.media.probe import get_probe_service
    directory.mkdir(parents=True, exist_ok=True)
    output = directory/'clip.mp4'
    async with RangeBridge(source, source.range_tracks) as bridge:
        args = ['-protocol_whitelist', 'http,tcp', '-seekable', '1',
                '-ss', f'{start:.3f}', '-i', bridge.url(0)]
        if len(source.range_tracks) == 2:
            args += ['-protocol_whitelist','http,tcp', '-seekable','1',
                     '-ss', f'{start:.3f}', '-i', bridge.url(1)]
        args += ['-t',f'{end-start:.3f}', '-map','0:v:0',
                 '-map', '1:a:0' if len(source.range_tracks)==2 else '0:a:0',
                 '-c:v','libx264','-threads','2','-preset','veryfast',
                 '-crf','20','-pix_fmt','yuv420p','-c:a','aac',
                 '-b:a','128k','-movflags','+faststart',str(output)]
        result = await FFmpegRunner().run(args, output_path=output, timeout_seconds=150)
        if bridge.error:
            raise bridge.error
        if not result.success:
            raise HighlightsError('CLIP_FAILED')
    if bridge.error:
        raise bridge.error
    probe = await get_probe_service().probe(output)
    if abs(probe.duration_seconds-(end-start))>1 or not probe.has_audio or not probe.width:
        raise HighlightsError('CLIP_FAILED')
    return output
