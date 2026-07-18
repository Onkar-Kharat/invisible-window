"""
Centralised configuration loaded from a .env file.

All other modules import from here so there's a single place to tweak
defaults (sample rate, model names, ports, ...).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from the project root regardless of the current working dir.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _looks_like_vosk_model(path: str) -> bool:
    """
    A Vosk model directory always contains a `conf/` subdir with
    mfcc.conf, model.conf, etc. Use that as a cheap, reliable
    "is this actually a Vosk model?" check.
    """
    if not path:
        return False
    try:
        return os.path.isdir(os.path.join(path, "conf"))
    except Exception:
        return False


def _resolve_vosk_model_path(explicit: Optional[str]) -> str:
    """
    Find a usable Vosk model on disk.

    Search order:
      1. `VOSK_MODEL_PATH` from .env, if it points at a real model
      2. ./models/vosk-model-small-en-us-0.15 (recommended)
      3. ./model/vosk-model-small-en-us-0.15  (legacy)
      4. Any ./models/vosk-model-*/  (auto-detect, if only one is present)
      5. Any ./model/vosk-model-*/   (auto-detect, legacy)
      6. The env value verbatim, even if invalid (so the eventual
         error message names the exact path the user set)
    """
    default = "models/vosk-model-small-en-us-0.15"

    # 1) explicit env value
    if explicit and _looks_like_vosk_model(explicit):
        return explicit

    # 2) and 3) conventional locations
    for cand in (default, "model/vosk-model-small-en-us-0.15"):
        if _looks_like_vosk_model(cand):
            return cand

    # 4) and 5) auto-detect any vosk-model-* in models/ or model/
    for parent in ("models", "model"):
        try:
            base = PROJECT_ROOT / parent
        except Exception:
            continue
        if not base.is_dir():
            continue
        matches = sorted(
            p for p in base.iterdir()
            if p.is_dir() and p.name.startswith("vosk-model-")
            and _looks_like_vosk_model(str(p))
        )
        if len(matches) == 1:
            return str(matches[0])
        if len(matches) > 1:
            # Multiple candidates — pick the "small" one if present,
            # otherwise the first.
            for m in matches:
                if "small" in m.name.lower():
                    return str(m)
            return str(matches[0])

    # 6) Nothing matched. Return whatever was requested so the user's
    #    error message points at the path they tried.
    return explicit or default


@dataclass
class Settings:
    # --- AI routing ---
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
    ollama_timeout_sec: float = _float("OLLAMA_TIMEOUT_SEC", 8.0)

    external_provider: str = os.getenv("EXTERNAL_PROVIDER", "openai").lower()
    external_api_key: Optional[str] = os.getenv("EXTERNAL_API_KEY") or None
    external_model: str = os.getenv("EXTERNAL_MODEL", "gpt-4o-mini")
    external_timeout_sec: float = _float("EXTERNAL_TIMEOUT_SEC", 15.0)

    # --- Transcription ---
    transcription_backend: str = os.getenv("TRANSCRIPTION_BACKEND", "vosk").lower()
    # The Vosk model can live in any of these locations; we pick
    # the first one that contains a `conf/` directory. This makes
    # the project work whether the user unpacked the model into
    # `models/...` (recommended) or `model/...` (legacy) or set
    # VOSK_MODEL_PATH in .env.
    vosk_model_path: str = _resolve_vosk_model_path(
        os.getenv("VOSK_MODEL_PATH"))
    whisper_model_size: str = os.getenv("WHISPER_MODEL_SIZE", "small")
    whisper_compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

    # --- Audio source ---
    # Where to listen for the interviewer's voice.
    #   * "cable" — read CABLE Output (VB-Audio Virtual Cable). This is
    #               the right source during a Zoom / Meet / Teams call:
    #               route Zoom's microphone to "CABLE Input" and the
    #               app will hear whatever is being sent through the
    #               virtual cable (i.e. the remote participants).
    #   * "mic"   — read the physical microphone (MIC_DEVICE_INDEX).
    audio_source: str = os.getenv("AUDIO_SOURCE", "cable").lower()
    cable_device_index: int = _int("CABLE_DEVICE_INDEX", 17)
    mic_device_index: Optional[int] = (
        _int("MIC_DEVICE_INDEX", -1) if os.getenv("MIC_DEVICE_INDEX") not in (None, "") else None
    )
    # Back-compat: if the legacy DEVICE_INDEX is set and MIC_DEVICE_INDEX
    # is not, fall back to it.
    legacy_device_index: Optional[int] = (
        _int("DEVICE_INDEX", -1) if os.getenv("DEVICE_INDEX") not in (None, "") else None
    )

    # --- Audio ---
    sample_rate: int = _int("SAMPLE_RATE", 16000)
    channels: int = _int("CHANNELS", 1)
    block_size: int = _int("BLOCK_SIZE", 4000)

    # --- Streaming UX ---
    # Time of "no new partial/final" before we commit the current
    # accumulated question and send it to the LLM. ~1.2s feels
    # natural: long enough that the speaker can pause to think,
    # short enough that the answer still feels live.
    end_of_utterance_ms: int = _int("END_OF_UTTERANCE_MS", 1200)
    # Energy below this RMS is treated as silence and not fed to Vosk.
    silence_rms: float = _float("SILENCE_RMS", 0.005)
    # Pause between broadcasting the committed question and the AI
    # answer, so the user can read the question first.
    answer_delay_ms: int = _int("ANSWER_DELAY_MS", 2000)

    # --- Session & Logging ---
    session_dir: str = os.getenv("SESSION_DIR", "sessions")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    
    # --- Overlay / server ---
    backend_host: str = os.getenv("BACKEND_HOST", "127.0.0.1")
    backend_port: int = _int("BACKEND_PORT", 8765)
    overlay_opacity: float = _float("OVERLAY_OPACITY", 0.85)
    overlay_font_size: int = _int("OVERLAY_FONT_SIZE", 14)
    
    # --- Voice Activity Detection ---
    # Two flavors are supported:
    #   * "webrtc"  — webrtcvad (lightweight, CPU-cheap). Recommended.
    #   * "energy"  — RMS-threshold based VAD (current behaviour).
    #   * "none"    — pass everything through to the recognizer.
    vad_mode: str = os.getenv("VAD_MODE", "webrtc").lower()
    # 0..3 (most aggressive = 3). webrtcvad only supports 10/20/30 ms
    # frames at 8/16/32/48 kHz; the streaming layer pads/slices as
    # needed.
    vad_aggressiveness: int = _int("VAD_AGGRESSIVENESS", 2)
    # Frames below this RMS are dropped before reaching Vosk
    # (only used when vad_mode == "energy").
    vad_rms_threshold: float = _float("VAD_RMS_THRESHOLD", 0.005)
    # Pad each voiced segment with this much pre-roll so we don't
    # clip the first word.
    vad_padding_ms: int = _int("VAD_PADDING_MS", 300)

    # Back-compat alias used elsewhere in the code base.
    silence_rms: float = _float("SILENCE_RMS", 0.005)
    silence_duration_ms: int = _int("SILENCE_DURATION_MS", 700)
    vad_threshold: float = _float("VAD_THRESHOLD", 0.5)

    # --- WebSocket audio streaming ---
    # Maximum size (in bytes) of one audio frame on /ws/audio.
    ws_audio_max_frame_bytes: int = _int("WS_AUDIO_MAX_FRAME_BYTES", 16 * 1024)

    # --- Playback ---
    playback_speed: float = _float("PLAYBACK_SPEED", 1.0)


def get_settings() -> Settings:
    """Return a fresh Settings instance so tests can override env vars."""
    return Settings()
