# Invisible Interview Assistant

A real-time invisible local AI desktop assistant for live interviews. A
small, floating, always-on-top PyQt5 overlay window floats over your
desktop. The mic button turns on live audio capture → VAD → speech
transcription (Vosk) → the transcript is auto-pasted into the overlay's
question input box, then streamed to your local Ollama model for an
answer that appears live.

> **Anti-screen-share:** the overlay is hidden from Zoom / Meet / Teams
> screen-capture APIs on Windows 10 1903+ via `WDA_EXCLUDEFROMCAPTURE`.

---

## What's new in this update

| Feature                          | Where                                                |
|----------------------------------|------------------------------------------------------|
| Audio capture button             | overlay toolbar → `🎙 Mic: Off/On`                   |
| Auto-paste transcribed question  | overlay input box (`question` event over `/ws`)      |
| VAD-gated streaming transcription| `backend/transcription.py` (WebRTC VAD + Vosk)       |
| `/ws/audio` low-latency endpoint | `backend/main.py`                                    |
| Start/stop mic REST endpoints    | `POST /api/capture/start` & `/api/capture/stop`      |
| Standalone mic client            | `python mic_stream.py`                               |

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

```
invisible_interview_main/
├── frontend/
│   └── overlay.py             # PyQt5 UI (mic button + question input)
├── backend/
│   ├── main.py                # FastAPI: WS, /ws/audio, /api/capture/*
│   ├── transcription.py       # Vosk + WebRTC VAD streaming
│   ├── mic_stream.py          # In-process server-side mic capture
│   ├── ai_router.py           # Ollama (and external) answer routing
│   └── logger.py
├── config/
│   └── settings.py
├── mic_stream.py              # Standalone mic client (streams to /ws/audio)
├── main.py                    # (legacy launcher; not required to start)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup (Windows 11, Python 3.10+)

```powershell
# 1. Clone / cd into the project
cd invisible_interview_main

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

# 6. Download a Vosk model (only needed if TRANSCRIPTION_BACKEND=vosk)
#    The small English model is ~50 MB:
#      https://alphacephei.com/vosk/models
#    Unpack into ./models/ so the path matches .env:
#      models/vosk-model-small-en-us-0.15/
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

You need **two** processes (and optionally a third for the standalone
mic client).

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

## API reference (new endpoints)

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

## Troubleshooting

- **No transcription in the overlay** — verify the backend log says
  `[mic] device opened: ...` and `[mic] listening on: ...`. If the
  device is wrong, set `CABLE_DEVICE_INDEX` / `MIC_DEVICE_INDEX` in
  `.env`.
- **VOSK model not found** — unpack the model into `models/` so the
  directory `models/vosk-model-small-en-us-0.15/` exists, or set
  `VOSK_MODEL_PATH` in `.env`.
- **webrtcvad not installed** — `pip install webrtcvad`, or set
  `VAD_MODE=energy` in `.env`.
- **No Ollama model** — run `ollama pull qwen3.5:4b` and restart the
  backend.

---

## License & disclaimer

For personal and educational use. Don't use this to cheat in ways
that violate your interviewer's or employer's policies.
