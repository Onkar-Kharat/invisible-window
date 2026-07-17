"""
FastAPI app that ties everything together.

  * POST /api/transcribe       – audio blob in, transcript + answer out
  * WS   /ws                   – live mic state + answer push
  * GET  /api/health           – liveness probe

The WS endpoint is what the PyQt overlay connects to. The /api/transcribe
endpoint is what the embedded mic loop hits each time VAD produces a
speech segment, so the overlay can render answers in real time.
"""
from __future__ import annotations

import asyncio
import base64
import json
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
    except Exception as e:
        print(f"[backend] WARNING: Transcription model could not be loaded ({e.__class__.__name__}: {e}).")
        print("[backend] Transcription API (/api/transcribe) will be disabled, but assistant will work.")
    yield
    # Nothing to tear down explicitly.


app = FastAPI(title="Invisible Interview Assistant", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


# ---- REST ---------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "transcription": settings.transcription_backend,
        "ollama_model": settings.ollama_model,
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
      capture  ->  Vosk (partial/final)  ->  asyncio.Queue
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
    print("[mic] loop starting", flush=True)

    # 1) Open the audio device. Resolves to CABLE Output (device 17)
    #    by default; the user just has to set Zoom's microphone to
    #    "CABLE Input" for the interviewer's voice to flow in.
    print("[mic] opening audio device...", flush=True)
    try:
        capture = StreamingCapture(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"[mic] FATAL: cannot open audio device: {exc}", flush=True)
        return
    print(f"[mic] device opened: {capture.description}", flush=True)

    # Tell the overlay what we're listening to (so the user can see
    # if VB-Cable is actually wired in correctly).
    await ws_manager.broadcast({
        "type": "audio_source",
        "device": capture.description,
    })
    print(f"[mic] listening on: {capture.description}", flush=True)

    # 2) Queue + recogniser. The recogniser's callbacks enqueue
    #    events; the consumer task below drains the queue.
    event_queue: asyncio.Queue[_StreamEvent] = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()
    capture.start(loop)

    # Each call to StreamingTranscriber.feed() invokes one of these
    # from the sounddevice audio thread. They must not block.
    def on_partial(text: str) -> None:
        try:
            loop.call_soon_threadsafe(
                event_queue.put_nowait,
                {"kind": "partial", "text": text},
            )
        except Exception:
            pass

    def on_final(text: str) -> None:
        try:
            loop.call_soon_threadsafe(
                event_queue.put_nowait,
                {"kind": "final", "text": text},
            )
        except Exception:
            pass

    streaming = StreamingTranscriber(settings, on_partial=on_partial,
                                    on_final=on_final)

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
        async for pcm in capture.chunks():
            # Feed Vosk. This is synchronous and CPU-bound, but Vosk
            # is fast (small model + 4 KB chunks). Run it in a
            # worker thread so the event loop stays free.
            await loop.run_in_executor(None, streaming.feed, pcm)

            # Drain anything Vosk produced for this chunk.
            while True:
                try:
                    event = event_queue.get_nowait()
                except asyncio.QueueEmpty:
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
    except asyncio.CancelledError:
        pass
    finally:
        if pending_commit is not None and not pending_commit.done():
            pending_commit.cancel()
        capture.stop()
        print("[mic] stopped.")


# Automatic microphone loop startup removed.



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=True,
    )
