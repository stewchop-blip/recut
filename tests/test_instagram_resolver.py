import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.pipeline import instagram_resolver as resolver


SIGNED = "https://video.cdninstagram.com/test.mp4?signature=DO_NOT_LOG"
SECRET = "test-secret-only-" + "x" * 32
URL = "https://www.instagram.com/reel/abc/"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_RESOLVER_URL", "https://resolver.test.workers.dev/instagram")
    monkeypatch.setenv("INSTAGRAM_RESOLVER_SECRET", SECRET)
    events = []
    class Logger:
        def info(self, event, **fields): events.append((event, fields))
        warning = info
    monkeypatch.setattr(resolver, "logger", Logger())
    return events


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(resolver.httpx, "AsyncClient", client)


def payload(**changes):
    return {"ok": True, "shortcode": "abc", "source": "embed", "video_url": SIGNED,
            "duration": 4, "width": 720, "height": 1280, **changes}


async def test_success_and_no_signed_url_or_secret_in_logs(monkeypatch, configured):
    def handler(request):
        assert request.headers["X-ReCut-Resolver-Secret"] == SECRET
        assert request.url.params["url"] == URL
        return httpx.Response(200, json=payload())
    transport(monkeypatch, handler)
    info = await resolver.extract_resolved_video(URL)
    assert info["formats"][0]["url"] == SIGNED
    assert "instagram_resolver_success" in str(configured)
    assert SIGNED not in str(configured)
    assert SECRET not in str(configured)


async def test_timeout(monkeypatch, configured):
    def handler(request): raise httpx.ReadTimeout(SIGNED + SECRET)
    transport(monkeypatch, handler)
    assert await resolver.extract_resolved_video(URL) is None
    assert "instagram_resolver_timeout" in str(configured)
    assert SECRET not in str(configured)


@pytest.mark.parametrize("status", [401, 502, 302])
async def test_http_error(monkeypatch, configured, status):
    transport(monkeypatch, lambda _: httpx.Response(status, text=SIGNED + SECRET))
    assert await resolver.extract_resolved_video(URL) is None
    assert f"instagram_resolver_http_{status}" in str(configured)
    assert "DO_NOT_LOG" not in str(configured)


async def test_invalid_json(monkeypatch, configured):
    transport(monkeypatch, lambda _: httpx.Response(200, text="invalid " + SIGNED))
    assert await resolver.extract_resolved_video(URL) is None
    assert "instagram_resolver_invalid_response" in str(configured)


@pytest.mark.parametrize("changes", [{"video_url": "https://cdninstagram.com.evil.test/video"},
    {"video_url": "https://127.0.0.1/video"}, {"shortcode": "other"}, {"source": SIGNED},
    {"duration": float("inf")}, {"duration": True}, {"width": -1}])
async def test_invalid_metadata(monkeypatch, configured, changes):
    transport(monkeypatch, lambda _: httpx.Response(200, text=json.dumps(payload(**changes))))
    assert await resolver.extract_resolved_video(URL) is None
    assert "DO_NOT_LOG" not in str(configured)


async def test_no_video(monkeypatch, configured):
    transport(monkeypatch, lambda _: httpx.Response(200, json={"ok": False, "reason": SIGNED}))
    assert await resolver.extract_resolved_video(URL) is None
    assert "instagram_resolver_no_video" in str(configured)
    assert "DO_NOT_LOG" not in str(configured)


async def test_disabled_env_never_calls_network(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_RESOLVER_URL", raising=False)
    network = AsyncMock()
    monkeypatch.setattr(resolver.httpx, "AsyncClient", network)
    assert await resolver.extract_resolved_video(URL) is None
    network.assert_not_called()


@pytest.mark.parametrize("url", ["https://localhost/instagram", "https://127.0.0.1/instagram",
    "https://example.workers.dev.evil.test/instagram", "http://example.workers.dev/instagram",
    "https://user@example.workers.dev/instagram", "https://example.workers.dev:8443/instagram"])
async def test_invalid_config_never_calls_network(monkeypatch, configured, url):
    monkeypatch.setenv("INSTAGRAM_RESOLVER_URL", url)
    network = AsyncMock()
    monkeypatch.setattr(resolver.httpx, "AsyncClient", network)
    assert await resolver.extract_resolved_video(URL) is None
    network.assert_not_called()


async def test_tiktok_never_sent_to_resolver(monkeypatch, configured):
    network = AsyncMock()
    monkeypatch.setattr(resolver.httpx, "AsyncClient", network)
    assert await resolver.extract_resolved_video("https://www.tiktok.com/@u/video/123") is None
    network.assert_not_called()


async def test_response_size_bound(monkeypatch, configured):
    transport(monkeypatch, lambda _: httpx.Response(200, text="x" * 32769))
    assert await resolver.extract_resolved_video(URL) is None


@pytest.mark.parametrize("resolved", [True, False])
async def test_downloader_resolver_success_or_local_fallback(tmp_path, monkeypatch, resolved):
    from app.pipeline import instagram_public as public
    from app.pipeline.url_downloader import DownloaderService, StructuredDownloadError
    monkeypatch.delenv("INSTAGRAM_COOKIES_B64", raising=False)
    monkeypatch.delenv("INSTAGRAM_PROXY_URL", raising=False)
    info = resolver.resolver_metadata(payload(), "abc")
    remote = AsyncMock(return_value=info if resolved else None)
    local = AsyncMock(return_value=info)
    monkeypatch.setattr(resolver, "extract_resolved_video", remote)
    monkeypatch.setattr(public, "extract_public_video", local)
    service = DownloaderService()
    async def run(args, **kwargs):
        if "--dump-json" in args:
            raise StructuredDownloadError("INSTAGRAM_AUTH_REQUIRED", "Instagram request failed")
        assert args[args.index("--proxy") + 1] == ""
        assert "--load-info-json" in args
        (tmp_path / "download.mp4").write_bytes(b"video")
        return []
    monkeypatch.setattr(service, "_run_ytdlp", run)
    assert (await service.download(URL, tmp_path)).duration_seconds == 4
    remote.assert_awaited_once_with(URL)
    assert local.await_count == int(not resolved)


async def test_downloader_tiktok_does_not_call_resolver(tmp_path, monkeypatch):
    from app.pipeline.url_downloader import DownloaderService
    remote = AsyncMock()
    monkeypatch.setattr(resolver, "extract_resolved_video", remote)
    monkeypatch.delenv("TIKTOK_COOKIES_B64", raising=False)
    service = DownloaderService()
    async def run(args, **kwargs):
        assert "--proxy" not in args
        if "--dump-json" in args:
            return [{"title": "TikTok", "duration": 4}]
        (tmp_path / "download.mp4").write_bytes(b"video")
        return []
    monkeypatch.setattr(service, "_run_ytdlp", run)
    await service.download("https://www.tiktok.com/@test/video/123", tmp_path)
    remote.assert_not_called()


def test_global_log_masking_redacts_resolver_secret(monkeypatch):
    from app.core.logging import mask_secrets_processor
    monkeypatch.setenv("INSTAGRAM_RESOLVER_SECRET", SECRET)
    output = mask_secrets_processor(None, "error", {"error": "request " + SECRET})
    assert SECRET not in str(output)
