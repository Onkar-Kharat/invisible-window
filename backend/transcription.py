"""
Transcription abstraction with two backends:

  * Vosk  — lightweight, streaming-friendly, ideal for real-time.
  * faster-whisper — heavier, more accurate, runs locally too.

A streaming variant (`StreamingTranscriber`) wraps Vosk and gates
incoming audio on a Voice Activity Detector so that only voiced
segments reach the recognizer. This keeps the transcription focused
on the interviewer's speech and dramatically reduces false
"fillers" / hallucinations during silence.

Both expose the same async API: `transcribe_pcm(pcm_bytes) -> str`.
"""
from __future__ import annotations

import asyncio
import json
import os
from functools import lru_cache
from typing import Callable, Optional

from config.settings import Settings


@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


# ---- VAD (WebRTC) -------------------------------------------------

class WebRTCVAD:
    """
    Thin wrapper around `webrtcvad` that takes raw 16-bit mono PCM at
    16 kHz (the sample rate the rest of the pipeline uses) and tells
    the caller whether each 20 ms frame contains speech.

    WebRTC VAD only supports 10/20/30 ms frames at 8/16/32/48 kHz.
    The audio layer always feeds us 16 kHz, so 20 ms = 320 samples
    = 640 bytes — that is the only frame size we use.
    """

    FRAME_MS = 20
    FRAME_SAMPLES = 320  # 16 kHz * 0.020s
    FRAME_BYTES = FRAME_SAMPLES * 2  # int16

    def __init__(self, aggressiveness: int = 2) -> None:
        try:
            import webrtcvad  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "webrtcvad is not installed. Run `pip install webrtcvad` "
                "or set VAD_MODE=energy in your .env."
            ) from exc
        if aggressiveness not in (0, 1, 2, 3):
            aggressiveness = 2
        self._vad = webrtcvad.Vad(int(aggressiveness))

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) < self.FRAME_BYTES:
            return False
        try:
            return bool(self._vad.is_speech(frame, 16000))
        except Exception:
            return False


class EnergyVAD:
    """RMS-threshold based VAD — same semantics as the legacy code path."""

    def __init__(self, threshold: float = 0.005) -> None:
        import math
        import numpy as np  # type: ignore

        self._math = math
        self._np = np
        self.threshold = float(threshold)

    def is_speech(self, frame: bytes) -> bool:
        np = self._np
        math = self._math
        if not frame:
            return False
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return False
        rms = float(math.sqrt(np.mean(np.square(samples)) + 1e-12))
        return rms >= self.threshold


def _make_vad(settings: Settings):
    mode = (settings.vad_mode or "webrtc").lower()
    if mode == "webrtc":
        try:
            return WebRTCVAD(settings.vad_aggressiveness)
        except Exception as exc:
            print(f"[transcription] WebRTC VAD unavailable ({exc}); "
                  "falling back to energy VAD.")
            mode = "energy"
    if mode == "energy":
        return EnergyVAD(
            settings.vad_rms_threshold
            or settings.silence_rms
            or 0.005
        )
    # mode == "none"  ->  a no-op VAD that always says "speech"
    class _AlwaysSpeech:
        def is_speech(self, frame: bytes) -> bool:  # noqa: ARG002
            return True
    return _AlwaysSpeech()


# ---- Transcriber (one-shot) ---------------------------------------

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


# ---- StreamingTranscriber (long-lived, VAD-gated) -----------------

