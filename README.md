# Invisible Interview Assistant

A real-time, local, AI-powered desktop overlay for live interviews. A
small, floating, always-on-top PyQt5 overlay window floats over your
desktop. The mic button turns on live audio capture → VAD → speech
transcription (Vosk) → the transcript is auto-pasted into the
overlay's question input box, then streamed to your local Ollama model
for an answer that appears live.

> **Anti-screen-share:** the overlay is hidden from Zoom / Meet /
> Teams screen-capture APIs on Windows 10 1903+ via
> `WDA_EXCLUDEFROMCAPTURE`. See [Security & privacy](#security--privacy)
> for the real limits of this approach.

---

## ⚠️ Ethics, legality, and the intended use case

This project is a **technical demonstration of a local, real-time
AI pipeline** (microphone → VAD → on-device speech recognition →
local LLM → desktop overlay). The interesting parts are:

- building a low-latency producer/consumer audio pipeline that never
  blocks the sounddevice callback,
- gating an ASR recognizer with a VAD so it only sees voiced frames,
- running everything locally so no audio ever leaves the machine,
- rendering a streaming LLM response inside a frameless, draggable,
  resizeable, semi-transparent PyQt5 overlay.

It is **not** a product to use during a real interview. Doing so is
fraud in most jurisdictions and is grounds for termination at most
companies. The screen-capture hiding is a Windows API affordance, not
a license to deceive. Use it to learn about the pipeline, not to
cheat people who trusted you enough to interview you.

---

## What's in this build

| Feature                                  | Where                                                |
|------------------------------------------|------------------------------------------------------|
| Audio capture button                     | overlay toolbar → `🎙 Mic: Off/On`                   |
| Auto-paste transcribed question          | overlay input box (`question` event over `/ws`)      |
| VAD-gated streaming transcription        | `backend/transcription.py` (WebRTC VAD + Vosk)       |
| `/ws/audio` low-latency endpoint         | `backend/main.py`                                    |
| Start/stop mic REST endpoints            | `POST /api/capture/start` & `/api/capture/stop`      |
| Standalone mic client                    | `python mic_stream.py`                               |
| Auto-detect Vosk model on disk           | `config/settings.py`, `setup_audio.py`               |
| Hide overlay from screen capture         | `frontend/overlay.py` (`WDA_EXCLUDEFROMCAPTURE`)     |
| Global hotkey to show/hide               | `Ctrl + Shift + H`                                   |

---

## Architecture

```
+--------------------+        ws://         +-------------------------+
|  Zoom / Meet / ... | -- interviewer's -->|  sounddevice (mic or    |
|  interviewer voice |        voice         |  VB-Audio CABLE Output) |
+--------------------+                      +-----------+-------------+
                                                        |
                                                        v
                                +-----------------------+-----------------------+
                                |                                             |
                                |  StreamingTranscriber                    |
                                |   1. WebRTC VAD (gated frames)           |
                                |   2. Vosk recognizer                     |
                                |      - partials                          |
                                |      - finals                            |
                                |      - 1.2s idle  -> committed question   |
                                +-----------------------+-----------------------+
                                                        |
                                  ws:// /ws        /ws  |  /ws/audio
                                                        v
                                +-----------------------+-----------------------+
                                |  backend/main.py (FastAPI)                |
                                |   /api/models   /api/ask   /ws            |
                                |   /api/transcribe                        |
                                |   /api/capture/{start,stop}              |
                                |   /ws/audio   /api/health                 |
                                +-----------------------+-------------------+
                                                        |
                                                        v
                                +-----------------------+-------------------+
                                |  Overlay (PyQt5)                        |
                                |   - question input box (auto-pasted)     |
                                |   - 🎙 Mic toggle button                |
                                |   - always-on-top, drag, resize         |
                                |   - Ctrl+Shift+H to show/hide           |
                                |   - hidden from screen capture          |
                                +-----------------------------------------+
```

---

## Project layout

The project root is the directory containing this README. There is a
separate, older `invisible_interview_main/` folder at the root that
is **not** part of the application — it's a leftover from an earlier
setup and can be ignored / deleted.

```
<project root>/
├── frontend/
│   └── overlay.py             # PyQt5 UI (mic button + question input)
├── backend/
│   ├── main.py                # FastAPI: WS, /ws/audio, /api/capture/*
│   ├── transcription.py       # Vosk + WebRTC VAD streaming
│   ├── mic_stream.py          # In-process server-side mic capture
│   ├── ai_router.py           # Ollama (and external) answer routing
│   └── logger.py              # Structured logger (rotating file)
├── config/
│   └── settings.py            # Env-driven Settings dataclass
├── mic_stream.py              # Standalone mic client (streams to /ws/audio)
├── setup_audio.py             # One-shot Vosk-model helper
├── main.py                    # Python launcher: spawns both processes
├── run_all.bat                # CMD launcher (opens two titled windows)
├── run_all.ps1                # PowerShell launcher (same as .bat)
├── run_all.sh                 # Git Bash / WSL / POSIX launcher
├── requirements.txt
├── .env.example
└── README.md
```

> ℹ️ **About `main.py` (the file at the project root):** this is the
> plain-Python launcher. It starts both `python -m backend.main` and
> `python -m frontend.overlay` as child processes, line-buffers their
> stdout under `[backend]` and `[overlay]` prefixes, and tears them
> down on Ctrl+C. If you prefer a shell-based launcher, use
> `run_all.bat` (CMD), `run_all.ps1` (PowerShell), or `run_all.sh`
> (Git Bash / WSL). See [Running](#running).

> ⚠️ **About the `invisible_interview_main/` directory** (if you see
> one in the tree): it is a leftover from an earlier setup, **not**
> part of this application. It contains a venv skeleton and can be
> safely deleted.

---

## Setup (Windows 11, Python 3.10+)

The codebase uses standard 3.9+ type-hint syntax (`set[X]`, `list[X]`,
PEP 604 unions) and runs on Python 3.10 and newer. If you only have
Python 3.12+ available, that is also fine.

```powershell
# 1. cd into the project
cd invisible-window-main

# 2. Create + activate a virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Ollama and pull a model
winget install Ollama.Ollama        # or download from https://ollama.com
ollama pull qwen3.5:4b

# 5. Configure settings
copy .env.example .env

# 6. Download a Vosk model and let the helper wire it into .env
python setup_audio.py
```

`setup_audio.py` will:
- find any Vosk model you've already unpacked into `models/` or `model/`,
- report its size,
- auto-write `VOSK_MODEL_PATH=...` to your `.env` if you haven't set
  one, and
- tell you exactly what to download if nothing is installed.

If you'd rather do it by hand, grab the small English model
(~50 MB) from <https://alphacephei.com/vosk/models> and unpack it so
this exists:

```
models/vosk-model-small-en-us-0.15/conf/
models/vosk-model-small-en-us-0.15/graph/
models/vosk-model-small-en-us-0.15/am/final.mdl
```

### Optional: install VB-Audio Virtual Cable

To capture the interviewer's voice (rather than your own mic), install
[VB-Audio Virtual Cable](https://vb-audio.com/Cable/) and set your
meeting app's microphone to **CABLE Input**. Then set in `.env`:

```ini
AUDIO_SOURCE=cable
CABLE_DEVICE_INDEX=17    # or whichever index your system shows
```

You can list indices with `python -m sounddevice`.

---

## Running

The application needs two processes running at the same time: the
FastAPI backend and the PyQt5 overlay. There are four ways to launch
both, depending on which shell you prefer.

### Pick one

| Shell | Command | What you get |
|---|---|---|
| **CMD (cmd.exe)** | `run_all.bat` | Two titled console windows pop up; close them or press any key in the launcher to stop |
| **PowerShell**    | `.\run_all.ps1` | Same as CMD, but you may need `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` once if Windows blocks the script |
| **Git Bash / WSL** | `./run_all.sh` | Single terminal; both processes' output tails with `tail -F`; Ctrl+C stops both |
| **Plain Python**   | `python main.py` | Single console, both processes' stdout interleaved with `[backend]` / `[overlay]` prefixes |

All four activate `.venv` if it exists, verify dependencies, start
the backend first (so the port binds), then start the overlay. None
of them require you to manage two terminals by hand.

### What the launcher does

1. Activates `.venv` if it exists; falls back to system Python with
   a warning if not.
2. Verifies that `fastapi`, `uvicorn`, `PyQt5`, `sounddevice`,
   `vosk`, `requests`, and `websocket` are importable. If anything
   is missing it prints the exact `pip install -r requirements.txt`
   command to run and exits.
3. Starts `python -m backend.main` and waits 2 seconds for the
   FastAPI server to bind to its port.
4. Starts `python -m frontend.overlay` (the actual floating GUI).
5. On Ctrl+C / any key / window close, kills both children.

### What you actually use

The overlay window is the only thing you click on. The backend
console is just there to print logs.

### If you'd rather run them by hand

You can still run each process in its own terminal. This is useful
when you're developing and want to restart just one of them.

```powershell
# Terminal 1 — backend (handles /ws, /ws/audio, /api/capture/*)
python -m backend.main

# Terminal 2 — floating overlay UI
python -m frontend.overlay
```

```powershell
# Terminal 3 (optional) — standalone mic client
# Only needed if you want to capture mic audio from a *different*
# process than the backend.
python mic_stream.py --source cable
```


Then, in the overlay:

1. Click the **🎙 Mic: Off** button in the toolbar. It switches to
   *🎙 Mic: On* and the backend's microphone loop starts.
2. The interviewer speaks. The overlay shows live partials, the
   question is auto-pasted into the question input box, and the
   answer streams in below.
3. Click **🎙 Mic: On** again to stop capture.

---

## Hotkeys & controls

| Action                         | Shortcut / control              |
|--------------------------------|---------------------------------|
| Show / hide overlay            | `Ctrl + Shift + H`              |
| Move / resize                  | drag the top strip / bottom-right corner |
| Toggle microphone capture      | `🎙 Mic` toolbar button          |
| Ask the model                  | type in the question box + Enter |
| Larger / smaller text          | `A+` / `A-` toolbar buttons     |
| More / less opaque             | `◑` / `◐` toolbar buttons       |
| Clear answers                  | `Clear` toolbar button          |
| Hide on double-click           | double-click the top strip      |

---

## API reference

| Method | Path                       | Purpose |
|--------|----------------------------|---------|
| `WS`   | `/ws`                      | overlay ↔ backend; receives `question`, `partial`, `final`, `answer`, `audio_source`, `speech` events |
| `WS`   | `/ws/audio`                | low-latency raw-PCM (int16 mono 16 kHz) audio in, JSON transcription out |
| `POST` | `/api/capture/start`       | start the server-side microphone capture loop |
| `POST` | `/api/capture/stop`        | stop the server-side microphone capture loop |
| `GET`  | `/api/capture/state`       | is server-side capture currently running? |
| `GET`  | `/api/health`              | liveness + active backend state |
| `POST` | `/api/ask`                 | stream an answer to the overlay over `/ws` |
| `POST` | `/api/transcribe`          | one-shot base64-audio → question + answer |
| `GET`  | `/api/models`              | list installed Ollama models |

### WebSocket frame format for `/ws/audio`

Client → server (binary frames):
- raw int16 little-endian mono PCM at 16 kHz; recommended block size
  is 250 ms (8 000 samples / 16 000 bytes).

Client → server (text frames, JSON):
```json
{"type": "config", "sample_rate": 16000, "channels": 1}
{"type": "stop"}
```

Server → client (text frames, JSON):
```json
{"type": "ready"}
{"type": "partial",  "text": "tell me about"}
{"type": "final",    "text": "tell me about your experience"}
{"type": "question", "text": "tell me about your experience"}
{"type": "error",    "message": "..."}
```

---

## Demonstration workflow

1. Open your meeting app (Zoom, Google Meet, MS Teams). Set the
   microphone to **CABLE Input** (or your physical mic if you
   prefer).
2. Start the backend: `python -m backend.main`
3. Start the overlay:  `python -m frontend.overlay`
4. Click the **🎙 Mic: Off** button in the overlay toolbar.
5. The interviewer speaks in the meeting.
6. Audio flows: VB-Cable → backend mic loop → WebRTC VAD → Vosk
   recognizer → `question` event → overlay WebSocket client.
7. The transcribed question **auto-pastes** into the question input
   box.
8. The local Ollama model generates the answer; tokens stream over
   the same WebSocket and render live in the overlay.
9. Click **🎙 Mic: On** again to stop capture.

---

## Configuration

Everything is driven by `.env` — see `.env.example` for the full list
of keys. The most important ones:

| Key | Default | Purpose |
|---|---|---|
| `AUDIO_SOURCE` | `cable` | `cable` for VB-Cable Output, `mic` for the physical mic |
| `CABLE_DEVICE_INDEX` | `17` | sounddevice index of CABLE Output |
| `MIC_DEVICE_INDEX` | _unset_ | sounddevice index of the physical mic |
| `TRANSCRIPTION_BACKEND` | `vosk` | `vosk` (streaming) or `faster_whisper` (one-shot) |
| `VOSK_MODEL_PATH` | `models/vosk-model-small-en-us-0.15` | auto-detected by `setup_audio.py` |
| `VAD_MODE` | `webrtc` | `webrtc`, `energy`, or `none` |
| `VAD_AGGRESSIVENESS` | `2` | 0–3, only for WebRTC VAD |
| `VAD_PADDING_MS` | `300` | pre-roll audio to keep the first phoneme |
| `END_OF_UTTERANCE_MS` | `1200` | silence before committing a question |
| `ANSWER_DELAY_MS` | `2000` | pause between the question and the LLM answer |
| `OLLAMA_MODEL` | `qwen3.5:4b` | any locally-pulled Ollama model |
| `BACKEND_HOST` | `127.0.0.1` | bind address; do **not** set to `0.0.0.0` casually |
| `BACKEND_PORT` | `8765` | FastAPI port |
| `OVERLAY_OPACITY` | `0.85` | initial window opacity (0.2–1.0) |

---

## Security & privacy

The system runs entirely on the local machine. The default
`BACKEND_HOST=127.0.0.1` means the FastAPI server is only reachable
from this device, which is what you want for personal use.

### What the screen-capture flag actually does

`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` is a
Windows 10 1903+ API that tells the Desktop Window Manager to
exclude the window from DWM-level screen captures. The fallback
flag is `WDA_MONITOR` (older Win 10).

It does **not** make the window invisible to:

- a human looking at your screen,
- the taskbar / Alt-Tab / Mission-Control-style overlays,
- a recording application that uses GDI or PrintWindow directly on
  your desktop (instead of DWM mirroring),
- meetings whose apps run at a higher integrity level than your
  user (rare, but possible on locked-down corporate machines),
- the cursor trail — if you keep moving the mouse to the overlay,
  anyone screen-recording can see *you* doing it.

Treat the flag as a "the interviewer won't notice the window in
their view of your shared screen" affordance, not as
plausible-deniable anti-detection. It is the right tool for
keeping a private window out of a presentation; it is the wrong
tool for hiding behaviour from a person.

### Network exposure

If you set `BACKEND_HOST=0.0.0.0` (for example, to stream mic
audio from a phone or a different machine), the following endpoints
become reachable from your LAN:

- `GET  /api/models`             (no auth)
- `GET  /api/health`             (no auth)
- `GET  /api/capture/state`      (no auth)
- `POST /api/capture/start`      (no auth)
- `POST /api/capture/stop`       (no auth)
- `POST /api/ask`                (no auth, will hit your local LLM)
- `WS   /ws`                     (no auth, streams your session)
- `WS   /ws/audio`               (no auth, accepts raw audio)

CORS is currently `allow_origins=["*"]`, so any browser on the
network could talk to it. **Only bind to `0.0.0.0` on a trusted
LAN, behind a firewall, and consider putting it behind a reverse
proxy with auth if you do.**

### Secrets

`.env` is in `.gitignore`. Don't remove that line. The
`EXTERNAL_API_KEY` field in `.env` is the only secret this project
ever holds; if you've ever committed a real key, rotate it.

### Logging

The structured logger in `backend/logger.py` writes to
`logs/app.log` (rotating, 10 MB × 5). The rest of the backend
currently uses `print()` for status, so the file log is mostly
empty in the default build. Don't rely on it for transcripts of
your session — they are not persisted anywhere by default.

---

## Known issues

These are real issues in the code, deliberately left in place
because the code works as-is and the project is a learning
artefact. They are listed here so they don't surprise you.

1. **`backend/logger.py` is largely dead code.** It's a
   well-formed rotating-file logger, but the backend uses
   `print()` everywhere instead of calling it. Logs are not
   actually written to `logs/app.log` in the current build.

2. **`faster-whisper` is in `requirements.txt` but unused.** The
   streaming pipeline only ever loads Vosk; the `faster-whisper`
   one-shot path is reachable only through `POST /api/transcribe`
   with a base64 audio blob. If you don't use that endpoint you
   can comment it out to save ~500 MB of disk + first-import time.

3. **`invisible_interview_main/` directory at the project root
   is leftover from an earlier setup** (some clones may have it).
   It contains a venv skeleton and is not part of the application.
   Safe to delete.

---

## Troubleshooting

- **No transcription in the overlay** — verify the backend log
  says `[mic] device opened: ...` and `[mic] listening on: ...`.
  If the device is wrong, set `CABLE_DEVICE_INDEX` /
  `MIC_DEVICE_INDEX` in `.env`.
- **VOSK model not found** — run `python setup_audio.py`. It
  will either confirm the model is in place or tell you exactly
  where to put it.
- **webrtcvad not installed** — `pip install webrtcvad`, or set
  `VAD_MODE=energy` in `.env`.
- **No Ollama model** — run `ollama pull qwen3.5:4b` and restart
  the backend. The overlay's model dropdown will show
  `No local models found — is Ollama running?` if Ollama isn't
  reachable on `OLLAMA_BASE_URL`.
- **Overlay invisible** — press `Ctrl+Shift+H` to toggle. If it
  never appears, check the console for `RegisterHotKey failed`;
  another app may already own that combination.
- **`.ps1` opens in Notepad** — don't double-click it. Open
  PowerShell, `cd` to the project root, and type `.\run_all.ps1`
  instead. If PowerShell blocks the script, run
  `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`
  once and try again. Or use `run_all.bat` from cmd.exe, which
  has no such restriction.
- **Launcher says "no .venv found"** — you skipped first-time
  setup. From the project root, run:
  ```
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt
  ```
- **Launcher says "missing Python dependencies"** — same fix as
  above, then re-run the launcher.

---

## License & disclaimer

For personal and educational use. Do not use this to cheat in ways
that violate your interviewer's, employer's, or institution's
policies. The authors of this project accept no responsibility for
misuse.
