"""Swappable LLM backend. One entry point:

    complete(system, user, max_tokens=1000) -> str

The provider is chosen by HISAAB_LLM_PROVIDER; if unset, the first provider
whose key is present wins; if none, "offline" (complete() raises NotConfigured).
The decision is logged once, to stderr, on first use.

    gemini    -> generativelanguage.googleapis.com   GEMINI_API_KEY     gemini-3.6-flash   (GEMINI_MODEL)
    anthropic -> api.anthropic.com/v1/messages       ANTHROPIC_API_KEY  claude-sonnet-4-6  (ANTHROPIC_MODEL)
    groq      -> api.groq.com/openai/v1/chat/...     GROQ_API_KEY       llama-3.3-70b-versatile (GROQ_MODEL)
    offline   -> raises NotConfigured immediately

httpx only, no SDKs.

Failure handling, split by cause:
  - 429 / 5xx / network   -> transient: retry 3x with 1s/2s backoff (4s cap),
                             honouring Retry-After; on final give-up, counted as
                             a rate_limit_fallback.
  - any other 4xx, or a
    200 with no usable text -> permanent: NOT retried, the API's error message
                             printed to stderr ONCE, counted as a config_error.
Either way complete() raises LLMError and the caller drops to its own stub, so
the run always finishes -- but a misconfiguration reads as a misconfiguration,
not as throttling.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

_TIMEOUT = 30.0
_RETRY_DELAYS = (1.0, 2.0, 4.0)  # seconds to wait after a failed attempt; last is the cap

_MODEL = {
    "gemini": os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "groq": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
}
_KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY"}


class NotConfigured(RuntimeError):
    """No LLM provider is configured (offline mode)."""


class LLMError(RuntimeError):
    """The configured provider did not return a usable completion."""


class ConfigError(LLMError):
    """Permanent failure -- bad model id, bad key, bad request, empty response.
    Not retried; the API's message is surfaced once, loudly."""


class _Retryable(Exception):
    """Transient HTTP failure (429 / 5xx)."""

    def __init__(self, status: int, retry_after: float | None):
        super().__init__(f"HTTP {status}")
        self.retry_after = retry_after


def _api_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        if msg:
            return f"HTTP {r.status_code}: {msg}"
    except Exception:
        pass
    return f"HTTP {r.status_code}: {r.text[:300]}"


def _check(r: httpx.Response) -> None:
    if r.status_code == 200:
        return
    if r.status_code == 429 or r.status_code >= 500:
        ra = r.headers.get("retry-after", "")
        raise _Retryable(r.status_code, float(ra) if ra.replace(".", "", 1).isdigit() else None)
    raise ConfigError(_api_message(r))  # 400/401/403/404/... -- permanent


# --- adapters: (system, user, max_tokens, key) -> text; raise _Retryable / httpx / ConfigError

def _gemini(system: str, user: str, max_tokens: int, key: str) -> str:
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL['gemini']}:generateContent",
        params={"key": key},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
        },
        timeout=_TIMEOUT,
    )
    _check(r)
    # gemini-3.6-flash interleaves reasoning parts: a part may carry only a
    # "thoughtSignature" and no "text" key. Never index parts[0]; take every
    # part that actually has text. Tolerate no candidates / no content key.
    cands = r.json().get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p["text"] for p in parts if isinstance(p, dict) and "text" in p)


