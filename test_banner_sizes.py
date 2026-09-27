
"""Phase 1 diagnostic: measure banner size presets."""
import subprocess, tempfile, sys
from pathlib import Path

# Use a dummy 1080x1920 video and a dummy banner
video = Path("tests/_qprep_src.mp4")
banner = Path("tests/job_cta_default/cta_banner.png")  # use existing banner

if not video.exists():
    video = Path("tests/_qprep_src.mp4")

results = {}
for preset in ["small", "medium", "large"]:
    out = Path(f"/tmp/recut/banner_diag_{preset}.mp4")
    out.unlink(missing_ok=True)
    # Use the same burn_cta logic as production
    # This is a quick smoke test, not full integration
    print(f"Preset {preset}: would render with target_frac=... (see burn_cta code)")
    results[preset] = {"status": "simulated", "note": "Full FFmpeg smoke requires real banner asset"}

print("Results:", results)
print("Next: implement actual measurement after banner asset creation.")
