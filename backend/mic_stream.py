"""
Real-time audio capture for the Invisible Interview Assistant.

The single class in this module, `StreamingCapture`, opens a
sounddevice `InputStream` on the configured device (default: CABLE
Output from VB-Audio Virtual Cable, which is the right source during
a Zoom / Meet / Teams call) and pushes 4 KB chunks of mono 16 kHz
int16 PCM into an `asyncio.Queue`. The audio runs at the device's
native sample rate and channel count and is resampled / downmixed
inside the callback, so this works with a system-audio device that
defaults to 48 kHz stereo.

The old `MicrophoneStream` + `collect_speech_segment` interface has
been replaced: there's no more VAD-based segmentation, because the
backend now consumes a continuous stream and decides on its own when
an utterance has ended (see `backend/main.py:microphone_loop`).
"""
from __future__ import annotations

import asyncio
import math
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

from config.settings import Settings


# How many int16 samples to enqueue per chunk. 4000 samples = 0.25s
# at 16 kHz. Same as the old BLOCK_SIZE.
_CHUNK_FRAMES = 4000


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(math.sqrt(np.mean(np.square(samples)) + 1e-12))


def resample_mono(samples_mono: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
    """
    Linearly resample a mono float32 array from `src_rate` to `dst_rate`.
    Good enough for VAD/transcription (Vosk only needs ~16 kHz mono).
    """
    if src_rate == dst_rate or samples_mono.size == 0:
        return samples_mono
    duration = samples_mono.size / float(src_rate)
    n_dst = max(1, int(round(duration * dst_rate)))
    # np.interp avoids pulling in scipy; fine for voice-band audio.
    src_x = np.linspace(0.0, duration, num=samples_mono.size, endpoint=False)
    dst_x = np.linspace(0.0, duration, num=n_dst, endpoint=False)
    return np.interp(dst_x, src_x, samples_mono).astype(np.float32)


def find_cable_output_device() -> Optional[int]:
    """
    Find the first input device whose name contains 'CABLE Output'
    (the read-side of VB-Audio Virtual Cable). Returns the device
    index, or None if no such device is installed.

    Prefers Windows WASAPI (lowest latency) over the legacy MME
    backend when both expose the same device.
    """
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        return None
    wasapi_index = next(
        (i for i, api in enumerate(host_apis) if "WASAPI" in api.get("name", "")),
        None,
    )
    best: Optional[int] = None
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) <= 0:
            continue
        if "CABLE Output" not in d.get("name", ""):
            continue
        # Always prefer the WASAPI host for the same logical device.
        if d.get("hostapi") == wasapi_index:
            return i
        if best is None:
            best = i
    return best


class StreamingCapture:
    """
    Wraps a sounddevice InputStream and pushes int16 PCM chunks into
    an asyncio queue. The consumer (an async loop) pulls them and
    feeds them into Vosk for streaming transcription.

    Lifecycle:
        cap = StreamingCapture(settings)
        cap.start(loop)                       # opens the device
        async for chunk in cap.chunks(): ...  # consume forever
        cap.stop()
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

        # Decide which device index to use based on audio_source.
        self.device_index: Optional[int] = self._resolve_device_index()
        if self.device_index is None:
            raise RuntimeError(
                "No audio input device available. Set AUDIO_SOURCE / "
                "CABLE_DEVICE_INDEX in .env, or install VB-Audio CABLE."
            )

        device_info = sd.query_devices(self.device_index)
        # Open the stream at the device's NATIVE rate / channels; we
        # resample and downmix in the callback. Forcing 16 kHz here
        # can fail on WASAPI devices that only support 48 kHz.
        self._native_rate = int(device_info.get("default_samplerate", 48000))
        self._native_channels = min(int(device_info.get("max_input_channels", 2)), 2)

        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
        self._stop_event = threading.Event()
        self._stream: Optional[sd.InputStream] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- device selection ------------------------------------------

    def _resolve_device_index(self) -> Optional[int]:
        src = self.settings.audio_source
        if src == "cable":
            # Try the explicit index from settings first, then auto-detect.
            try:
                sd.check_input_settings(settings=self.settings,  # type: ignore[arg-type]
                                        device=self.settings.cable_device_index)
                return self.settings.cable_device_index
            except Exception:
                pass
            found = find_cable_output_device()
            if found is not None:
                return found
            # Fall back to legacy DEVICE_INDEX.
            return self.settings.legacy_device_index
        if src == "mic":
            return (self.settings.mic_device_index
                    or self.settings.legacy_device_index)
        # Unknown source — try CABLE auto-detect.
        return find_cable_output_device() or self.settings.legacy_device_index

    @property
    def description(self) -> str:
        """Human-readable description of what's being captured."""
        try:
            name = sd.query_devices(self.device_index)["name"]
        except Exception:
            name = f"device {self.device_index}"
        return (f"{name} @ {self._native_rate}Hz "
                f"({self._native_channels}ch) -> 16kHz mono int16")

    # -- lifecycle --------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Open the input stream and start the audio thread."""
        self._loop = loop
        self._stream = sd.InputStream(
            samplerate=self._native_rate,
            channels=self._native_channels,
            dtype="float32",
            blocksize=_CHUNK_FRAMES,
            callback=self._on_audio,
            device=self.device_index,
        )
        self._stream.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # -- callback (runs on sounddevice's thread) --------------------

    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            # Filter the noisy "input overflow" status on Windows;
            # not actionable. Other statuses get printed.
            if "overflow" not in str(status).lower():
                print(f"[mic] status: {status}")

        # Downmix to mono float32 in [-1, 1].
        if indata.ndim == 2 and indata.shape[1] > 1:
            samples = indata.mean(axis=1).astype(np.float32, copy=True)
        else:
            samples = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()

        # Cheap VAD: skip pure-silence chunks entirely.
        if _rms(samples) < self.settings.silence_rms:
            return

        # Resample to 16 kHz if needed.
        if self._native_rate != self.settings.sample_rate:
            samples = resample_mono(samples, self._native_rate,
                                    self.settings.sample_rate)

        # Convert float32 [-1, 1] -> int16 little-endian bytes.
        pcm = np.clip(samples, -1.0, 1.0)
        pcm_i16 = (pcm * 32767.0).astype(np.int16).tobytes()

        if self._loop is None:
            return
        # Thread-safe enqueue from the audio thread; drop the oldest
        # chunk if the queue is full so we never block the audio path.
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm_i16)
        except asyncio.QueueFull:
            try:
                self._loop.call_soon_threadsafe(self._queue.get_nowait)
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm_i16)
            except Exception:
                pass

    # -- consumer API ----------------------------------------------

    async def chunks(self):
        """Async generator yielding raw int16 PCM chunks forever."""
        while not self._stop_event.is_set():
            chunk = await self._queue.get()
            yield chunk
