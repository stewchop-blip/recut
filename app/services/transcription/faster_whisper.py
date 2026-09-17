"""faster-whisper implementation of TranscriptionService.

Model loading is lazy — the underlying `WhisperModel` is constructed on
the first call to `transcribe()` and reused for the lifetime of the
process. On Railway 512 MB the small+int8 model takes ~400 MB peak
RSS, so we gate construction behind `memory_guard.require_free_for_whisper`.
"""
import asyncio
import time
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.memory_guard import MemoryGuardError, require_free_for_whisper
from app.services.transcription.base import (
    Segment,
    TranscriptionError,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
    Word,
)

logger = get_logger(__name__)


class WhisperTranscriptionService(TranscriptionService):
    """Lazy-loaded faster-whisper wrapper."""

    def __init__(
        self,
        model: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ) -> None:
        s = get_settings()
        self._model_name = model or s.whisper_model
        self._device = device or s.whisper_device
        self._compute_type = compute_type or s.whisper_compute_type
        self._model = None  # populated on first transcribe()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "faster-whisper"

    @property
    def model_id(self) -> str:
        return self._model_name

    async def _ensure_loaded(self) -> None:
        """Load the model once. Refuses if RAM is insufficient."""
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            try:
                require_free_for_whisper(self._model_name)
            except MemoryGuardError as e:
                logger.error("whisper_memory_guard_failed", error=str(e))
                raise TranscriptionError(str(e)) from e

            logger.info(
                "whisper_loading",
                model=self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
            t0 = time.monotonic()
            try:
                # faster_whisper.WhisperModel is CPU-only here; import
                # lazily so the module is only loaded when actually used.
                from faster_whisper import WhisperModel
                self._model = WhisperModel(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                )
            except Exception as e:
                logger.error("whisper_load_failed", error=str(e)[:200])
                raise TranscriptionError(f"Failed to load whisper model: {e}") from e

            dt = time.monotonic() - t0
            logger.info("whisper_loaded", model=self._model_name, load_seconds=round(dt, 1))

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        await self._ensure_loaded()
        assert self._model is not None

        loop = asyncio.get_running_loop()

        def _run() -> TranscriptionResult:
            # faster_whisper yields (segments_iter, info). Run synchronously
            # inside a worker thread so the event loop stays responsive.
            segments_iter, info = self._model.transcribe(
                request.audio_path,
                language=request.language or None,
                beam_size=request.beam_size,
                word_timestamps=request.word_timestamps,
                vad_filter=False,
            )
            segs: list[Segment] = []
            text_parts: list[str] = []
            for seg in segments_iter:
                words: tuple[Word, ...] = ()
                if request.word_timestamps and getattr(seg, "words", None):
                    words = tuple(
                        Word(start=w.start, end=w.end, text=w.word)
                        for w in seg.words
                    )
                segs.append(Segment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=(seg.text or "").strip(),
                    words=words,
                ))
                if seg.text:
                    text_parts.append(seg.text.strip())
            return TranscriptionResult(
                segments=tuple(segs),
                language=info.language or request.language,
                duration_seconds=float(info.duration),
                model=self._model_name,
                raw_text=" ".join(text_parts).strip(),
            )

        t0 = time.monotonic()
        try:
            result = await loop.run_in_executor(None, _run)
        except Exception as e:
            logger.error("whisper_transcribe_failed", error=str(e)[:200])
            raise TranscriptionError(f"Transcription failed: {e}") from e
        dt = time.monotonic() - t0
        logger.info(
            "whisper_transcribed",
            segments=len(result.segments),
            duration_seconds=round(result.duration_seconds, 1),
            took_seconds=round(dt, 1),
            chars=len(result.raw_text),
        )
        return result


# Global instance — model is loaded on first transcribe(), reused after.
_service: WhisperTranscriptionService | None = None


def get_transcription_service() -> WhisperTranscriptionService:
    global _service
    if _service is None:
        _service = WhisperTranscriptionService()
    return _service