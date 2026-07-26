"""Standalone speech-to-text for voice messages.

The connector transcribes voice notes locally (no myagent call): a voice note is
the user "speaking" a message, so it becomes plain text at the edge and works
with any bound agent/model. Audio *files* sent as attachments are NOT handled
here — those are forwarded to myagent, which lets the model read them natively
or via document_extract.

Uses ffmpeg to normalize any codec to 16 kHz mono WAV, then faster-whisper to
transcribe. The Whisper model is loaded once (lazily) and reused; STT is
serialized on an asyncio lock (Whisper on CPU is heavy and not safe to run
concurrently on one model instance). Model size via MYAGENT_WHISPER_MODEL
(default 'small'); downloaded once on first use and cached under
~/.cache/huggingface.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile

log = logging.getLogger("connectors.stt")

_lock = asyncio.Lock()
_model = None            # lazily loaded WhisperModel
_unavailable: str | None = None  # reason STT can't run (missing dep), if any


def _load_model():
    from faster_whisper import WhisperModel  # imported lazily; heavy dependency
    size = os.environ.get("MYAGENT_WHISPER_MODEL", "small")
    log.info("loading Whisper model '%s' (first use downloads it)", size)
    return WhisperModel(size, device="cpu", compute_type="int8")


def _transcribe_sync(model, raw: bytes, language: str | None) -> str:
    tmpdir = tempfile.mkdtemp(prefix="stt_")
    src = os.path.join(tmpdir, "in")
    wav = os.path.join(tmpdir, "out.wav")
    try:
        with open(src, "wb") as f:
            f.write(raw)
        # Normalize to 16 kHz mono WAV — ffmpeg handles opus/oga/mp3/m4a/...
        conv = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
            capture_output=True,
        )
        audio_path = wav if conv.returncode == 0 and os.path.exists(wav) else src
        segments, _info = model.transcribe(audio_path, language=(language or None))
        return "".join(seg.text for seg in segments).strip()
    finally:
        for p in (src, wav):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


async def transcribe(raw: bytes, language: str | None = None) -> str:
    """Transcribe audio bytes to text. Raises RuntimeError if STT is
    unavailable (faster-whisper not installed) or transcription fails."""
    global _model, _unavailable
    if _unavailable:
        raise RuntimeError(_unavailable)
    async with _lock:
        if _model is None:
            try:
                _model = await asyncio.to_thread(_load_model)
            except Exception as e:
                _unavailable = f"speech-to-text non disponibile: {e}"
                log.warning("%s", _unavailable)
                raise RuntimeError(_unavailable)
        return await asyncio.to_thread(_transcribe_sync, _model, raw, language)
