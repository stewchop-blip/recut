"""Make small synthetic WAV files for tests using stdlib `wave`.

We don't need real speech — just a valid 16 kHz mono 16-bit WAV that
faster-whisper will refuse to load (no speech), but a fake
TranscriptionService can pretend to transcribe.
"""
import struct
import wave
from pathlib import Path


def make_silence_wav(path: Path, seconds: float = 0.5, sample_rate: int = 16_000) -> Path:
    """Write a silence-only mono 16-bit WAV."""
    n_samples = int(seconds * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return path


def make_sine_wav(path: Path, seconds: float = 1.0, freq: int = 440, sample_rate: int = 16_000) -> Path:
    """Write a 440 Hz sine wave mono 16-bit WAV.

    Not actual speech, but valid PCM that any WAV-aware tool will load.
    """
    import math
    n_samples = int(seconds * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            sample = int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack("<h", sample)
        wf.writeframes(bytes(frames))
    return path