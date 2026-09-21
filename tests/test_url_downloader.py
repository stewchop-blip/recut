"""Tests for URL validation and downloader."""

import pytest

from app.pipeline.url_downloader import DownloaderService, URLDownloadResult, UnsupportedURLError


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
