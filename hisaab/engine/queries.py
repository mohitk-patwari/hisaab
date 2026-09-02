"""Deterministic query handlers. Each takes (ledger, params) -> Explanation.

No LLM, no network, no randomness — everything routes through decompose().
"""

from __future__ import annotations

from datetime import date, timedelta, timezone

from hisaab.domain.models import Explanation, Ledger, Provenance, TracedValue

from hisaab.engine.decompose import _COMPONENTS, decompose

IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset, no DST


def _line(explanation: Explanation, component: str) -> TracedValue:
    for ln in explanation.lines:
        if ln.label == component:
            return ln
    raise KeyError(f"no such component {component!r}; expected one of {_COMPONENTS}")


def explain_settlement(ledger: Ledger, params: dict) -> Explanation:
    return decompose(ledger, params["settlement_id"])


def explain_delta(ledger: Ledger, params: dict) -> Explanation:
    a = decompose(ledger, params["settlement_id_a"])
    b = decompose(ledger, params["settlement_id_b"])
    by_a = {ln.label: ln for ln in a.lines}
    by_b = {ln.label: ln for ln in b.lines}

    lines = []
    for comp in _COMPONENTS:
        la, lb = by_a[comp], by_b[comp]
        lines.append(
            TracedValue(
                value_paise=lb.value_paise - la.value_paise,
                label=f"{comp}_delta",
                provenance=list(la.provenance) + list(lb.provenance),
            )
        )

    computed_delta = b.total.value_paise - a.total.value_paise
    stated_delta = ledger.settlements_by_id[b.settlement_id].net_paise - ledger.settlements_by_id[a.settlement_id].net_paise
    residual = stated_delta - computed_delta

    total = TracedValue(
        value_paise=computed_delta,
        label="computed_net_delta",
        provenance=list(a.total.provenance) + list(b.total.provenance),
    )
    resolved = residual == 0
    reason = None
    if not resolved:
        reason = (
            f"delta unresolved: stated net moved {stated_delta:+d} paise but components moved "
            f"{computed_delta:+d}. Carried from settlement {a.settlement_id} "
            f"(residual {a.residual_paise:+d}) and {b.settlement_id} (residual {b.residual_paise:+d})."
        )

    return Explanation(
        settlement_id=f"{a.settlement_id}->{b.settlement_id}",
        question=f"What changed between settlements {a.settlement_id} and {b.settlement_id}?",
        lines=lines,
        total=total,
        residual_paise=residual,
        resolved=resolved,
        exception_reason=reason,
    )


def component_breakdown(ledger: Ledger, params: dict) -> Explanation:
    settlement_id = params["settlement_id"]
    component = params["component"]
    full = decompose(ledger, settlement_id)
    ln = _line(full, component)
    return Explanation(
        settlement_id=settlement_id,
        question=f"Just the {component} of settlement {settlement_id}.",
        lines=[ln],
        total=TracedValue(value_paise=ln.value_paise, label=f"{component}_total", provenance=list(ln.provenance)),
        residual_paise=0,
        resolved=True,
    )


def find_settlement_by_date(ledger: Ledger, params: dict) -> Explanation:
    target: date = params["date_ist"]

    def ist_date(dt):
        aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        return aware.astimezone(IST).date()

    matches = [s for s in ledger.settlements if ist_date(s.settled_at_utc) == target]
    if len(matches) == 1:
        return decompose(ledger, matches[0].settlement_id)

    why = "no settlement" if not matches else f"{len(matches)} settlements"
    return Explanation(
        settlement_id=matches[0].settlement_id if matches else "",
        question=f"Which settlement settled on {target.isoformat()} IST?",
        lines=[],
        total=TracedValue(
            value_paise=0,
            label="no_unique_settlement",
            provenance=[Provenance(source_table="settlements", source_id="(none)", field="settled_at_utc")],
        ),
        residual_paise=0,
        resolved=False,
        exception_reason=f"{why} settled on {target.isoformat()} IST",
    )


def largest_deduction(ledger: Ledger, params: dict) -> Explanation:
    settlement_id = params["settlement_id"]
    full = decompose(ledger, settlement_id)
    by_label = {ln.label: ln for ln in full.lines}

    # Category deductions plus each individual money-out adjustment row.
    candidates: list[tuple[str, int, list[Provenance]]] = [
        (label, by_label[label].value_paise, list(by_label[label].provenance))
        for label in ("fees", "tax", "refunds")
    ]
    for a in ledger.adjustments_by_settlement_id.get(settlement_id, []):
        if a.amount_paise < 0:
            candidates.append(
                (
                    f"adjustment:{a.kind}",
                    -a.amount_paise,
                    [Provenance(source_table="adjustments", source_id=a.adjustment_id, field="amount_paise")],
                )
            )

    label, amount, provs = max(candidates, key=lambda c: c[1])
    line = TracedValue(value_paise=amount, label=label, provenance=provs)
    return Explanation(
        settlement_id=settlement_id,
        question=f"What was the biggest deduction from settlement {settlement_id}?",
        lines=[line],
        total=line,
        residual_paise=0,
        resolved=True,
    )


HANDLERS = {
    "explain_settlement": explain_settlement,
    "explain_delta": explain_delta,
    "component_breakdown": component_breakdown,
    "find_settlement_by_date": find_settlement_by_date,
    "largest_deduction": largest_deduction,
}