class StreamingTranscriber:
    """
    Long-lived Vosk recognizer that consumes raw int16 mono PCM
    chunks at 16 kHz, gates them with a VAD, and fires callbacks as
    words are recognised.

    Callbacks (both optional):
        on_partial(text: str)  — fired when the in-progress transcript
                                 changes (live "what we think they're
                                 saying" updates).
        on_final(text: str)    — fired when Vosk commits a final
                                 sentence/phrase segment.
        on_speech(active: bool) — fired on VAD transitions
                                  (speech start / end). Useful for UI
                                  indicators.

    Usage:
        st = StreamingTranscriber(settings, on_partial=..., on_final=...)
        for chunk in mic.chunks():
            st.feed(chunk)
    """

    def __init__(self, settings: "Settings",
                 on_partial: Optional[Callable[[str], None]] = None,
                 on_final:   Optional[Callable[[str], None]] = None,
                 on_speech:  Optional[Callable[[bool], None]] = None) -> None:
        from vosk import KaldiRecognizer  # type: ignore

        if settings.transcription_backend != "vosk":
            raise RuntimeError(
                "StreamingTranscriber currently only supports the vosk "
                f"backend; got '{settings.transcription_backend}'."
            )
        if not hasattr(settings, "_streaming_model"):
            # Lazily load (or reuse) the Vosk model so we don't pay
            # the load cost twice.
            t = Transcriber(settings)
            settings._streaming_model = t._model  # type: ignore[attr-defined]
        self._rec = KaldiRecognizer(
            settings._streaming_model,  # type: ignore[attr-defined]
            settings.sample_rate,
        )
        self._rec.SetWords(True)
        self.settings = settings
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_speech = on_speech
        self._last_partial = ""
        self._last_error: Optional[str] = None
        # VAD setup
        self._vad = _make_vad(settings)
        self._vad_buffer = bytearray()
        self._frame_bytes = WebRTCVAD.FRAME_BYTES
        self._speech_active = False
        self._padding_bytes = int(
            settings.vad_padding_ms * 16 * 2  # 16 kHz mono int16
        )
        self._pre_roll = bytearray()
        # Force a frame-size commit even when VAD says "no speech" so
        # the recognizer's internal buffers get flushed periodically.
        self._silence_bytes_since_active = 0

    # -- public API --------------------------------------------------

    def feed(self, pcm_bytes: bytes) -> None:
        """Push one int16 mono PCM chunk (any size) into the recognizer."""
        if not pcm_bytes:
            return
        try:
            self._feed_with_vad(pcm_bytes)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{exc.__class__.__name__}: {exc}"

    def reset(self) -> None:
        """Discard any in-progress recognition state."""
        try:
            self._rec.Reset()
        except Exception:  # noqa: BLE001
            pass
        self._last_partial = ""
        self._vad_buffer.clear()
        self._pre_roll = bytearray()
        self._speech_active = False

    # -- internals ---------------------------------------------------

    def _feed_with_vad(self, pcm_bytes: bytes) -> None:
        # 1) Make sure we are aligned to the VAD frame size (320
        #    samples = 640 bytes @ 16 kHz). We accumulate any short
        #    remainder and prepend it to the next chunk.
        buf = self._vad_buffer
        buf.extend(pcm_bytes)
        # Anything that doesn't fit a full frame stays in the buffer.
        usable = len(buf) - (len(buf) % self._frame_bytes)
        if usable == 0:
            return
        aligned = bytes(buf[:usable])
        del buf[:usable]

        # 2) Walk frame-by-frame and decide what to forward.
        to_forward = bytearray()
        n_frames = len(aligned) // self._frame_bytes
        for i in range(n_frames):
            frame = aligned[i * self._frame_bytes:(i + 1) * self._frame_bytes]
            is_speech = bool(self._vad.is_speech(frame))
            if is_speech:
                if not self._speech_active:
                    self._speech_active = True
                    if self._on_speech is not None:
                        try:
                            self._on_speech(True)
                        except Exception:
                            pass
                    # Prepend a little audio from before the speech
                    # started so we don't chop the first phoneme.
                    if self._pre_roll:
                        to_forward.extend(self._pre_roll)
                        self._pre_roll.clear()
                to_forward.extend(frame)
                self._silence_bytes_since_active = 0
            else:
                if self._speech_active:
                    # Pass through a short tail so the recognizer
                    # gets a natural end-of-word.
                    to_forward.extend(frame)
                    self._silence_bytes_since_active += self._frame_bytes
                    if (self._silence_bytes_since_active
                            >= self.settings.silence_duration_ms * 16 * 2):
                        # End of speech: reset recognizer, fire
                        # FinalResult so we get a clean segment
                        # commit, then update UI state.
                        self._speech_active = False
                        self._silence_bytes_since_active = 0
                        self._finalize()
                        if self._on_speech is not None:
                            try:
                                self._on_speech(False)
                            except Exception:
                                pass
                else:
                    # Stay in pre-roll until we have enough audio
                    # to actually matter if speech starts.
                    self._pre_roll.extend(frame)
                    if len(self._pre_roll) > self._padding_bytes:
                        # Drop the oldest half so we don't grow
                        # unbounded.
                        del self._pre_roll[: self._padding_bytes // 2]

        # 3) Forward the selected bytes to Vosk.
        if to_forward:
            self._push_to_recognizer(bytes(to_forward))
        else:
            # Even if VAD found nothing, periodically ask Vosk for a
            # partial so the UI doesn't go stale.
            self._poll_partial()

    def _finalize(self) -> None:
        """Commit the current recognizer state and emit a final segment."""
        try:
            result = json.loads(self._rec.Result())
        except json.JSONDecodeError:
            result = {}
        text = (result.get("text") or "").strip()
        self._last_partial = ""
        if text and self._on_final is not None:
            self._on_final(text)
        try:
            self._rec.Reset()
        except Exception:
            pass

    def _push_to_recognizer(self, pcm_bytes: bytes) -> None:
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
            self._poll_partial()

    def _poll_partial(self) -> None:
        try:
            partial = json.loads(self._rec.PartialResult()).get("partial", "")
        except json.JSONDecodeError:
            return
        partial = (partial or "").strip()
        if partial and partial != self._last_partial and self._on_partial is not None:
            self._last_partial = partial
            self._on_partial(partial)
