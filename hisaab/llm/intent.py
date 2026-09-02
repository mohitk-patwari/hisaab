"""parse(question) -> Intent | None. Map English to one engine query handler.

The LLM (via providers.complete) returns strict JSON validated against Intent;
one retry on a parse/validation failure, then return None. Returning None
(cannot map confidently) is a success path. With no provider configured, or if
the provider fails, _stub_parse -- a deliberately dumb regex -- takes over.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from hisaab.llm import providers

Handler = Literal[
    "explain_settlement",
    "explain_delta",
    "component_breakdown",
    "find_settlement_by_date",
    "largest_deduction",
]

_REQUIRED: dict[str, tuple[str, ...]] = {
    "explain_settlement": ("settlement_id",),
    "explain_delta": ("settlement_id_a", "settlement_id_b"),
    "component_breakdown": ("settlement_id", "component"),
    "find_settlement_by_date": ("date_ist",),
    "largest_deduction": ("settlement_id",),
}
_COMPONENTS = {"gross", "fees", "tax", "refunds", "adjustments"}


class Intent(BaseModel):
    model_config = ConfigDict(frozen=True)

    handler: Handler
    params: dict[str, str]

    @model_validator(mode="after")
    def _check_params(self) -> "Intent":
        missing = [k for k in _REQUIRED[self.handler] if not self.params.get(k)]
        if missing:
            raise ValueError(f"{self.handler} needs params {missing}")
        if self.handler == "component_breakdown" and self.params["component"] not in _COMPONENTS:
            raise ValueError(f"component must be one of {sorted(_COMPONENTS)}")
        if self.handler == "find_settlement_by_date":
            date.fromisoformat(self.params["date_ist"])  # ValueError if malformed
        return self

    def query_params(self) -> dict:
        """Params shaped for hisaab.engine.queries.HANDLERS."""
        if self.handler == "find_settlement_by_date":
            return {"date_ist": date.fromisoformat(self.params["date_ist"])}
        return dict(self.params)


_SYSTEM = (
    "You convert a question about Razorpay settlements into ONE query. "
    "Reply with STRICT JSON only, no prose, no code fences. Shape: "
    '{"handler": <name>, "params": {<key>: <string>}}. Handlers and params:\n'
    '- explain_settlement: {"settlement_id"}\n'
    '- explain_delta: {"settlement_id_a", "settlement_id_b"}\n'
    '- component_breakdown: {"settlement_id", "component"}  component in '
    "gross|fees|tax|refunds|adjustments\n"
    '- find_settlement_by_date: {"date_ist"}  ISO YYYY-MM-DD\n'
    '- largest_deduction: {"settlement_id"}\n'
    'If the question does not map cleanly, reply {"handler": "none", "params": {}}.'
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict | None:
    m = _JSON.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


_PARSE_FAILED = object()  # unextractable JSON or Intent validation error -> worth one retry


def _to_intent(raw: str):
    """-> Intent | None (confident no-map) | _PARSE_FAILED (garbled)."""
    obj = _extract_json(raw)
    if obj is None:
        return _PARSE_FAILED
    if obj.get("handler") in (None, "", "none"):
        return None
    try:
        return Intent(**obj)
    except (ValidationError, TypeError, ValueError):
        return _PARSE_FAILED


def parse(question: str) -> Intent | None:
    prompts = (question, question + "\n\nReturn ONLY the JSON object, nothing else.")
    for i, user in enumerate(prompts):  # initial try + one retry, only on a parse failure
        try:
            raw = providers.complete(_SYSTEM, user, max_tokens=1000)
        except (providers.NotConfigured, providers.LLMError):
            return _stub_parse(question)  # complete() has already counted the failure
        result = _to_intent(raw)
        if result is not _PARSE_FAILED:
            return result  # Intent, or None (confident no-map) -- both are success
    return None  # parse failed twice


# --- offline deterministic fallback ---------------------------------------
#
# This regex is the DUMB BASELINE the LLM ablation is measured against. Do not
# teach it new synonyms to lift its score -- the gap to the LLM is the result.
# The one allowed change (prompt point 6) is the refusal-safety gate below: it
# only makes the stub REFUSE more, never route better.

# A settlement id token, with or without a leading "settlement " word:
# matches stl_0000 (real generator), setl_1 (fixtures/demo), "settlement stl_7", s3.
_ID = re.compile(r"\b(?:settlement\s+)?((?:se?tl|s)_?\d+)\b", re.IGNORECASE)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Refusal-safety gate (prompt point 6). Naming a settlement id is not enough to
# get a decomposition: if the question asks about an attribute Hisaab does not
# model -- card network, city, customer, contract terms -- refuse instead of
# defaulting to explain_settlement and answering confidently. This is the only
# change to _stub_parse; it makes it refuse more, never route better, so the
# ablation baseline stays honest.
_NOT_MODELLED = (
    "card", "network", "city", "location", "customer", "contract",
    "currency", "percentage", "interest", "pending", "gmv",
)


def _stub_parse(question: str) -> Intent | None:
    q = question.lower()
    ids = [m.group(1) for m in _ID.finditer(question)]

    if (d := _DATE.search(question)) and not ids:
        return _mk("find_settlement_by_date", date_ist=d.group())
    if len(ids) >= 2 and any(w in q for w in ("delta", "differ", "compare", " vs ", "between", "changed", "farak")):
        return _mk("explain_delta", settlement_id_a=ids[0], settlement_id_b=ids[1])
    if ids:
        if any(w in q for w in _NOT_MODELLED):
            return None  # asks about something Hisaab has no row for -> refuse, don't guess
        if "gst" in q:  # the standard Indian term for the tax component
            return _mk("component_breakdown", settlement_id=ids[0], component="tax")
        for comp in _COMPONENTS:
            if comp in q or comp.rstrip("s") in q:
                return _mk("component_breakdown", settlement_id=ids[0], component=comp)
        if any(w in q for w in ("largest", "biggest", "top deduction", "most")):
            return _mk("largest_deduction", settlement_id=ids[0])
        return _mk("explain_settlement", settlement_id=ids[0])
    return None


def _mk(handler: str, **params: str) -> Intent | None:
    try:
        return Intent(handler=handler, params=params)
    except ValidationError:
        return None
