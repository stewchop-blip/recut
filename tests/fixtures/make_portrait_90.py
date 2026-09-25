import asyncio
import subprocess
from pathlib import Path
from app.services.media.probe import get_probe_service
from app.services.media.geometry import get_display_geometry

def create_portrait_rot90():
    out = Path("tests/fixtures/portrait_90.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:d=2",
        "-vf", "setsar=1,transpose=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)
    ], check=True)
    print("portrait_90 fixture:", out.stat().st_size, "bytes")

if __name__ == "__main__":
    create_portrait_rot90()
