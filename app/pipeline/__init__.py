"""Pipeline orchestration — pure logic, no Telegram imports.

Services:
- VideoValidator: size, duration, format checks
- VideoDownloader: streams Telegram file into a job_dir
- JobRegistry: convenience wrapper around JobRepository

The pipeline itself (transcribe → analyze → cut → render) is wired up
in later stages. This module stays small.
"""
from app.pipeline.validator import VideoValidator, VideoValidationError
from app.pipeline.downloader import VideoDownloader
from app.pipeline.extractor import AudioExtractor, AudioExtractionError

__all__ = [
    "VideoValidator",
    "VideoValidationError",
    "VideoDownloader",
    "AudioExtractor",
    "AudioExtractionError",
]
