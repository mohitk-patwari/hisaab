"""Scoring + report rendering for the eval.

Rules baked in here, not left to callers:
  - never emit a bare accuracy: always "<n>/<denom>" via ratio()
  - never truncate the exception list: render_report dumps all of them
  - exit_code() is 1 whenever UNSUPPORTED NUMBERS > 0
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# ponytail: money-shaped token heuristic + exact-paise equality. Good enough while
# the narrator formats rupees plainly. Upgrade to tolerance / locale-aware parsing
# if narration starts writing lakh grouping or "~9.4k".
_MONEY_RE = re.compile(r"(?:₹|Rs\.?|INR)?\s?\d[\d,]*(?:\.\d+)?", re.IGNORECASE)


def _token_to_paise(tok: str) -> int | None:
    stripped = re.sub(r"(?i)(₹|Rs\.?|INR|,|\s)", "", tok)
    if not stripped or stripped in {".", "-"}:
        return None
    try:
        paise = Decimal(stripped) * 100
    except InvalidOperation:
        return None
    if paise != paise.to_integral_value():
        return None
    return abs(int(paise))


def _is_money_shaped(tok: str) -> bool:
    return bool(re.search(r"(?i)₹|Rs|INR", tok)) or "." in tok or "," in tok


def extract_money_paise(text: str) -> list[int]:
    """Every money-shaped number in a string, as integer paise."""
    out: list[int] = []
    for m in _MONEY_RE.finditer(text or ""):
        tok = m.group(0).strip()
        if not _is_money_shaped(tok):
            continue
        p = _token_to_paise(tok)
        if p is not None:
            out.append(p)
    return out


def supported_paise(explanation) -> set[int]:
    """Paise values an Explanation actually traces to a source row."""
    vals = {abs(line.value_paise) for line in explanation.lines}
    vals.add(abs(explanation.total.value_paise))
    vals.add(abs(explanation.residual_paise))
    return vals


def unsupported_numbers(narration: str, explanation) -> list[int]:
    """Money numbers in the narration that no TracedValue backs. Hallucinations."""
    ok = supported_paise(explanation)
    return [p for p in extract_money_paise(narration) if p not in ok]


def ratio(n: int, denom: int) -> str:
    return f"{n}/{denom}"


@dataclass
class QResult:
    qid: str
    question: str
    expected_intent: str
    got_intent: str | None
    answerable: bool
    expected_paise: int | None
    got_paise: int | None
    resolved: bool
    exception_reason: str | None
    latency_ms: float
    unsupported: list[int] = field(default_factory=list)

    @property
    def intent_ok(self) -> bool:
        return self.got_intent == self.expected_intent

    @property
    def answer_ok(self) -> bool:
        return self.answerable and self.got_paise is not None and self.got_paise == self.expected_paise

    @property
    def refused(self) -> bool:
        return (not self.resolved) or self.got_intent == "unsupported"

    @property
    def refusal_ok(self) -> bool:
        return (not self.answerable) and self.refused


@dataclass
class Report:
    seed: int
    n_settlements: int
    results: list[QResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def unanswerable(self) -> int:
        return sum(1 for r in self.results if not r.answerable)

    @property
    def unsupported_total(self) -> int:
        return sum(len(r.unsupported) for r in self.results)

    @property
    def exceptions(self) -> list[QResult]:
        return [r for r in self.results if r.exception_reason]


def render_report(rep: Report) -> str:
    total = rep.total
    intent_ok = sum(1 for r in rep.results if r.intent_ok)
    answer_ok = sum(1 for r in rep.results if r.answer_ok)
    refusal_ok = sum(1 for r in rep.results if r.refusal_ok)
    latencies = [r.latency_ms for r in rep.results] or [0.0]
    mean_ms = statistics.fmean(latencies)

    lines = [
        f"HISAAB EVAL — seed {rep.seed}",
        f"{rep.n_settlements} settlements · {total} questions",
        "",
        f"Intent classification      {ratio(intent_ok, total)}",
        f"Answer numerically correct {ratio(answer_ok, total)}",
        f"UNSUPPORTED NUMBERS         {ratio(rep.unsupported_total, total)}",
        f"Correct refusals            {ratio(refusal_ok, rep.unanswerable)} unanswerable",
        f"Mean latency                {mean_ms:.0f} ms",
        "",
        f"UNRESOLVED EXCEPTIONS ({len(rep.exceptions)}):",
    ]
    if rep.exceptions:
        for r in rep.exceptions:  # full list, never truncated
            lines.append(f'  {r.qid}  "{r.question}"  -> {r.exception_reason}')
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def exit_code(rep: Report) -> int:
    return 1 if rep.unsupported_total > 0 else 0


def demo() -> None:
    """Self-check: the number-integrity rule is the one that must not rot."""

    class _TV:
        def __init__(self, v):
            self.value_paise = v

    class _Expl:
        lines = [_TV(941200), _TV(-1200)]
        total = _TV(940000)
        residual_paise = 0

    e = _Expl()
    assert extract_money_paise("you got ₹9,412.00 minus ₹12.00") == [941200, 1200]
    assert extract_money_paise("across 3 refunds") == []  # bare small int ignored
    assert unsupported_numbers("net ₹9,400.00 from ₹9,412.00 less ₹12.00", e) == []
    assert unsupported_numbers("net ₹9,400.00 but also ₹5,000.00 appeared", e) == [500000]
    assert ratio(0, 300) == "0/300"

    rep = Report(seed=42, n_settlements=250, results=[])
    assert exit_code(rep) == 0
    rep.results.append(
        QResult("Q1", "q", "x", "x", True, 1, 1, True, None, 1.0, unsupported=[500000])
    )
    assert exit_code(rep) == 1
    assert "UNRESOLVED EXCEPTIONS (0)" in render_report(rep)
    print("metrics.demo ok")


if __name__ == "__main__":
    demo()
