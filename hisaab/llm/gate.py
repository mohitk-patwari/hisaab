"""The gate: no number reaches the user unless the computed trace contains it.

verify(narration, explanation) pulls every money-shaped numeric token out of the
narration and checks each one against the value_paise figures in the Explanation
(rupee or paise reading, sign-insensitive). Anything left over is an UNSUPPORTED
NUMBER: the narration is thrown away and replaced with a refusal.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from hisaab.domain.models import Explanation

# A number token: optional leading sign / currency mark, then digits with
# optional thousands commas and decimals. Lookbehind/ahead keep us from matching
# digits that are part of an identifier like "setl_1" or "utr_9".
_NUM = re.compile(r"(?<![\w.])-?(?:₹|Rs\.?\s*)?\d[\d,]*(?:\.\d+)?(?![\w])")


def format_rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    return f"{sign}₹{abs(paise) // 100:,}.{abs(paise) % 100:02d}"


def _token_to_paise(token: str) -> set[int]:
    """Every plausible paise value this token could denote (rupees or paise)."""
    s = token.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace(",", "").replace(" ", "")
    s = s.lstrip("-")
    if not s or s == ".":
        return set()
    try:
        d = Decimal(s)
    except InvalidOperation:
        return set()
    out: set[int] = set()
    rupees = d * 100
    if rupees == rupees.to_integral_value():
        out.add(int(rupees))
    if d == d.to_integral_value():
        out.add(int(d))
    return out


def _allowed(explanation: Explanation) -> set[int]:
    vals = {abs(tv.value_paise) for tv in (*explanation.lines, explanation.total)}
    vals.add(abs(explanation.residual_paise))
    vals.add(0)  # "residual ₹0.00", "0 refunds"
    return vals


def verify(narration: str, explanation: Explanation) -> tuple[str, list[str]]:
    allowed = _allowed(explanation)
    question_digits = re.sub(r"[^\d]", "", explanation.question)

    violations: list[str] = []
    for m in _NUM.finditer(narration):
        token = m.group().strip()
        cands = {abs(v) for v in _token_to_paise(token)}
        if cands & allowed:
            continue
        bare = re.sub(r"[^\d]", "", token)
        if bare and bare in question_digits:  # a figure the engine itself stated (e.g. a date)
            continue
        violations.append(token)

    if violations:
        refusal = (
            "I can't stand behind that answer: it contains "
            f"{len(violations)} number(s) absent from the computed trace "
            f"({', '.join(violations)}). Trust the trace below instead."
        )
        return refusal, violations
    return narration, []
