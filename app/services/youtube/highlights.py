"""Bounded YouTube VOD captions and selective HLS media transfer.

Only unencrypted, muxed MPEG-TS VOD playlists are supported. Select segment
URLs before doing any media I/O; never hand a remote URL to FFmpeg. Byte
counters include every received body chunk, including failed transfers.
"""
import asyncio
import ipaddress
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp
from aiohttp.resolver import ThreadedResolver

from app.core.logging import get_logger
from app.pipeline.transcriber import TranscribedJob
from app.services.transcription.base import Segment
from app.services.media.ffmpeg_runner import FFmpegRunner
from app.services.media.probe import get_probe_service

logger = get_logger(__name__)


class HighlightsError(ValueError):
    """Fixed code only: signed URLs and remote response bodies stay private."""


def youtube_url(value: str) -> str:
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            raise ValueError
        if p.hostname == 'youtu.be':
            video_id = p.path.strip('/')
        elif p.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
            if p.path == '/watch':
                video_id = parse_qs(p.query).get('v', [''])[0]
            elif p.path.startswith(('/shorts/', '/embed/')):
                video_id = p.path.split('/')[2]
            else:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError
        return f'https://www.youtube.com/watch?v={video_id}'
    except (TypeError, ValueError, IndexError):
        raise HighlightsError('INVALID_YOUTUBE_URL') from None


def allowed_url(value: str, *, captions=False) -> bool:
    try:
        p = urlsplit(value)
        host = p.hostname or ''
        return (p.scheme == 'https' and not p.username and not p.password
                and p.port in (None, 443) and not p.fragment
                and (host.endswith('.googlevideo.com') or
                     (captions and host in {'www.youtube.com', 'youtube.com'}
                      and p.path == '/api/timedtext')))
    except (ValueError, TypeError):
        return False


class PublicResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        answers = await super().resolve(host, port, family)
        if not answers or any(not ipaddress.ip_address(a['host']).is_global for a in answers):
            raise HighlightsError('UNSAFE_ADDRESS')
        return answers


@dataclass(frozen=True)
class Part:
    url: str
    start: float
    end: float


def parse_playlist(text: str, url: str, duration: float) -> tuple[Part, ...]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U' or '#EXT-X-ENDLIST' not in lines:
        raise HighlightsError('PARTIAL_UNSUPPORTED')
    # fMP4, encrypted media, byte-range resources, live and discontinuous
    # timelines need separate verified implementations. Fail closed.
    forbidden = ('#EXT-X-KEY:', '#EXT-X-MAP:', '#EXT-X-BYTERANGE:',
                 '#EXT-X-DISCONTINUITY', '#EXT-X-STREAM-INF:', '#EXT-X-GAP',
                 '#EXT-X-I-FRAMES-ONLY')
    parts = []
    clock = 0.0
    length = None
    for line in lines:
        if line.startswith(forbidden):
            raise HighlightsError('PARTIAL_UNSUPPORTED')
        if line.startswith('#EXTINF:'):
            try:
                if length is not None:
                    raise ValueError
                length = float(line.split(':', 1)[1].split(',')[0])
                if not math.isfinite(length) or not 0 < length <= 20:
                    raise ValueError
            except ValueError:
                raise HighlightsError('PARTIAL_UNSUPPORTED') from None
        elif not line.startswith('#'):
            target = urljoin(url, line)
            if length is None or not allowed_url(target):
                raise HighlightsError('PARTIAL_UNSUPPORTED')
            parts.append(Part(target, clock, clock + length))
            clock += length
            length = None
    if length is not None or not parts or len(parts) > 4000 or abs(clock-duration) > 5:
        raise HighlightsError('PARTIAL_UNSUPPORTED')
    return tuple(parts)


def select_parts(parts, start, end, duration):
    if not all(math.isfinite(v) for v in (start, end, duration)) or not 0 <= start < end <= duration:
        raise HighlightsError('INVALID_TIMESTAMPS')
    if not 5 <= end-start <= 62:
        raise HighlightsError('INVALID_TIMESTAMPS')
    selected = tuple(p for p in parts if p.end > start and p.start < end)
    if not selected or selected[0].start > start or selected[-1].end + .1 < end:
        raise HighlightsError('INVALID_TIMESTAMPS')
    if len(selected) >= len(parts) or len(selected) > 64:
        raise HighlightsError('PARTIAL_UNSUPPORTED')
    return selected


