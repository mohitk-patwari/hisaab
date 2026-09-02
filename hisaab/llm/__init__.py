"""LLM plumbing shared by intent + narrate.

Reads ANTHROPIC_API_KEY from .env. If it is absent, have_llm() returns False and
callers fall back to their own deterministic stubs so the pipeline runs offline.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

_API_KEY = os.getenv("ANTHROPIC_API_KEY")
_MODEL = os.getenv("HISAAB_MODEL", "claude-opus-5")
_URL = "https://api.anthropic.com/v1/messages"


def have_llm() -> bool:
    return bool(_API_KEY)


def call_llm(system: str, user: str, *, max_tokens: int = 1024) -> str:
    """One-shot completion -> assistant text. Raises on transport/HTTP error."""
    resp = httpx.post(
        _URL,
        headers={
            "x-api-key": _API_KEY or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
