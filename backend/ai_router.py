"""
AI answer routing.

Strategy:
  1. Try the local Ollama model.  Quick, private, no per-call cost.
  2. If it errors or times out, fall back to the configured external
     provider (OpenAI / Anthropic / Gemini).  Or "echo" if no key is set.

The public function `generate_answer(question, context)` is what the
FastAPI handler calls.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import requests

from config.settings import Settings


SYSTEM_PROMPT = (
    "You are a candidate in a professional interview. Respond with concise, confident, and well structured answers that demonstrate clarity, accuracy, and practical insight. Highlight relevant skills, tools, and workflows when applicable. Avoid unnecessary details or filler. Every answer should sound like what an interviewer expects from a strong candidate: professional tone, logical flow, and clear takeaways."
)


def _build_prompt(question: str, context: Optional[str]) -> str:
    ctx = (context or "").strip()
    if ctx:
        return (
            f"Recent context:\n{ctx}\n\n"
            f"Question: {question.strip()}\n\n"
            "Answer in a concise, professional voice."
        )
    return f"Question: {question.strip()}\n\nAnswer concisely and professionally."


# -- Ollama ---------------------------------------------------------

def list_ollama_models(settings: Settings) -> list[dict]:
    """Queries the local Ollama instance to list installed models."""
    try:
        url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
        resp = requests.get(url, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
        models = []
        for m in data.get("models", []):
            models.append({
                "name": m.get("name"),
                "size": m.get("size"),
                "modified_at": m.get("modified_at"),
            })
        return models
    except Exception as e:
        print(f"[ai] failed to list Ollama models: {e}")
        return []


def ollama_stream_generate(settings: Settings, prompt: str, model: Optional[str] = None):
    """Synchronous generator for /api/generate streaming from local Ollama."""
    target_model = model or settings.ollama_model
    payload = {
        "model": target_model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": True,
        "think": False,
        "options": {"temperature": 0.4, "num_predict": 320},
    }
    url = f"{settings.ollama_base_url.rstrip('/')}/api/generate"
    resp = requests.post(
        url,
        json=payload,
        timeout=(5.0, 60.0),
        stream=True
    )
    resp.raise_for_status()
    for line in resp.iter_lines():
        if line:
            data = json.loads(line.decode("utf-8"))
            token = data.get("response", "")
            if token:
                yield token


def _ollama_generate(settings: Settings, prompt: str) -> str:
    """Synchronous /api/generate call against a local Ollama server."""
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        # qwen3 / qwen3.5 are "thinking" models. Disable the visible
        # chain-of-thought so it doesn't leak into the answer.
        "think": False,
        "options": {"temperature": 0.4, "num_predict": 320},
    }
    resp = requests.post(
        f"{settings.ollama_base_url.rstrip('/')}/api/generate",
        json=payload,
        timeout=settings.ollama_timeout_sec,
    )
    resp.raise_for_status()
    data = resp.json()
    # Some Ollama builds return a "thinking" field for reasoning models
    # (qwen3.5, deepseek-r1, ...). We want only the final "response".
    text = (data.get("response") or "").strip()
    if not text and data.get("thinking"):
        # Fall back: strip the visible thinking block if it leaked into
        # the response anyway.
        text = data["thinking"].strip()
    return text


# -- External providers --------------------------------------------

def _external_generate(settings: Settings, prompt: str) -> str:
    provider = settings.external_provider
    if provider == "echo" or not settings.external_api_key:
        return (
            "(External API not configured — this is a placeholder. "
            "Set EXTERNAL_API_KEY in your .env to enable the fallback.)\n\n"
            f"Suggested angle: {prompt[:240]}"
        )

    if provider == "openai":
        return _openai_generate(settings, prompt)
    if provider == "anthropic":
        return _anthropic_generate(settings, prompt)
    if provider == "gemini":
        return _gemini_generate(settings, prompt)
    raise ValueError(f"Unknown external provider: {provider}")


def _openai_generate(settings: Settings, prompt: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.external_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.external_model,
        "temperature": 0.4,
        "max_tokens": 260,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(url, headers=headers, json=body, timeout=settings.external_timeout_sec)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _anthropic_generate(settings: Settings, prompt: str) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": settings.external_api_key or "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.external_model,
        "max_tokens": 260,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    r = requests.post(url, headers=headers, json=body, timeout=settings.external_timeout_sec)
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


def _gemini_generate(settings: Settings, prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.external_model}:generateContent?key={settings.external_api_key}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 260},
    }
    r = requests.post(url, json=body, timeout=settings.external_timeout_sec)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


# -- Public async entry point --------------------------------------

async def generate_answer(question: str, context: Optional[str] = None,
                          settings: Optional[Settings] = None) -> tuple[str, str]:
    """
    Returns (answer, source) where source is "ollama" or "external:<provider>".
    Runs blocking HTTP calls in a worker thread to keep the event loop free.
    """
    settings = settings or Settings()
    prompt = _build_prompt(question, context)
    loop = asyncio.get_running_loop()

    # 1) Try Ollama
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _ollama_generate, settings, prompt),
            timeout=settings.ollama_timeout_sec + 1,
        )
        if text:
            return text, "ollama"
    except (requests.RequestException, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        print(f"[ai] Ollama failed ({exc.__class__.__name__}: {exc}); falling back.")

    # 2) Fallback to external
    text = await loop.run_in_executor(None, _external_generate, settings, prompt)
    return text, f"external:{settings.external_provider}"
