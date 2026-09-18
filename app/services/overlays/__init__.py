"""CTA overlay services.

Stage I of the pipeline. Loads (or auto-generates) a CTA PNG and
overlays it onto each vertical MP4 at a configured position and time
window. The overlay is always inside the configured safe margin.
"""
from app.services.overlays.cta_generator import generate_default_cta, ensure_cta_asset
from app.services.overlays.cta import CTAService, CTAOverlaySpec

__all__ = [
    "CTAService",
    "CTAOverlaySpec",
    "generate_default_cta",
    "ensure_cta_asset",
]