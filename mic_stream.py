"""
mic_stream.py — standalone microphone capture client.

Captures audio from the configured input device (default: physical
microphone, or VB-Audio CABLE Output if `AUDIO_SOURCE=cable`) and
streams raw 16-bit mono PCM frames to the backend's
`/ws/audio` WebSocket endpoint. The backend runs the VAD + Vosk
recognizer and returns partial / final / committed-question events
on the same socket.

This module is the **client-side** counterpart to the in-process
microphone loop in `backend/main.py`. Use it if you want to capture
audio on a different machine from the one running the backend, or
if you simply prefer to run capture as a separate process.

Usage
-----

    python mic_stream.py                      # capture the default mic
    python mic_stream.py --source cable       # capture CABLE Output
    python mic_stream.py --device 3           # explicit device index
    python mic_stream.py --backend ws://localhost:8765/ws/audio

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None

from config.settings import Settings


def _resolve_device(source: str, settings: Settings) -> Optional[int]:
    """Pick a sounddevice input index for the requested source."""
    if source == "cable":
        try:
            devices = sd.query_devices()
            host_apis = sd.query_hostapis()
        except Exception:
            return None
        wasapi_index = next(
            (i for i, api in enumerate(host_apis) if "WASAPI" in api.get("name", "")),
            None,
        )
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) <= 0:
                continue
            if "CABLE Output" in d.get("name", ""):
                if d.get("hostapi") == wasapi_index:
                    return i
        return settings.cable_device_index if settings.cable_device_index >= 0 else None
    if source == "mic":
        if settings.mic_device_index is not None and settings.mic_device_index >= 0:
            return settings.mic_device_index
        try:
            return sd.default.device[0]
        except Exception:
            return None
    return None


def _audio_thread(device_index: Optional[int],
                  sample_rate: int,
                  frame_samples: int,
                  out_queue: "queue.Queue[bytes]",
                  stop_event: threading.Event) -> None:
    """Capture thread: sounddevice -> thread-safe queue of int16 PCM frames."""
    if device_index is None:
        print("[mic_stream] no input device available.", file=sys.stderr)
        return

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status and "overflow" not in str(status).lower():
            print(f"[mic_stream] status: {status}", file=sys.stderr)
        # Downmix to mono float32 in [-1, 1].
        if indata.ndim == 2 and indata.shape[1] > 1:
            samples = indata.mean(axis=1).astype(np.float32, copy=True)
        else:
            samples = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        # int16 little-endian
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        try:
            out_queue.put_nowait(pcm)
        except queue.Full:
            # Drop oldest; never block the audio callback.
            try:
                out_queue.get_nowait()
            except Exception:
                pass
            try:
                out_queue.put_nowait(pcm)
            except Exception:
                pass

    try:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=frame_samples,
            callback=callback,
            device=device_index,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[mic_stream] failed to open input device {device_index}: {exc}",
              file=sys.stderr)
        return

    with stream:
        while not stop_event.is_set():
            time.sleep(0.05)


def run(source: str,
        device_index: Optional[int],
        backend_url: str,
        sample_rate: int,
        block_ms: int) -> int:
    if websocket is None:
        print("[mic_stream] websocket-client is not installed. "
              "Run `pip install websocket-client`.", file=sys.stderr)
        return 1

    frame_samples = max(1, int(sample_rate * block_ms / 1000))
    frame_bytes = frame_samples * 2  # int16 mono

    audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=128)
    stop_event = threading.Event()
    cap_thread = threading.Thread(
        target=_audio_thread,
        args=(device_index, sample_rate, frame_samples, audio_q, stop_event),
        daemon=True,
    )
    cap_thread.start()

    backoff = 1.0
    while not stop_event.is_set():
        try:
            print(f"[mic_stream] connecting {backend_url}")
            ws = websocket.WebSocket()
            ws.connect(backend_url)
            # Send an initial config frame so the backend can verify
            # the sample rate matches its VAD/recognizer settings.
            ws.send(json.dumps({
                "type": "config",
                "sample_rate": sample_rate,
                "channels": 1,
            }))
            backoff = 1.0

            receiver = threading.Thread(
                target=_receive_loop, args=(ws, stop_event), daemon=True
            )
            receiver.start()

            while not stop_event.is_set():
                try:
                    pcm = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                # Split into <=16 KiB frames for the wire.
                for i in range(0, len(pcm), 16 * 1024):
                    ws.send_binary(pcm[i:i + 16 * 1024])
        except Exception as exc:  # noqa: BLE001
            print(f"[mic_stream] connection error: {exc}", file=sys.stderr)
            time.sleep(min(backoff, 10))
            backoff *= 1.5
        finally:
            try:
                ws.close()
            except Exception:
                pass

    stop_event.set()
    cap_thread.join(timeout=1.0)
    return 0


def _receive_loop(ws, stop_event: threading.Event) -> None:
    """Print transcription results coming back from the backend."""
    while not stop_event.is_set():
        try:
            msg = ws.recv()
        except Exception:
            return
        if not msg:
            continue
        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            continue
        kind = payload.get("type")
        if kind == "partial":
            print(f"  … {payload.get('text', '')}")
        elif kind == "final":
            print(f"  ✓ {payload.get('text', '')}")
        elif kind == "question":
            print(f"\n>>> {payload.get('text', '')}\n")
        elif kind == "ready":
            print("[mic_stream] backend ready")
        elif kind == "error":
            print(f"[mic_stream] backend error: {payload.get('message')}",
                  file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("mic", "cable"),
                        help="Audio source (overrides AUDIO_SOURCE env)")
    parser.add_argument("--device", type=int, default=None,
                        help="Explicit sounddevice input index")
    parser.add_argument("--backend",
                        help="Backend WebSocket URL "
                             "(default: ws://<host>:<port>/ws/audio)")
    parser.add_argument("--sample-rate", type=int, default=None,
                        help="Capture sample rate in Hz (default 16000)")
    parser.add_argument("--block-ms", type=int, default=250,
                        help="Block size in milliseconds (default 250)")
    args = parser.parse_args()

    settings = Settings()
    source = (args.source or settings.audio_source or "mic").lower()
    sample_rate = args.sample_rate or settings.sample_rate
    device_index = args.device if args.device is not None else _resolve_device(source, settings)
    backend_url = (args.backend
                  or f"ws://{settings.backend_host}:{settings.backend_port}/ws/audio")

    if device_index is None:
        print(f"[mic_stream] no input device found for source={source!r}",
              file=sys.stderr)
        return 1

    try:
        dev = sd.query_devices(device_index)
        print(f"[mic_stream] source={source} device={device_index} "
              f"name={dev.get('name')!r} rate={sample_rate}")
    except Exception as exc:  # noqa: BLE001
        print(f"[mic_stream] warning: could not describe device {device_index}: {exc}")

    try:
        return run(source, device_index, backend_url, sample_rate, args.block_ms)
    except KeyboardInterrupt:
        print("\n[mic_stream] stopping…")
        return 0


if __name__ == "__main__":
    sys.exit(main())
