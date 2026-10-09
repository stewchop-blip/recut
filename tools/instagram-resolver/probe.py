"""Run on Railway (preferred) or locally after publishing the Worker.

Secret is read from env or a hidden prompt; signed URLs never enter stdout/logs.
Downloads MP4 directly, with TLS verification and CDN redirect validation.
"""
import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

import httpx

REEL = "https://www.instagram.com/reel/DXRR2sziOG_/"


def cdn(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in {None, 443}
                and (parsed.hostname or "").endswith((".cdninstagram.com", ".fbcdn.net")))
    except (ValueError, TypeError):
        return False


def probe():
    endpoint = os.getenv("INSTAGRAM_RESOLVER_URL") or input("Worker /instagram URL: ").strip()
    p = urlsplit(endpoint)
    if (p.scheme != "https" or not (p.hostname or "").endswith(".workers.dev")
            or p.path != "/instagram" or p.username or p.password or p.query or p.fragment
            or p.port not in {None, 443}):
        return {"ok": False, "reason": "endpoint_invalid"}
    secret = os.getenv("INSTAGRAM_RESOLVER_SECRET") or getpass.getpass("Resolver secret (hidden): ")
    with httpx.Client(timeout=28, follow_redirects=False, trust_env=False) as client:
        r = client.get(endpoint, params={"url": REEL}, headers={"X-ReCut-Resolver-Secret": secret})
        if r.status_code != 200:
            return {"ok": False, "reason": f"resolver_http_{r.status_code}"}
        data = r.json()
        if data.get("ok") is not True or data.get("shortcode") != "DXRR2sziOG_" or not cdn(data.get("video_url")):
            return {"ok": False, "reason": "resolver_invalid_response"}
        url = data["video_url"]
    # New client: the resolver header is never forwarded to Instagram CDN.
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with tempfile.TemporaryDirectory(prefix="recut_resolver_probe_") as directory:
            path = Path(directory) / "probe.mp4"
            size = 0
            for _ in range(3):
                if not cdn(url):
                    return {"ok": False, "reason": "cdn_redirect_disallowed"}
                with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status_code != 200:
                        return {"ok": False, "reason": f"cdn_http_{response.status_code}"}
                    with path.open("wb") as output:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 50 * 1024 * 1024:
                                return {"ok": False, "reason": "test_size_limit"}
                            output.write(chunk)
                    break
            else:
                return {"ok": False, "reason": "cdn_redirect_limit"}
            if not size:
                return {"ok": False, "reason": "cdn_empty"}
            process = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=15)
            if process.returncode:
                return {"ok": False, "reason": "ffprobe_failed"}
            media = json.loads(process.stdout)
            streams = media.get("streams", [])
            return {"ok": any(s.get("codec_type") == "video" for s in streams),
                    "source": data["source"] if data.get("source") in
                    {"embed", "post_html", "graphql_post_root_271"} else "unknown",
                    "signed_url_received": True, "cdn_direct_bytes": size,
                    "audio": any(s.get("codec_type") == "audio" for s in streams),
                    "duration": media.get("format", {}).get("duration")}


if __name__ == "__main__":
    try:
        result = probe()
    except Exception:
        result = {"ok": False, "reason": "probe_transport_parse_or_tool_error"}
    print(json.dumps(result))
    raise SystemExit(0 if result.get("ok") else 1)
