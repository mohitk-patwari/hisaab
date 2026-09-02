"""Scoring + report rendering for the eval.

Rules baked in here, not left to callers:
  - never emit a bare accuracy: always "<n>/<denom>" via ratio()
  - never truncate the exception list: render_report dumps all of them
  - exit_code() is 1 whenever UNSUPPORTED NUMBERS > 0

The UNSUPPORTED NUMBERS themselves come from the production gate
(hisaab.llm.gate.verify) — run.py hands the violation tokens straight in.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# ponytail: money-shaped token heuristic + exact-paise equality. Good enough while
# the narrator formats rupees plainly. Upgrade to tolerance / locale-aware parsing
# if narration starts writing lakh grouping or "~9.4k".
#
# A comma only counts as thousands-grouping when exactly 3 digits follow it
# (₹1,741,811). Without that requirement, decompose()'s debug-style
# exception_reason ("stated_net=6247711, computed_net=...") gets its sentence
# comma swallowed into the number, turning a bare "6247711," into a phantom
# "6,247,711" and flagging it as an unsupported/hallucinated figure.
_MONEY_RE = re.compile(
    r"(?:₹|Rs\.?|INR)?\s?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", re.IGNORECASE
)


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
    unsupported: list = field(default_factory=list)  # gate violation tokens

    @property
    def intent_ok(self) -> bool:
        return self.got_intent == self.expected_intent

    @property
    def refused(self) -> bool:
        return (not self.resolved) or self.got_intent == "unsupported"

    @property
    def attempted(self) -> bool:
        """Pipeline committed to a number rather than refusing."""
        return (not self.refused) and self.got_paise is not None

    # --- the three honest outcomes for an ANSWERABLE question ---
    @property
    def answer_ok(self) -> bool:
        return self.answerable and self.attempted and self.got_paise == self.expected_paise

    @property
    def wrong_answer(self) -> bool:
        return self.answerable and self.attempted and self.got_paise != self.expected_paise

    @property
    def wrong_refusal(self) -> bool:
        """Refused a question that genuinely had an answer. The honest one."""
        return self.answerable and self.refused

    # --- outcomes for an UNANSWERABLE question ---
    @property
    def refusal_ok(self) -> bool:
        return (not self.answerable) and self.refused

    @property
    def bogus_answer(self) -> bool:
        """Produced a number for a question that had no answer."""
        return (not self.answerable) and not self.refused


@dataclass
class Report:
    seed: int
    n_settlements: int
    results: list[QResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def answerable(self) -> int:
        return sum(1 for r in self.results if r.answerable)

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
    ans = rep.answerable
    unans = rep.unanswerable
    n = lambda pred: sum(1 for r in rep.results if pred(r))  # noqa: E731

    intent_ok = n(lambda r: r.intent_ok)
    answer_ok = n(lambda r: r.answer_ok)
    wrong_ans = n(lambda r: r.wrong_answer)
    wrong_ref = n(lambda r: r.wrong_refusal)
    refusal_ok = n(lambda r: r.refusal_ok)
    bogus = n(lambda r: r.bogus_answer)
    latencies = [r.latency_ms for r in rep.results] or [0.0]
    mean_ms = statistics.fmean(latencies)

    lines = [
        f"HISAAB EVAL — seed {rep.seed}",
        f"{rep.n_settlements} settlements · {total} questions",
        "",
        f"Intent classification      {ratio(intent_ok, total)}",
        f"Answer numerically correct {ratio(answer_ok, ans)} answerable",
        f"Wrong answer               {ratio(wrong_ans, ans)} answerable",
        f"WRONG refusal              {ratio(wrong_ref, ans)} answerable   <- refused a question that had an answer",
        f"UNSUPPORTED NUMBERS         {ratio(rep.unsupported_total, total)}",
        f"Correct refusals            {ratio(refusal_ok, unans)} unanswerable",
        f"Answered the unanswerable   {ratio(bogus, unans)} unanswerable",
        f"Mean latency                {mean_ms:.0f} ms",
        "",
        f"UNRESOLVED EXCEPTIONS ({len(rep.exceptions)}):",
    ]
    if rep.exceptions:
        for r in rep.exceptions:  # full list, never truncated
            tag = "  [WRONG-REFUSAL]" if r.wrong_refusal else ""
            lines.append(f'  {r.qid}  "{r.question}"  -> {r.exception_reason}{tag}')
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def exit_code(rep: Report) -> int:
    return 1 if rep.unsupported_total > 0 else 0


def demo() -> None:
    """Self-check: the three-way outcome split is the thing that must not rot."""

    def qr(**kw):
        base = dict(
            qid="Q", question="q", expected_intent="x", got_intent="x", answerable=True,
            expected_paise=100, got_paise=100, resolved=True, exception_reason=None, latency_ms=1.0,
        )
        base.update(kw)
        return QResult(**base)

    assert ratio(0, 300) == "0/300"
    # the three honest outcomes for an answerable question are mutually exclusive
    assert qr().answer_ok and not qr().wrong_answer and not qr().wrong_refusal
    assert qr(got_paise=99).wrong_answer and not qr(got_paise=99).answer_ok
    assert qr(resolved=False, exception_reason="ambiguous").wrong_refusal
    assert qr(got_intent="unsupported").wrong_refusal
    # unanswerable outcomes
    assert qr(answerable=False, resolved=False, exception_reason="absent").refusal_ok
    assert qr(answerable=False, got_paise=5).bogus_answer
    assert not qr(answerable=False, resolved=False).wrong_refusal

    rep = Report(seed=42, n_settlements=250, results=[qr(unsupported=["₹5,000.00"])])
    assert exit_code(rep) == 1
    r = render_report(rep)
    assert "WRONG refusal" in r and "0/1 answerable" in r
    assert exit_code(Report(seed=42, n_settlements=250, results=[qr()])) == 0
    print("metrics.demo ok")


if __name__ == "__main__":
    demo()
