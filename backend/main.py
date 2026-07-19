"""
FastAPI app that ties everything together.

  * POST /api/transcribe       – audio blob in, transcript + answer out
  * POST /api/ask              – stream answer over WS
  * WS   /ws                   – live mic state + answer push
  * WS   /ws/audio             – raw PCM audio in, transcription out
  * POST /api/capture/start    – start server-side mic capture
  * POST /api/capture/stop     – stop server-side mic capture
  * GET  /api/health           – liveness probe
  * GET  /api/capture/state    – is capture currently on?
"""
from __future__ import annotations

import asyncio
import base64
import json
import queue as _queue
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.ai_router import generate_answer, list_ollama_models, ollama_stream_generate
from backend.transcription import Transcriber, StreamingTranscriber
from config.settings import Settings
from backend.mic_stream import StreamingCapture


settings = Settings()
transcriber: Optional[Transcriber] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the (heavy) transcription model once on startup if available."""
    global transcriber
    print(f"[backend] loading transcription model: {settings.transcription_backend}")
    try:
        transcriber = Transcriber(settings)
        print("[backend] ready.")
    except FileNotFoundError as e:
        # The Vosk model directory is missing — the most common
        # reason "the mic captures but nothing appears". Print an
        # actionable message rather than a bare traceback.
        print("[backend] ============================================")
        print(f"[backend] WARNING: {e}")
        print(f"[backend]   -> Download a model from "
              f"https://alphacephei.com/vosk/models")
        print(f"[backend]   -> Unpack into the project root so that")
        print(f"[backend]      {settings.vosk_model_path}/conf/")
        print(f"[backend]      {settings.vosk_model_path}/graph/")
        print(f"[backend]      {settings.vosk_model_path}/am/final.mdl  exist.")
        print(f"[backend]   -> Or set VOSK_MODEL_PATH in .env to the correct path.")
        print(f"[backend]   -> Until then, /api/transcribe is disabled, and")
        print(f"[backend]      the 🎙 Mic button in the overlay will not produce")
        print(f"[backend]      any transcription (audio still flows, but Vosk")
        print(f"[backend]      can't decode it).")
        print("[backend] ============================================")
    except Exception as e:
        print(f"[backend] WARNING: Transcription model could not be loaded ({e.__class__.__name__}: {e}).")
        print("[backend] Transcription API (/api/transcribe) will be disabled, but assistant will work.")
    yield
    # Nothing to tear down explicitly.


app = FastAPI(title="Invisible Interview Assistant", lifespan=lifespan)
# CORS: by default the backend is bound to 127.0.0.1 (see Settings),
# so any browser on the same machine is same-origin and CORS does
# not apply. If a user explicitly sets BACKEND_HOST=0.0.0.0 to run
# the overlay on a different machine, only the loopback and LAN
# origins they actually trust should be able to talk to it. The
# default policy below allows only loopback; if you need LAN
# access, set BACKEND_CORS_ALLOW_ORIGINS in .env to a comma-separated
# list of allowed origins, e.g. "http://192.168.1.42:8765".
import os as _os
_cors_env = _os.getenv("BACKEND_CORS_ALLOW_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    # Safe default: only loopback. Anyone binding to 0.0.0.0 has
    # to opt in to wider CORS explicitly.
    _cors_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Pydantic models ----------------------------------------------

class TranscribeRequest(BaseModel):
    audio_b64: Optional[str] = None  # raw 16-bit PCM, base64-encoded
    text: Optional[str] = None       # for manual text input (testing)
    context: Optional[str] = None


class TranscribeResponse(BaseModel):
    question: str
    answer: str
    source: str


class AskRequest(BaseModel):
    question: str
    model: Optional[str] = None


class CaptureStartRequest(BaseModel):
    source: Optional[str] = None   # "mic" | "cable" (defaults to settings)
    device_index: Optional[int] = None


# ---- REST ---------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "transcription": settings.transcription_backend,
        "ollama_model": settings.ollama_model,
        "capture_running": capture_state.is_running,
    }


@app.get("/api/models")
async def api_models():
    """Returns installed Ollama models."""
    return list_ollama_models(settings)


@app.post("/api/ask")
async def api_ask(req: AskRequest):
    """Streams answer tokens over the WebSocket connection."""
    from backend.ai_router import _build_prompt
    question = req.question.strip()
    if not question:
        return {"status": "error", "message": "Empty question"}

    await ws_manager.broadcast({"type": "stream_start", "question": question})

    prompt = _build_prompt(question, context=None)
    loop = asyncio.get_running_loop()

    def run_generator():
        try:
            yield from ollama_stream_generate(settings, prompt, model=req.model)
        except Exception as e:
            print(f"[backend] Ollama stream error: {e}")
            yield f"\n\n(Ollama stream error: {e})"

    iterator = run_generator()
    while True:
        token = await loop.run_in_executor(None, next, iterator, None)
        if token is None:
            break
        await ws_manager.broadcast({"type": "token", "text": token})

    await ws_manager.broadcast({"type": "stream_end", "source": "ollama"})
    return {"status": "ok"}


@app.post("/api/transcribe", response_model=TranscribeResponse)
async def api_transcribe(req: TranscribeRequest) -> TranscribeResponse:
    """
    Accept a base64-encoded 16-bit PCM chunk (or pre-typed text for
    testing) and return the question + AI answer.
    """
    if req.text:
        question = req.text.strip()
    elif req.audio_b64:
        if transcriber is None:
            return TranscribeResponse(
                question="",
                answer="(Transcription model not loaded/available)",
                source="none"
            )
        pcm = base64.b64decode(req.audio_b64)
        question = (await transcriber.transcribe_pcm(pcm)).strip()
    else:
        return TranscribeResponse(question="", answer="(no input)", source="none")

    if not question:
        return TranscribeResponse(question="", answer="(no speech detected)", source="none")

    answer, source = await generate_answer(question, context=req.context, settings=settings)
    return TranscribeResponse(question=question, answer=answer, source=source)


# ---- WebSocket ----------------------------------------------------

class WSManager:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        await ws.send_text(json.dumps({"type": "ready"}))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = WSManager()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """
    Overlay clients connect here to receive answer payloads.
    The microphone loop also publishes status updates on this channel.
    """
    await ws_manager.connect(ws)
    try:
        while True:
            # We don't expect the overlay to send data, but read so we
            # notice disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# ---- /ws/audio  (raw PCM up, transcription JSON down) -------------

@app.websocket("/ws/audio")
async def ws_audio_endpoint(ws: WebSocket) -> None:
    """
    Bidirectional WebSocket for low-latency audio transcription.

    Wire format (text frames, JSON):
        client -> server:
            {"type": "config", "sample_rate": 16000, "channels": 1}   (optional)
            {"type": "stop"}

        server -> client:
            {"type": "ready"}
            {"type": "partial",  "text": "..."}
            {"type": "final",    "text": "..."}
            {"type": "question", "text": "..."}      (committed question)
            {"type": "error",    "message": "..."}
    """
    await ws.accept()
    await ws.send_text(json.dumps({"type": "ready"}))

    if settings.transcription_backend != "vosk":
        await ws.send_text(json.dumps({
            "type": "error",
            "message": (
                "Streaming transcription requires TRANSCRIPTION_BACKEND=vosk. "
                f"Got '{settings.transcription_backend}'."
            ),
        }))
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    # Coalesce partials: we only ever care about the latest one. The
    # producer (Vosk) fires a partial on essentially every audio frame
    # — far faster than the WS sender can drain a small queue — so a
    # bounded queue of every partial would always be full. Instead we
    # hold the latest partial in a single-slot cell, and keep a
    # small thread-safe queue for finals (which the bridge task below
    # moves into the asyncio world).
    latest_partial: dict = {"text": ""}
    finals_q: _queue.Queue = _queue.Queue(maxsize=64)
    finals_bridge: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
    stopped = asyncio.Event()
    partial_lock = threading.Lock()

    def on_partial(text: str) -> None:
        # Called from the audio thread (inside the run_in_executor).
        # Cheap, lock-protected, never blocks.
        with partial_lock:
            latest_partial["text"] = text

    def on_final(text: str) -> None:
        # Push onto a thread-safe queue. If it fills (network
        # wedged), drop the oldest. We never raise back into the
        # audio thread.
        payload = {"type": "final", "text": text}
        try:
            finals_q.put_nowait(payload)
        except _queue.Full:
            try:
                finals_q.get_nowait()
            except Exception:
                pass
            try:
                finals_q.put_nowait(payload)
            except Exception:
                pass

    async def _finals_bridge() -> None:
        """Move finals from the thread-safe queue to the asyncio queue."""
        while not stopped.is_set():
            try:
                payload = finals_q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.02)
                continue
            try:
                finals_bridge.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    finals_bridge.get_nowait()
                except Exception:
                    pass
                try:
                    finals_bridge.put_nowait(payload)
                except Exception:
                    pass

    try:
        streaming = StreamingTranscriber(settings, on_partial=on_partial,
                                        on_final=on_final)
    except Exception as exc:  # noqa: BLE001
        await ws.send_text(json.dumps({"type": "error",
                                        "message": f"{exc}"}))
        await ws.close()
        return

    question_buffer: list[str] = []
    last_activity = loop.time()
    end_of_utt_sec = settings.end_of_utterance_ms / 1000.0
    pending_commit: Optional[asyncio.Task] = None

    def schedule_commit_check() -> None:
        nonlocal pending_commit
        if pending_commit is not None and not pending_commit.done():
            pending_commit.cancel()
        pending_commit = asyncio.create_task(_commit_check())

    async def _commit_check() -> None:
        await asyncio.sleep(end_of_utt_sec)
        if question_buffer:
            question = " ".join(question_buffer).strip()
            question_buffer.clear()
            streaming.reset()
            try:
                await ws.send_text(json.dumps({"type": "question",
                                                "text": question}))
            except Exception:
                return
            # Also push to the overlay WS so it auto-pastes the
            # committed question into the input box.
            await ws_manager.broadcast({"type": "question", "text": question})

    sender_task = asyncio.create_task(
        _audio_sender(ws, finals_bridge, latest_partial, partial_lock,
                      stopped, loop)
    )
    bridge_task = asyncio.create_task(_finals_bridge())

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                pcm = msg["bytes"]
                await loop.run_in_executor(None, streaming.feed, pcm)
                last_activity = loop.time()
                # Arm the end-of-utterance timer on every chunk so
                # the question commits shortly after speech stops.
                schedule_commit_check()
            elif "text" in msg and msg["text"] is not None:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "stop":
                    break
    except WebSocketDisconnect:
        pass
    finally:
        stopped.set()
        for t in (sender_task, bridge_task):
            t.cancel()
        for t in (sender_task, bridge_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if pending_commit is not None and not pending_commit.done():
                pending_commit.cancel()
        except Exception:
            pass


async def _audio_sender(ws: WebSocket,
                       finals_queue: asyncio.Queue,
                       latest_partial: dict,
                       partial_lock: threading.Lock,
                       stopped: asyncio.Event,
                       loop: asyncio.AbstractEventLoop) -> None:
    """
    Drain recognizer output and forward to the client.

    Partials are coalesced: the producer overwrites the single
    `latest_partial` cell from the audio thread, and we read it here
    roughly every 60 ms. This keeps the wire payload small and means
    a slow network never back-pressures the audio path.
    """
    last_sent_partial = ""
    try:
        while not stopped.is_set():
            # 1) Drain any final segments first (they're rare but
            #    must arrive in order).
            while True:
                try:
                    evt = finals_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await ws.send_text(json.dumps(evt))
                except Exception:
                    return

            # 2) Send the latest partial, if it changed.
            with partial_lock:
                current = latest_partial.get("text", "")
            if current and current != last_sent_partial:
                last_sent_partial = current
                try:
                    await ws.send_text(json.dumps(
                        {"type": "partial", "text": current}))
                except Exception:
                    return

            await asyncio.sleep(0.06)
    except asyncio.CancelledError:
        return


# ---- Server-side mic capture (toggle from the audio button) -------

class CaptureState:
    """Tracks whether a server-side mic loop is currently running."""

    def __init__(self) -> None:
        self.is_running: bool = False
        self.task: Optional[asyncio.Task] = None
        self.device: Optional[str] = None
        self.lock = asyncio.Lock()


capture_state = CaptureState()


@app.post("/api/capture/start")
async def api_capture_start(req: Optional[CaptureStartRequest] = None) -> dict:
    """
    Starts the server-side microphone capture loop. The loop streams
    audio into Vosk via StreamingTranscriber and broadcasts the
    transcribed questions over the existing /ws channel (so the
    overlay's input box auto-fills).

    Idempotent: calling this twice is a no-op.
    """
    if capture_state.is_running:
        return {"status": "ok", "already_running": True,
                "device": capture_state.device}

    async with capture_state.lock:
        if capture_state.is_running:
            return {"status": "ok", "already_running": True,
                    "device": capture_state.device}
        capture_state.task = asyncio.create_task(microphone_loop())
        return {"status": "ok", "started": True}


@app.post("/api/capture/stop")
async def api_capture_stop() -> dict:
    """Stops the server-side microphone capture loop, if running."""
    async with capture_state.lock:
        if not capture_state.is_running:
            return {"status": "ok", "already_stopped": True}
        if capture_state.task is not None:
            capture_state.task.cancel()
            try:
                await capture_state.task
            except (asyncio.CancelledError, Exception):
                pass
            capture_state.task = None
        capture_state.is_running = False
        capture_state.device = None
    await ws_manager.broadcast({"type": "audio_source", "device": None})
    return {"status": "ok", "stopped": True}


@app.get("/api/capture/state")
async def api_capture_state() -> dict:
    """Returns whether the server-side mic capture is currently running."""
    return {
        "running": capture_state.is_running,
        "device": capture_state.device,
    }


# ---- Embedded streaming mic loop --------------------------------

# Queue between the producer (Vosk callbacks) and the consumer
# (websocket broadcasts + AI pipeline). The producer never blocks
# the audio thread; the consumer is single-task so message ordering
# is preserved.
_StreamEvent = dict  # {"kind": "partial"|"final"|"audio_source", "text": ...}


async def _commit_and_answer(question: str) -> None:
    """
    Called by the consumer once we have a complete question. Handles
    the 2 s 'show the question, then answer' UX:
      1. broadcast the question
      2. sleep ANSWER_DELAY_MS
      3. call generate_answer (Ollama)
      4. broadcast the answer
    """
    if not question.strip():
        return
    await ws_manager.broadcast({"type": "question", "text": question})
    print(f"[mic] question: {question}")

    delay_sec = max(0.0, settings.answer_delay_ms / 1000.0)
    if delay_sec > 0:
        await ws_manager.broadcast({
            "type": "state",
            "state": f"answer in {delay_sec:.0f}s",
        })
        await asyncio.sleep(delay_sec)

    answer, source = await generate_answer(question, settings=settings)
    await ws_manager.broadcast({
        "type": "answer",
        "question": question,
        "answer": answer,
        "source": source,
    })
    print(f"[ai] {source}: {answer[:80]}...")


async def microphone_loop() -> None:
    """
    Streaming pipeline:
      capture  ->  VAD -> Vosk (partial/final)  ->  asyncio.Queue
                                              |
                                              v
                                       consumer task
                                              |
                                              +-- partial -> broadcast
                                              +-- final   -> buffer
                                              +-- 1.2s idle -> commit + AI

    The producer never awaits (it just enqueues from the audio
    thread via Vosk's callbacks), so audio latency stays bounded.
    """
    capture_state.is_running = True
    print("[mic] loop starting", flush=True)
    try:
        # 1) Open the audio device. Resolves to CABLE Output (device 17)
        #    by default; the user just has to set Zoom's microphone to
        #    "CABLE Input" for the interviewer's voice to flow in.
        print("[mic] opening audio device...", flush=True)
        try:
            capture = StreamingCapture(settings)
        except Exception as exc:  # noqa: BLE001
            print(f"[mic] FATAL: cannot open audio device: {exc}", flush=True)
            return
        capture_state.device = capture.description
        print(f"[mic] device opened: {capture.description}", flush=True)

        # Tell the overlay what we're listening to (so the user can see
        # if VB-Cable is actually wired in correctly).
        await ws_manager.broadcast({
            "type": "audio_source",
            "device": capture.description,
        })
        print(f"[mic] listening on: {capture.description}", flush=True)

        # 2) Producer queue (thread-safe) + recogniser. Audio callbacks
        #    run on the sounddevice audio thread, so we use a plain
        #    `queue.Queue` here. If it fills, the helper drops the
        #    oldest entry — we'd rather skip a stale partial than
        #    raise back into the audio thread (which would log a
        #    "QueueFull" exception and stop the mic loop).
        event_q: _queue.Queue = _queue.Queue(maxsize=1024)
        loop = asyncio.get_running_loop()
        capture.start(loop)

        def _safe_put(payload: _StreamEvent) -> None:
            try:
                event_q.put_nowait(payload)
            except _queue.Full:
                # Drop the oldest and retry. This is safe because we
                # only care about the latest partial + recent finals.
                # Count + warn so a sustained overflow is visible.
                nonlocal _dropped_events, _last_drop_warn_at
                _dropped_events += 1
                now = loop.time()
                if now - _last_drop_warn_at > 5.0:
                    print(f"[mic] WARNING: dropped {_dropped_events} "
                          f"event(s) so far (consumer too slow).",
                          flush=True)
                    _last_drop_warn_at = now
                try:
                    event_q.get_nowait()
                except Exception:
                    pass
                try:
                    event_q.put_nowait(payload)
                except Exception:
                    pass

        _dropped_events = 0
        _last_drop_warn_at = 0.0

        # Each call to StreamingTranscriber.feed() invokes one of these
        # from the sounddevice audio thread. They must not block.
        def on_partial(text: str) -> None:
            _safe_put({"kind": "partial", "text": text})

        def on_final(text: str) -> None:
            _safe_put({"kind": "final", "text": text})

        def on_speech(active: bool) -> None:
            _safe_put({
                "kind": "audio_source",
                "text": "speech_on" if active else "speech_off",
            })

        try:
            streaming = StreamingTranscriber(settings, on_partial=on_partial,
                                            on_final=on_final,
                                            on_speech=on_speech)
        except Exception as exc:  # noqa: BLE001
            # Most common cause: missing Vosk model on disk. The mic
            # is open and audio is flowing, but we can't transcribe
            # — broadcast a clear error so the overlay can show it.
            print(f"[mic] FATAL: failed to build StreamingTranscriber: {exc}",
                  flush=True)
            await ws_manager.broadcast({
                "type": "audio_source",
                "device": f"NO TRANSCRIBER: {exc}",
            })
            # Drain the producer queue so the audio thread doesn't
            # block, but never call the recognizer.
            try:
                while True:
                    event_q.get_nowait()
            except _queue.Empty:
                pass
            return

        # 3) Consumer task. This is the only place that broadcasts, so
        #    ordering is well-defined.
        question_buffer: list[str] = []
        end_of_utt_sec = settings.end_of_utterance_ms / 1000.0
        last_activity = loop.time()
        pending_commit: Optional[asyncio.Task] = None

        def schedule_commit_check() -> None:
            """(Re)arm the end-of-utterance timer."""
            nonlocal pending_commit
            if pending_commit is not None and not pending_commit.done():
                pending_commit.cancel()
            pending_commit = asyncio.create_task(_commit_check())

        async def _commit_check() -> None:
            await asyncio.sleep(end_of_utt_sec)
            # If we got here without being cancelled, the speaker has
            # been quiet long enough that we think the question is done.
            if question_buffer:
                question = " ".join(question_buffer).strip()
                question_buffer.clear()
                streaming.reset()
                await _commit_and_answer(question)

        try:
            frame_count = 0
            async for pcm in capture.chunks():
                frame_count += 1
                if frame_count == 1:
                    print(f"[mic] first PCM chunk received: "
                          f"{len(pcm)} bytes", flush=True)
                if frame_count % 200 == 0:
                    print(f"[mic] {frame_count} chunks received "
                          f"({frame_count * len(pcm) / 1024:.0f} KB)",
                          flush=True)
                # Feed Vosk. This is synchronous and CPU-bound, but Vosk
                # is fast (small model + 4 KB chunks). Run it in a
                # worker thread so the event loop stays free.
                await loop.run_in_executor(None, streaming.feed, pcm)

                # Drain anything Vosk produced for this chunk.
                while True:
                    try:
                        event = event_q.get_nowait()
                    except _queue.Empty:
                        break
                    kind = event["kind"]
                    text = event["text"]
                    if kind == "partial":
                        await ws_manager.broadcast({
                            "type": "partial",
                            "text": text,
                        })
                        last_activity = loop.time()
                        schedule_commit_check()
                    elif kind == "final":
                        question_buffer.append(text)
                        last_activity = loop.time()
                        # Push the final to the overlay too, so the user
                        # sees the segment commit visually.
                        await ws_manager.broadcast({
                            "type": "final",
                            "text": text,
                        })
                        schedule_commit_check()
                    elif kind == "audio_source":
                        await ws_manager.broadcast({
                            "type": "speech",
                            "active": text == "speech_on",
                        })
        except asyncio.CancelledError:
            pass
        finally:
            if pending_commit is not None and not pending_commit.done():
                pending_commit.cancel()
            capture.stop()
            print("[mic] stopped.", flush=True)
    finally:
        capture_state.is_running = False
        capture_state.device = None


# Automatic microphone loop startup removed.


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=True,
    )
