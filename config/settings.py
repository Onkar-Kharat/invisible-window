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
    vosk_model_path: str = os.getenv("VOSK_MODEL_PATH", "models/vosk-model-small-en-us-0.15")
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

    # --- Overlay / server ---
    backend_host: str = os.getenv("BACKEND_HOST", "127.0.0.1")
    backend_port: int = _int("BACKEND_PORT", 8765)
    overlay_opacity: float = _float("OVERLAY_OPACITY", 0.85)
    overlay_font_size: int = _int("OVERLAY_FONT_SIZE", 14)


def get_settings() -> Settings:
    """Return a fresh Settings instance so tests can override env vars."""
    return Settings()
