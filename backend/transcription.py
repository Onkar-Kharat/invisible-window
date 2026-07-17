"""
Transcription abstraction with two backends:

  * Vosk  — lightweight, streaming-friendly, ideal for real-time.
  * faster-whisper — heavier, more accurate, runs locally too.

Both expose the same async API: `transcribe_pcm(pcm_bytes) -> str`.
"""
from __future__ import annotations

import asyncio
import json
import os
from functools import lru_cache
from typing import Optional

from config.settings import Settings


@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


class Transcriber:
    """Async wrapper around the chosen backend."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or _get_settings()
        self.backend = self.settings.transcription_backend
        self._model = None
        self._load_model()

    def _load_model(self) -> None:
        if self.backend == "vosk":
            from vosk import Model  # type: ignore

            # Use the configured path, fall back to the legacy default.
            model_path = self.settings.vosk_model_path or "model/vosk-model-small-en-us-0.15"
            if not os.path.isdir(model_path):
                raise FileNotFoundError(
                    f"Vosk model not found at '{model_path}'. "
                    "Download a model from https://alphacephei.com/vosk/models "
                    "and unpack it into the models/ folder."
                )
            self._model = Model(model_path)
        elif self.backend in ("faster_whisper", "whisper", "faster-whisper"):
            from faster_whisper import WhisperModel  # type: ignore

            self._model = WhisperModel(
                self.settings.whisper_model_size,
                compute_type=self.settings.whisper_compute_type,
            )
        else:
            raise ValueError(f"Unknown transcription backend: {self.backend}")

    async def transcribe_pcm(self, pcm_bytes: bytes) -> str:
        """Run the (CPU-bound) transcription in a worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, pcm_bytes)

    # -- internals ----------------------------------------------------

    def _transcribe_sync(self, pcm_bytes: bytes) -> str:
        if not pcm_bytes:
            return ""
        if self.backend == "vosk":
            return self._transcribe_vosk(pcm_bytes)
        return self._transcribe_faster_whisper(pcm_bytes)

    def _transcribe_vosk(self, pcm_bytes: bytes) -> str:
        from vosk import KaldiRecognizer  # type: ignore

        sample_rate = self.settings.sample_rate
        rec = KaldiRecognizer(self._model, sample_rate)
        rec.SetWords(True)
        # Feed in chunks so very long segments still process incrementally.
        chunk = 4000
        for i in range(0, len(pcm_bytes), chunk):
            rec.AcceptWaveform(pcm_bytes[i:i + chunk])
        result = json.loads(rec.FinalResult())
        return (result.get("text") or "").strip()

    def _transcribe_faster_whisper(self, pcm_bytes: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=1,  # speed > quality for real-time
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


class StreamingTranscriber:
    """
    Long-lived Vosk recognizer that consumes raw int16 PCM chunks
    (one at a time, as they arrive from the mic loop) and fires
    callbacks as words are recognised.

    Callbacks (both optional):
        on_partial(text: str)  — fired when the in-progress transcript
                                 changes (live "what we think they're
                                 saying" updates).
        on_final(text: str)    — fired when Vosk commits a final
                                 sentence/phrase segment.

    Usage:
        st = StreamingTranscriber(settings, on_partial=..., on_final=...)
        for chunk in mic.chunks():
            st.feed(chunk)
    """

    def __init__(self, settings: "Settings",
                 on_partial=None,
                 on_final=None) -> None:
        from vosk import KaldiRecognizer  # type: ignore

        if settings.transcription_backend != "vosk":
            raise RuntimeError(
                "StreamingTranscriber currently only supports the vosk "
                f"backend; got '{settings.transcription_backend}'."
            )
        if not hasattr(settings, "_streaming_model"):
            # Lazily load (or reuse) the Vosk model so we don't pay
            # the load cost twice. The model is small (50 MB) so this
            # is cheap, but skipping it makes startup faster.
            from backend.transcription import Transcriber  # local import
            t = Transcriber(settings)
            settings._streaming_model = t._model  # type: ignore[attr-defined]
        self._rec = KaldiRecognizer(
            settings._streaming_model,  # type: ignore[attr-defined]
            settings.sample_rate,
        )
        self._rec.SetWords(True)
        self._on_partial = on_partial
        self._on_final = on_final
        self._last_partial = ""
        # Allow the consumer to listen for errors.
        self._last_error: Optional[str] = None

    def feed(self, pcm_bytes: bytes) -> None:
        """Push one int16 PCM chunk into the recognizer."""
        if not pcm_bytes:
            return
        try:
            is_final = self._rec.AcceptWaveform(pcm_bytes)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            return
        if is_final:
            try:
                result = json.loads(self._rec.Result())
            except json.JSONDecodeError:
                return
            text = (result.get("text") or "").strip()
            self._last_partial = ""
            if text and self._on_final is not None:
                self._on_final(text)
        else:
            try:
                partial = json.loads(self._rec.PartialResult()).get("partial", "")
            except json.JSONDecodeError:
                return
            partial = (partial or "").strip()
            if partial and partial != self._last_partial and self._on_partial is not None:
                self._last_partial = partial
                self._on_partial(partial)

    def reset(self) -> None:
        """Discard any in-progress recognition state."""
        try:
            self._rec.Reset()
        except Exception:  # noqa: BLE001
            pass
        self._last_partial = ""