def captions_json3(body: bytes, duration: float, language: str) -> TranscribedJob:
    try:
        payload = json.loads(body)
        segments = []
        for item in payload.get('events', []):
            text = ' '.join(''.join(s.get('utf8', '') for s in item.get('segs', [])).split())
            if not text:
                continue
            start = float(item['tStartMs'])/1000
            end = min(duration, start + float(item.get('dDurationMs', 0))/1000)
            if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= duration:
                raise ValueError
            if segments and start < segments[-1].start:
                raise ValueError
            # JSON3 auto captions can repeat the same rolling line.
            if segments and text == segments[-1].text and start <= segments[-1].end:
                previous = segments.pop()
                segments.append(Segment(start=previous.start, end=max(previous.end,end), text=text))
            else:
                segments.append(Segment(start=start, end=end, text=text))
        if not segments or sum(len(s.text) for s in segments) > 80_000 or len(segments) > 12000:
            raise ValueError
        return TranscribedJob(language=language, duration_seconds=duration,
            segments=tuple(segments), raw_text=' '.join(s.text for s in segments), model='youtube-captions')
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HighlightsError('CAPTIONS_UNAVAILABLE') from None


def metadata_error(stderr: bytes) -> str:
    """Classify diagnostics without logging signed URLs, cookies or remote text."""
    text = stderr.decode('utf-8', errors='replace').lower()
    if any(s in text for s in ("confirm you’re not a bot", "confirm you're not a bot", 'sign in to confirm')):
        return 'YOUTUBE_AUTH_REQUIRED'
    if '429' in text or 'too many requests' in text:
        return 'YOUTUBE_RATE_LIMITED'
    if any(s in text for s in ('no supported javascript runtime', 'challenge solving failed', 'n challenge')):
        return 'YOUTUBE_JS_FAILED'
    if any(s in text for s in ('private video', 'video unavailable', 'video has been removed', 'not available in your country')):
        return 'YOUTUBE_VIDEO_UNAVAILABLE'
    if 'requested format is not available' in text:
        return 'PARTIAL_UNSUPPORTED'
    if any(s in text for s in ('timed out', 'unable to download', 'name resolution', 'connection refused', 'certificate verify failed')):
        return 'YOUTUBE_NETWORK_FAILED'
    return 'YOUTUBE_UNAVAILABLE'


async def metadata(url: str) -> dict:
    """Metadata only, bounded pipe readers and process lifetime."""
    canonical = youtube_url(url)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-plugin-dirs',
        '--js-runtimes', 'node', '--no-playlist', '--skip-download', '--socket-timeout', '15',
        '--retries', '1', '--dump-single-json', '--', canonical,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    async def diagnostics():
        tail = bytearray()
        while chunk := await proc.stderr.read(16384):
            tail.extend(chunk)
            del tail[:-65536]
        return bytes(tail)
    stderr_task = asyncio.create_task(diagnostics())
    async def read():
        output = bytearray()
        while chunk := await proc.stdout.read(65536):
            if len(output) + len(chunk) > 8*1024*1024:
                raise HighlightsError('METADATA_TOO_LARGE')
            output.extend(chunk)
        await proc.wait()
        stderr = await stderr_task
        if proc.returncode:
            code = metadata_error(stderr)
            logger.warning('youtube_metadata_failed', error_code=code, exit_code=proc.returncode)
            raise HighlightsError(code)
        try:
            info = json.loads(output)
            if not isinstance(info, dict) or info.get('id') != canonical.rsplit('=',1)[1]:
                raise ValueError
            return info
        except ValueError:
            raise HighlightsError('YOUTUBE_UNAVAILABLE') from None
    try:
        async with asyncio.timeout(60):
            return await read()
    finally:
        if proc.returncode is None:
            import os, signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        await asyncio.gather(stderr_task, return_exceptions=True)