def _anthropic(system: str, user: str, max_tokens: int, key: str) -> str:
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": _MODEL["anthropic"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=_TIMEOUT,
    )
    _check(r)
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def _groq(system: str, user: str, max_tokens: int, key: str) -> str:
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        json={
            "model": _MODEL["groq"],
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=_TIMEOUT,
    )
    _check(r)
    return (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""


_ADAPTERS = {"gemini": _gemini, "anthropic": _anthropic, "groq": _groq}

# --- resolution + run-state ------------------------------------------------

_forced_offline = False
_resolved: tuple[str, str] | None = None  # (kind, human description)
_logged = False
_rate_limit_fallbacks = 0
_config_errors = 0
_config_error_logged = False


def force_offline(value: bool = True) -> None:
    """Make complete() behave as offline regardless of keys (the --offline flag)."""
    global _forced_offline, _resolved, _logged
    _forced_offline = value
    _resolved = None
    _logged = False


def _resolve() -> tuple[str, str]:
    global _resolved
    if _resolved is None:
        _resolved = _decide()
    return _resolved


def log_decision(extra: str = "") -> None:
    """Print the active-path banner once, to stderr. Called by cli.py and
    eval.run at startup, and by complete() as a fallback for direct library use.
    Idempotent -- the first caller wins, so there's no double line."""
    global _logged
    if not _logged:
        print(f"[hisaab] LLM path: {_resolve()[1]}{extra}", file=sys.stderr)
        _logged = True


def _decide() -> tuple[str, str]:
    if _forced_offline:
        return "offline", "OFFLINE: regex stub (--offline)"

    env = (os.getenv("HISAAB_LLM_PROVIDER") or "").strip().lower()
    if env == "offline":
        return "offline", "OFFLINE: regex stub (HISAAB_LLM_PROVIDER=offline)"
    if env in _ADAPTERS:
        if os.getenv(_KEY_ENV[env]):
            return env, f"LLM: {env}/{_MODEL[env]}"
        return "offline", f"OFFLINE: regex stub ({env} requested but {_KEY_ENV[env]} not set)"
    if env:
        return "offline", f"OFFLINE: regex stub (unknown HISAAB_LLM_PROVIDER={env!r})"

    for kind in ("gemini", "anthropic", "groq"):  # first key present wins, in this order
        if os.getenv(_KEY_ENV[kind]):
            return kind, f"LLM: {kind}/{_MODEL[kind]}"
    return "offline", "OFFLINE: regex stub (no provider key set)"


def describe() -> str:
    return _resolve()[1]


def is_offline() -> bool:
    return _resolve()[0] == "offline"


def rate_limit_fallbacks() -> int:
    return _rate_limit_fallbacks


def config_errors() -> int:
    return _config_errors


def _bump_rate_limit() -> None:
    global _rate_limit_fallbacks
    _rate_limit_fallbacks += 1


def _bump_config_error(exc: Exception) -> None:
    global _config_errors, _config_error_logged
    _config_errors += 1
    if not _config_error_logged:
        print(
            f"\n[hisaab] LLM CONFIG ERROR -- not retrying; every question now answered by the "
            f"regex stub:\n    {exc}\n",
            file=sys.stderr,
        )
        _config_error_logged = True


def _delay_s() -> float:
    try:
        return max(0, int(os.getenv("HISAAB_LLM_DELAY_MS", "100"))) / 1000.0
    except ValueError:
        return 0.1


def complete(system: str, user: str, max_tokens: int = 1000) -> str:
    log_decision()
    kind, _ = _resolve()
    if kind == "offline":
        raise NotConfigured(describe())
    adapter, key = _ADAPTERS[kind], os.getenv(_KEY_ENV[kind])

    time.sleep(_delay_s())  # inter-request pacing so a 300-question run doesn't 429 itself

    last: Exception | None = None
    for i, delay in enumerate(_RETRY_DELAYS):
        try:
            text = adapter(system, user, max_tokens, key)
            if not text.strip():
                raise ConfigError(f"{kind}: 200 OK but no text in the response (model {_MODEL[kind]!r})")
            return text
        except ConfigError as exc:  # permanent -- bad model / key / body / empty
            _bump_config_error(exc)
            raise
        except (_Retryable, httpx.TransportError) as exc:  # transient -- rate limit / 5xx / network
            last = exc
            if i < len(_RETRY_DELAYS) - 1:
                time.sleep(getattr(exc, "retry_after", None) or delay)

    _bump_rate_limit()
    raise LLMError(f"{kind}: gave up after {len(_RETRY_DELAYS)} attempts ({last})")
