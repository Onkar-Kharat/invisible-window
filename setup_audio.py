"""
setup_audio.py — one-shot setup helper for audio capture.

Walks the project, finds any existing Vosk model on disk
(under either ./models/ or ./model/), and either:

  * prints a one-line report of what it found, or
  * prints actionable next steps if nothing is installed.

Usage:
    python setup_audio.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _is_vosk_model(path: Path) -> bool:
    return path.is_dir() and (path / "conf").is_dir()


def _find_models() -> list[Path]:
    found: list[Path] = []
    for parent in (PROJECT_ROOT / "models", PROJECT_ROOT / "model"):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and child.name.startswith("vosk-model-"):
                if _is_vosk_model(child):
                    found.append(child)
    return found


def main() -> int:
    print("=" * 60)
    print("Invisible Interview Assistant — audio setup")
    print("=" * 60)
    print(f"Project root: {PROJECT_ROOT}")
    print()

    found = _find_models()
    if found:
        print(f"✅ Found {len(found)} Vosk model(s) on disk:")
        for p in found:
            size_mb = sum(f.stat().st_size for f in p.rglob('*')
                          if f.is_file()) / (1024 * 1024)
            print(f"   • {p.relative_to(PROJECT_ROOT)}  ({size_mb:.0f} MB)")
        # Update .env to point at the first one, unless the user
        # already set VOSK_MODEL_PATH and it's valid.
        env_path = PROJECT_ROOT / ".env"
        env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        has_valid_env = any(
            line.strip().startswith("VOSK_MODEL_PATH=")
            and _is_vosk_model(
                PROJECT_ROOT / line.split("=", 1)[1].strip().strip('"').strip("'")
            )
            for line in env_text.splitlines()
        )
        if not has_valid_env:
            chosen = found[0]
            rel = chosen.relative_to(PROJECT_ROOT).as_posix()
            if env_path.exists():
                if "VOSK_MODEL_PATH" in env_text:
                    # Replace existing
                    lines = []
                    for line in env_text.splitlines():
                        if line.strip().startswith("VOSK_MODEL_PATH="):
                            lines.append(f"VOSK_MODEL_PATH={rel}")
                        else:
                            lines.append(line)
                    env_path.write_text(
                        "\n".join(lines) + "\n", encoding="utf-8"
                    )
                else:
                    with env_path.open("a", encoding="utf-8") as f:
                        f.write(f"\nVOSK_MODEL_PATH={rel}\n")
            else:
                env_path.write_text(f"VOSK_MODEL_PATH={rel}\n", encoding="utf-8")
            print(f"\n   ↳ wrote VOSK_MODEL_PATH={rel} to .env")
        print()
        print("Next:")
        print("  1. python -m backend.main       # start the backend")
        print("  2. python -m frontend.overlay   # start the overlay")
        print("  3. Click 🎙 Mic: Off in the overlay toolbar")
        print("  4. In your meeting app, set mic to CABLE Input")
        print("  5. Speak → text auto-pastes into the question box")
        return 0

    print("❌ No Vosk model found on disk.")
    print()
    print("To install one:")
    print("  1. Download the small English model (~50 MB):")
    print("       https://alphacephei.com/vosk/models")
    print("     (Click 'vosk-model-small-en-us-0.15' to download)")
    print()
    print("  2. Unpack it into the project so this exists:")
    print("       models/vosk-model-small-en-us-0.15/conf/")
    print("       models/vosk-model-small-en-us-0.15/graph/")
    print("       models/vosk-model-small-en-us-0.15/am/final.mdl")
    print()
    print("  Or in PowerShell:")
    print('     Invoke-WebRequest "https://alphacephei.com/vosk/models/'
          'vosk-model-small-en-us-0.15.zip" -OutFile "models.zip"')
    print('     Expand-Archive models.zip -DestinationPath . -Force')
    print('     Rename-Item vosk-model-small-en-us-0.15 models\\'
          'vosk-model-small-en-us-0.15')
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
