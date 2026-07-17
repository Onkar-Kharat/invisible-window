# Local AI Desktop Assistant

A real-time local AI desktop assistant. A small, floating, always-on-top PyQt5 overlay window floats over your desktop, allowing you to select local Ollama models, type questions, and read AI-generated answers streamed chunk-by-chunk in real-time.

---

## Architecture

```
        +------------------+          ws://          +-----------------+
        | User Interaction | <------ /ws <---------- | frontend/       |
        |  (Type question) |                         | overlay.py      |
        +--------+---------+                         | (PyQt5 UI)      |
                 |                                   +-----------------+
                 v
        +--------+-----------------------------------+
        | backend/main.py  (FastAPI Server)          |
        |   /api/models   /api/ask   /ws             |
        +--------+-------------------+---------------+
                 |                   |
                 v                   v
        +--------+---------+  +------+------------+
        | transcription.py |  | ai_router.py      |
        |   (Optional)     |  |  Ollama (Stream)  |
        +------------------+  +-------------------+
```

* **Floating overlay (PyQt5)** — frameless, always-on-top, adjustable opacity / font size, draggable, auto-scrolling, hotkey-toggled (`Ctrl+Shift+H`). Includes model selection dropdown and question input.
* **Backend (FastAPI)** — exposes REST + WebSocket. Communicates with local Ollama to list models (`/api/models`) and stream responses (`/api/ask`).
* **AI router** — connects to your local Ollama instance (default: `http://localhost:11434`), lists available models, and streams responses chunk-by-chunk.

---

## Project layout

```
local_ai_desktop_assistant/
├── frontend/
│   └── overlay.py            # PyQt5 utility window + WebSocket client
├── backend/
│   ├── main.py               # FastAPI app, WS endpoints
│   ├── transcription.py      # Transcriber helpers (optional)
│   └── ai_router.py          # Ollama tag listing and streaming logic
├── config/
│   └── settings.py           # Settings and configuration
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

Tested on Windows 11 with Python 3.10+.

```powershell
# 1. Activate your virtual environment
.\.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Ollama and pull a model
winget install Ollama.Ollama        # or download from https://ollama.com
ollama pull qwen3.5:4b

# 4. Configure settings (.env file)
copy .env.example .env
```

---

## Running

You need **two processes** (or two terminals) running:

```powershell
# Terminal 1 — backend server
python -m backend.main

# Terminal 2 — floating overlay UI
python -m frontend.overlay
```
# Run the This code 
python main.py



---

## Hotkeys & controls

| Action            | Shortcut               |
|-------------------|------------------------|
| Show / hide       | `Ctrl + Shift + H`     |
| Model Selection   | Dropdown in toolbar    |
| Ask Question      | Type text + Press Enter or click Ask |
| Larger text       | `A+` button (toolbar)  |
| Smaller text      | `A-` button            |
| More opaque       | `◑` button             |
| Less opaque       | `◐` button             |
| Clear answers     | `Clear` button         |
| Move              | drag the top strip     |
| Hide on double-click | double-click top strip |

---

# Run the This code 
python main.py



## License & disclaimer

This project is provided for personal and educational use.


