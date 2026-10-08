from unittest.mock import AsyncMock

import pytest

from app.pipeline.instagram_proxy import InstagramProxyConfigError, instagram_proxy_url
from app.pipeline.url_downloader import DownloaderService, StructuredDownloadError


@pytest.mark.parametrize("message", ["HTTP Error 407", "Proxy Authentication Required",
                                    "Tunnel connection failed: 407"])
def test_proxy_auth_failure_is_distinct(message):
    from app.pipeline.url_downloader import classify_ytdlp_error
    assert classify_ytdlp_error(message, "instagram") == "INSTAGRAM_PROXY_AUTH_REQUIRED"


@pytest.mark.parametrize("value", ["", "http://user:pass@proxy.example:8000",
    "https://proxy.example:443", "socks5://proxy.example:1080", "socks5h://proxy.example:1080"])
def test_proxy_config(value, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_PROXY_URL", value)
    assert instagram_proxy_url() == (value or None)


@pytest.mark.parametrize("value", ["secret", "ftp://proxy.example", "http://proxy.example:bad",
    "http://proxy.example:65536", "http://proxy.example:0", "http://proxy.example/path",
    "http://proxy.example?password=secret", "http://proxy.example#secret", "http://user:secret@"])
def test_invalid_config_is_safe(value, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_PROXY_URL", value)
    with pytest.raises(InstagramProxyConfigError, match="^instagram_proxy_config_invalid$"):
        instagram_proxy_url()


@pytest.mark.parametrize("platform", ["instagram", "tiktok"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_route_is_scoped_and_covers_metadata_and_media(tmp_path, monkeypatch, platform, fallback):
    import app.pipeline.instagram_public as public
    proxy = "http://route-user:route-pass@proxy.example:8000"
    monkeypatch.setenv("INSTAGRAM_PROXY_URL", proxy)
    monkeypatch.delenv("INSTAGRAM_COOKIES_B64", raising=False)
    monkeypatch.delenv("TIKTOK_COOKIES_B64", raising=False)
    service = DownloaderService()
    calls = []
    metadata = {"id": "abc", "title": "test", "duration": 2}
    public_download = AsyncMock(return_value=metadata)
    monkeypatch.setattr(public, "extract_public_video", public_download)
    async def run(args, **kwargs):
        calls.append(args)
        if "--dump-json" in args:
            if fallback and platform == "instagram":
                raise StructuredDownloadError("INSTAGRAM_AUTH_REQUIRED", "Instagram request failed")
            return [metadata]
        (tmp_path / "download.mp4").write_bytes(b"video")
        return []
    monkeypatch.setattr(service, "_run_ytdlp", run)
    url = ("https://www.instagram.com/reel/abc/" if platform == "instagram"
           else "https://www.tiktok.com/@test/video/1234567890")
    await service.download(url, tmp_path)
    assert len(calls) == 2
    for args in calls:
        assert ("--proxy" in args) == (platform == "instagram")
        if platform == "instagram":
            assert args[args.index("--proxy") + 1] == proxy
    assert public_download.await_count == int(fallback and platform == "instagram")
    assert ("--load-info-json" in calls[-1]) == (platform == "instagram")


async def test_invalid_instagram_proxy_fails_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "invalid-secret")
    service = DownloaderService()
    run = AsyncMock()
    monkeypatch.setattr(service, "_run_ytdlp", run)
    with pytest.raises(StructuredDownloadError) as error:
        await service.download("https://www.instagram.com/reel/abc/", tmp_path)
    assert error.value.code == "INSTAGRAM_PROXY_CONFIG_INVALID"
    assert "invalid-secret" not in str(error.value)
    run.assert_not_called()


def test_proxy_credentials_are_masked(monkeypatch):
    from app.core.logging import mask_secrets_processor
    proxy = "http://route-user:route%2Dpassword@proxy.example:8000"
    monkeypatch.setenv("INSTAGRAM_PROXY_URL", proxy)
    output = mask_secrets_processor(None, "error", {
        "error": f"{proxy} route-user route%2Dpassword route-password",
        "proxy_url": "anything", "route": "proxy"})
    assert output["route"] == "proxy"
    for value in (proxy, "route-user", "route%2Dpassword", "route-password", "anything"):
        assert value not in str(output)
