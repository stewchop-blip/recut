"""Tests for URL validation and downloader."""

import pytest
import asyncio

from app.pipeline.url_downloader import (
    DownloaderService, URLDownloadResult, UnsupportedURLError,
    StructuredDownloadError,
)


@pytest.fixture()
def svc():
    return DownloaderService()


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch?v=123",
    "https://youtu.be/123",
    "https://vm.tiktok.com/abc/",
    "https://www.tiktok.com/@u/video/1",
    "https://instagram.com/reel/abc/",
    "https://www.instagram.com/reel/abc/",
    "https://instagr.am/reel/abc/",
])
def test_supported_urls(svc, url):
    assert svc._validate(url).startswith("https://")


@pytest.mark.parametrize("url", [
    "http://youtube.com/watch?v=1",
    "http://instagram.com/reel/1",
    "ftp://tiktok.com/x",
    "not-a-url",
    "",
    "https://localhost/download",
    "https://127.0.0.1/x",
    "https://192.168.1.1/x",
    "https://example.com/video",
])
def test_unsupported_urls_rejected(svc, url):
    with pytest.raises(UnsupportedURLError):
        svc._validate(url)


async def test_ytdlp_failure_does_not_expose_signed_url(monkeypatch, svc):
    secret_url = "https://cdn.example/video.mp4?token=private-signature"
    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_data(f"ERROR: failed {secret_url}".encode())
    stderr.feed_eof()

    class FailedProcess:
        returncode = 1

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

    async def spawn(*args, **kwargs):
        return FailedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(StructuredDownloadError) as error:
        await svc._run_ytdlp(["yt-dlp", "https://youtu.be/abcdefghijk"])

    assert error.value.code == "YTDLP_METADATA_FAILED"
    assert secret_url not in str(error.value)
    assert secret_url not in error.value.detail
