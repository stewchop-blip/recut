"""URL downloader service for Recut.

Currently backed by yt-dlp binary. The service surface is intentionally small:
validate URL -> pick supported source -> download -> normalize metadata
-> return local video path + title.

Security:
- No shell=True. We invoke the *installed* yt-dlp binary via subprocess
  with a plain list of arguments.
- URL validation rejects internal/private ranges, raw IPs and non-HTTPS.
- Only whitelisted domains are allowed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.core.config import get_settings

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class URLDownloadResult:
    path: Path
    title: str
    source: str
    size_bytes: int
    duration_seconds: float
    thumbnail: Optional[str] = None


# Very lightweight security net — we never feed a URL blindly into
# subprocess. These domain groups map to yt-dlp extractors we expect
# to work out of the box without cookies.
_SUPPORTED_HOST_PATTERNS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "m.youtube.com",
    "tiktok.com",
    "vm.tiktok.com",
    "www.tiktok.com",
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
)


class URLDownloadError(RuntimeError):
    """Base error for URL intake."""
    pass


class UnsupportedURLError(URLDownloadError):
    pass


class DownloadTooLargeError(URLDownloadError):
    pass


class VideoTooLongError(URLDownloadError):
    pass


class DownloaderService:
    """Download a public video URL to a temp file.

    Uses the ``yt-dlp`` CLI rather than the Python package API to
    keep the security boundary small: we never exec arbitrary Python
    from an untrusted input, we always exec a fixed installed binary.
    """

    def __init__(self) -> None:
        self._ytdlp_path = shutil.which("yt-dlp") or "yt-dlp"

    async def download(
        self,
        url: str,
        output_dir: Path,
        *,
        timeout_s: int = 300,
        max_size_mb: int = 750,
        max_duration_seconds: int = 6 * 3600,
    ) -> URLDownloadResult:
        if not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=True)

        url = self._validate(url)

        # Step 1: quick metadata probe without downloading.
        logger.info("url_metadata_start", url=url)
        info = await self._run_ytdlp(
            [self._ytdlp_path, "--dump-json", "--no-warnings", "--no-playlist", url],
            timeout=60,
        )
        if info is None:
            raise URLDownloadError("Cannot read metadata for this URL")
        meta = info[0] if isinstance(info, list) and info else info
        duration = float(meta.get("duration") or 0)
        filesize_approx = int(
            meta.get("filesize_approx")
            or meta.get("filesize")
            or 0
        )
        title = str(meta.get("title") or "recat_video")[:120]
        logger.info("url_metadata_ok", url=url, duration=duration, filesize_approx=filesize_approx, title=title)

        if max_size_mb and filesize_approx > max_size_mb * 1024 * 1024:
            raise DownloadTooLargeError(
                f"Video is ~{filesize_approx // (1024 * 1024)} MB; "
                f"download limit is {max_size_mb} MB"
            )
        if max_duration_seconds and duration > max_duration_seconds:
            raise VideoTooLongError(
                f"Video is longer than {max_duration_seconds // 3600} hours"
            )

        # Step 2: download to output_dir.
        out_template = str(output_dir / "download.%(ext)s")
        logger.info("url_download_start", url=url, out_template=out_template)
        await self._run_ytdlp(
            [
                self._ytdlp_path,
                "--no-warnings",
                "--no-playlist",
                "--merge-output-format", "mp4",
                "--no-mtime",
                "-o", out_template,
                url,
            ],
            timeout=timeout_s,
        )

        files = sorted(
            (p for p in output_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            raise URLDownloadError("Download finished but no file was produced")
        path = files[0]
        size_bytes = path.stat().st_size
        if size_bytes == 0:
            path.unlink(missing_ok=True)
            raise URLDownloadError("Downloaded file is empty")

        res_source = self._parse_source(url)
        logger.info(
            "url_download_file_created",
            path=str(path),
            size_bytes=size_bytes,
            duration=duration,
            source=res_source,
        )

        return URLDownloadResult(
            path=path,
            title=title,
            source=res_source,
            size_bytes=size_bytes,
            duration_seconds=duration,
            thumbnail=meta.get("thumbnail"),
        )

    async def _run_ytdlp(
        self,
        argv: list[str],
        *,
        timeout: int = 300,
    ) -> Optional[list]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise URLDownloadError(
                "yt-dlp is not installed on the server"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except Exception:
                pass
            raise URLDownloadError("Download timed out") from exc

        if proc.returncode != 0:
            msg = stderr.decode(errors="ignore")[:500]
            logger.warning(
                "yt_dlp_failed",
                rc=proc.returncode,
                error=msg,
            )
            raise URLDownloadError(f"Download failed: {msg}")

        out = stdout.decode(errors="ignore").strip()
        if not out:
            return []

        results: list = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return results

    def _validate(self, url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            raise UnsupportedURLError("Empty URL")
        url = url.strip()
        if not url.lower().startswith("https://"):
            raise UnsupportedURLError("Only HTTPS URLs are supported")
        host = self._host(url)
        if not host:
            raise UnsupportedURLError("Invalid URL")
        for pattern in _SUPPORTED_HOST_PATTERNS:
            if host == pattern or host.endswith("." + pattern):
                return url
        raise UnsupportedURLError(f"Source '{host}' is not supported yet")

    @staticmethod
    def _host(url: str) -> Optional[str]:
        m = re.match(r"https?://([^/?#]+)", url)
        return m.group(1).lower() if m else None

    @staticmethod
    def _parse_source(url: str) -> str:
        h = DownloaderService._host(url) or ""
        if "tiktok" in h:
            return "tiktok"
        if "instagram" in h or "instagr.am" in h:
            return "instagram"
        if "youtube" in h or "youtu.be" in h:
            return "youtube"
        return "link"