class YouTubeSource:
    def __init__(self, max_bytes=96*1024*1024, max_duration=3600):
        self.max_bytes = max_bytes
        self.max_duration = max_duration
        self.received_bytes = 0
        self.media_bytes = 0
        self.duration = 0.0
        self.parts = ()
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=2),
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            auto_decompress=False, trust_env=False,
            headers={'User-Agent':'Mozilla/5.0', 'Accept-Encoding':'identity'})
        return self

    async def __aexit__(self, *args):
        await self.session.close()
        logger.info('youtube_highlights_transfer', received_bytes=self.received_bytes,
                    media_bytes=self.media_bytes)

    async def fetch(self, url, limit, *, captions=False, media=False):
        if not allowed_url(url, captions=captions):
            raise HighlightsError('UNSAFE_URL')
        if self.received_bytes >= self.max_bytes:
            raise HighlightsError('TRANSFER_LIMIT')
        async with self.session.get(url, allow_redirects=False) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise HighlightsError('YOUTUBE_UNAVAILABLE')
            if response.content_length and response.content_length > min(limit, self.max_bytes-self.received_bytes):
                raise HighlightsError('TRANSFER_LIMIT')
            body = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                self.received_bytes += len(chunk)
                if media:
                    self.media_bytes += len(chunk)
                if self.received_bytes > self.max_bytes or len(body)+len(chunk) > limit:
                    raise HighlightsError('TRANSFER_LIMIT')
                body.extend(chunk)
            if not body:
                raise HighlightsError('YOUTUBE_UNAVAILABLE')
            return bytes(body)

    async def prepare(self, url):
        info = await metadata(url)
        try:
            self.duration = float(info.get('duration') or 0)
            if (not math.isfinite(self.duration) or not 120 <= self.duration <= self.max_duration
                    or info.get('live_status') in {'is_live','is_upcoming','post_live'}
                    or info.get('is_live') or info.get('has_drm')
                    or info.get('availability') in {'private','premium_only','subscriber_only','needs_auth'}):
                raise ValueError
        except (ValueError, TypeError):
            raise HighlightsError('SOURCE_UNSUPPORTED') from None
        # Prefer muxed AVC/AAC VOD at <=720p. No separate audio rendition,
        # master playlist resolution or full HTTP resource fallback.
        formats = [f for f in info.get('formats', [])
                   if f.get('protocol') in {'m3u8','m3u8_native'}
                   and str(f.get('vcodec','')).startswith(('avc1','h264'))
                   and str(f.get('acodec','')).startswith(('mp4a','aac'))
                   and 0 < (f.get('height') or 0) <= 720 and not f.get('has_drm')
                   and allowed_url(f.get('url',''))]
        if not formats:
            raise HighlightsError('PARTIAL_UNSUPPORTED')
        formats.sort(key=lambda f: abs((f.get('height') or 0)-480))
        for f in formats[:3]:
            try:
                body = await self.fetch(f['url'], 1024*1024)
                self.parts = parse_playlist(body.decode('utf-8'), f['url'], self.duration)
                break
            except (HighlightsError, UnicodeError):
                continue
        if not self.parts:
            raise HighlightsError('PARTIAL_UNSUPPORTED')
        language = info.get('language') or ''
        for name in ('subtitles', 'automatic_captions'):
            tracks = info.get(name) or {}
            # Manual first; original language then ru/en then other originals.
            keys = sorted(tracks, key=lambda k:(k!=language, k not in {'ru','en'},k))
            attempts = 0
            for lang in keys:
                for track in tracks[lang]:
                    if track.get('ext') != 'json3' or not allowed_url(track.get('url',''),captions=True):
                        continue
                    # Automatic translated copies are not the original speech.
                    if 'tlang' in parse_qs(urlsplit(track['url']).query):
                        continue
                    attempts += 1
                    if attempts > 3:
                        break
                    try:
                        body = await self.fetch(track['url'], 2*1024*1024,captions=True)
                        return captions_json3(body, self.duration, lang)
                    except HighlightsError:
                        continue
                if attempts > 3:
                    break
        raise HighlightsError('CAPTIONS_UNAVAILABLE')

    async def download_clip(self, start, end, directory: Path) -> Path:
        selected = select_parts(self.parts, start, end, self.duration)
        directory.mkdir(parents=True, exist_ok=True)
        raw = directory/'selected.ts'
        try:
            with raw.open('wb') as output:
                for part in selected:
                    # No giant segment or remote input is ever handed to FFmpeg.
                    body = await self.fetch(part.url, 8*1024*1024, media=True)
                    # TS packets are 188 bytes, and start with sync byte 0x47.
                    if len(body)<188 or body[0]!=0x47 or (len(body)>376 and body[188]!=0x47):
                        raise HighlightsError('INVALID_MEDIA')
                    output.write(body)
            out = directory/'clip.mp4'
            result = await FFmpegRunner().run([
                '-protocol_whitelist','file', '-ss',f'{start-selected[0].start:.3f}',
                '-i',str(raw), '-t',f'{end-start:.3f}',
                '-map','0:v:0','-map','0:a:0','-c:v','libx264','-threads','2',
                '-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-c:a','aac',
                '-b:a','128k','-movflags','+faststart',str(out)],
                output_path=out, timeout_seconds=150)
            if not result.success:
                raise HighlightsError('CLIP_FAILED')
            probe = await get_probe_service().probe(out)
            if abs(probe.duration_seconds-(end-start))>1 or not probe.has_audio or not probe.width:
                raise HighlightsError('CLIP_FAILED')
            return out
        finally:
            raw.unlink(missing_ok=True)
