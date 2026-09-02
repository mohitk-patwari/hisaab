"""narrate(explanation) -> short English answer.

The LLM sees ONLY the TracedValue figures (label + value_paise) and the
residual/resolved status -- never raw ledger rows. Offline, a deterministic
sentence is assembled from the same figures.
"""

from __future__ import annotations

import json

from hisaab.domain.models import Explanation
from hisaab.llm import call_llm, have_llm
from hisaab.llm.gate import format_rupees

_SYSTEM = (
    "You are given the components of a settlement calculation as JSON. Write 1-3 "
    "plain sentences explaining the result to a merchant. Use ONLY the rupee "
    "amounts implied by the given paise values. Do NOT invent any number, do NOT "
    "mention row counts, dates, or ids that are not in the JSON. If resolved is "
    "false, say the figures do not reconcile and quote the exception_reason."
)


def _payload(explanation: Explanation) -> dict:
    return {
        "question": explanation.question,
        "lines": [{"label": ln.label, "value_paise": ln.value_paise} for ln in explanation.lines],
        "total": {"label": explanation.total.label, "value_paise": explanation.total.value_paise},
        "residual_paise": explanation.residual_paise,
        "resolved": explanation.resolved,
        "exception_reason": explanation.exception_reason,
    }


def narrate(explanation: Explanation) -> str:
    if have_llm():
        try:
            text = call_llm(_SYSTEM, json.dumps(_payload(explanation)), max_tokens=400).strip()
            if text:
                return text
        except Exception:
            pass
    return _stub_narrate(explanation)


def _stub_narrate(explanation: Explanation) -> str:
    if not explanation.lines:  # e.g. find_settlement_by_date with no hit
        return f"{explanation.question} {explanation.exception_reason or 'No result.'}"
    parts = ", ".join(f"{ln.label} {format_rupees(ln.value_paise)}" for ln in explanation.lines)
    head = f"{explanation.question} {parts}. Net {format_rupees(explanation.total.value_paise)}."
    if explanation.resolved:
        return f"{head} Reconciled: residual {format_rupees(explanation.residual_paise)}."
    return f"{head} Does not reconcile ({format_rupees(explanation.residual_paise)}): {explanation.exception_reason}"
