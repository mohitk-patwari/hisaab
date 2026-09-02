"""Rebuild a settlement's net from first principles, with full provenance.

Pure integer arithmetic. No LLM, no network, no randomness.

    net = gross - fees - tax - refunds + adjustments

Payments, refunds and fees now carry an explicit settlement_id (added
2026-09-02 -- see FAILURES.md "domain contract unfrozen once"), so
membership is a direct FK lookup via Ledger.payments_for/refunds_for/
fees_for. A None settlement_id means genuinely unsettled, not missing data.

The old date-window logic (assign a row to the settlement whose window
(previous_settlement.settled_at_utc, this_settlement.settled_at_utc] contains
its own timestamp) is kept, but demoted: it now runs ONLY over rows with
settlement_id=None, purely to report "a naive date guess would have swept
this row in" via exception_reason. It never contributes to the computed
net -- an unsettled row is unsettled regardless of how well its timestamp
lines up with some settlement's window.
"""

from __future__ import annotations

from datetime import datetime

from hisaab.domain.models import (
    Adjustment,
    Explanation,
    Fee,
    Ledger,
    Payment,
    Provenance,
    Refund,
    TracedValue,
)

_COMPONENTS = ("gross", "fees", "tax", "refunds", "adjustments")
# Deductions first: sign / rounding bugs live in the subtracted components more
# often than in gross. Order is also the deterministic tie-break for _suspect().
_SUSPECT_ORDER = ("adjustments", "refunds", "tax", "fees", "gross")


# ponytail: duplicated from hisaab/llm/gate.py's format_rupees (2 lines) rather
# than importing it -- engine computes, llm narrates, and this module has no
# business depending on the llm layer. Extract to a shared module if a third
# copy shows up.
def _fmt_rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    return f"{sign}₹{abs(paise) // 100:,}.{abs(paise) % 100:02d}"


def _ordered_settlements(ledger: Ledger):
    return sorted(ledger.settlements, key=lambda s: s.settled_at_utc)


def _window(ledger: Ledger, settlement_id: str):
    """Return (settlement, lo_exclusive_or_None, hi_inclusive).

    FALLBACK ONLY: no longer used to decide primary membership (that's the
    settlement_id FK now). Still needed to bound the window a null-FK row
    would have landed in, for the "still unsettled" diagnostic below.
    """
    if settlement_id not in ledger.settlements_by_id:
        raise KeyError(f"no settlement {settlement_id!r} in ledger")
    ordered = _ordered_settlements(ledger)
    ids = [s.settlement_id for s in ordered]
    i = ids.index(settlement_id)
    this = ordered[i]
    lo = ordered[i - 1].settled_at_utc if i > 0 else None
    return this, lo, this.settled_at_utc


def _in_window(ts: datetime, lo, hi) -> bool:
    return (lo is None or ts > lo) and ts <= hi


def _unsettled_in_window(
    ledger: Ledger, lo, hi
) -> tuple[list[Payment], list[Refund]]:
    """Rows with settlement_id=None whose own timestamp falls in this
    settlement's date window -- what the old window-only logic would have
    assigned here. Reported, never included: settlement_id=None means
    genuinely unsettled, not "assign me by best guess."
    """
    payments = [p for p in ledger.payments
                if p.settlement_id is None and _in_window(p.captured_at_utc, lo, hi)]
    refunds = [r for r in ledger.refunds
               if r.settlement_id is None and _in_window(r.created_at_utc, lo, hi)]
    return payments, refunds


def _unsettled_note(payments: list[Payment], refunds: list[Refund]) -> str:
    parts = []
    if payments:
        ids = [p.payment_id for p in payments]
        total = _fmt_rupees(sum(p.gross_paise for p in payments))
        parts.append(
            f"{len(payments)} payment(s) captured in this settlement's window are still "
            f"unsettled ({total} total, {ids}) -- excluded from this net pending settlement"
        )
    if refunds:
        ids = [r.refund_id for r in refunds]
        total = _fmt_rupees(sum(r.amount_paise for r in refunds))
        parts.append(
            f"{len(refunds)} refund(s) in this settlement's window are still unsettled "
            f"({total} total, {ids}) -- excluded from this net pending settlement"
        )
    return "; ".join(parts) + "."


def _prov(table: str, ids, field: str) -> list[Provenance]:
    return [Provenance(source_table=table, source_id=str(i), field=field) for i in ids]


def _traced(value: int, label: str, provs: list[Provenance], settlement_id: str) -> TracedValue:
    if not provs:
        # A genuinely empty component still needs provenance: cite the absence.
        provs = [Provenance(source_table="settlements", source_id=settlement_id, field=f"(no {label} rows)")]
    return TracedValue(value_paise=value, label=label, provenance=provs)


def decompose(ledger: Ledger, settlement_id: str) -> Explanation:
    this, lo, hi = _window(ledger, settlement_id)  # bounds kept only for the fallback below

    payments = ledger.payments_for(settlement_id)
    refunds = ledger.refunds_for(settlement_id)
    fees = ledger.fees_for(settlement_id)
    adjustments = ledger.adjustments_by_settlement_id.get(settlement_id, [])

    gross = sum(p.gross_paise for p in payments)
    fee_total = sum(f.fee_paise for f in fees)
    tax_total = sum(f.tax_paise for f in fees)
    refund_total = sum(r.amount_paise for r in refunds)
    adj_total = sum(a.amount_paise for a in adjustments)  # amounts are signed

    computed_net = gross - fee_total - tax_total - refund_total + adj_total
    stated_net = this.net_paise
    residual = stated_net - computed_net

    lines = [
        _traced(gross, "gross", _prov("payments", [p.payment_id for p in payments], "gross_paise"), settlement_id),
        _traced(fee_total, "fees", _prov("fees", [f.payment_id for f in fees], "fee_paise"), settlement_id),
        _traced(tax_total, "tax", _prov("fees", [f.payment_id for f in fees], "tax_paise"), settlement_id),
        _traced(refund_total, "refunds", _prov("refunds", [r.refund_id for r in refunds], "amount_paise"), settlement_id),
        _traced(adj_total, "adjustments", _prov("adjustments", [a.adjustment_id for a in adjustments], "amount_paise"), settlement_id),
    ]

    all_prov = [pr for ln in lines for pr in ln.provenance]
    all_prov.append(Provenance(source_table="settlements", source_id=settlement_id, field="net_paise"))
    total = TracedValue(value_paise=computed_net, label="computed_net", provenance=all_prov)

    resolved = residual == 0
    reason = None
    if not resolved:
        reason = _suspect(residual, stated_net, computed_net, lines, payments, refunds, fees, adjustments)
    else:
        # FALLBACK fires here: resolved is still True (the FK-based net is
        # exact), but if the old window logic would have swept in a still-
        # unsettled row, say so -- it's real, useful context, not an error.
        unsettled_payments, unsettled_refunds = _unsettled_in_window(ledger, lo, hi)
        if unsettled_payments or unsettled_refunds:
            reason = _unsettled_note(unsettled_payments, unsettled_refunds)

    return Explanation(
        settlement_id=settlement_id,
        question=f"How is the net of settlement {settlement_id} ({_fmt_rupees(stated_net)}) built up?",
        lines=lines,
        total=total,
        residual_paise=residual,
        resolved=resolved,
        exception_reason=reason,
    )


def _suspect(
    residual: int, stated: int, computed: int, lines: list[TracedValue],
    payments: list[Payment], refunds: list[Refund], fees: list[Fee], adjustments: list[Adjustment],
) -> str:
    """Name the single component most likely to carry the discrepancy.

    Never generic: always cites a component, and where possible the specific
    source rows and the direction of the gap. Order of checks: structural gaps
    in the row set first, then exact-value matches, then the largest deduction.
    All amounts are ₹-formatted prose -- no bare paise integers, since this
    text is embedded verbatim in narrations and money-shaped tokens there get
    checked against the trace.
    """
    by_label = {ln.label: ln for ln in lines}
    ctx = (
        f"The settlement states {_fmt_rupees(stated)}; recomputing from source rows gives "
        f"{_fmt_rupees(computed)}."
    )

    if all(by_label[l].value_paise == 0 for l in _SUSPECT_ORDER):
        return (
            f"gross suspect: the settlement states {_fmt_rupees(stated)} but no payments, fees, "
            f"refunds or adjustments resolve to it -- its contributing row set is empty."
        )

    # Structural: FK payments that carry no fee row -> fees/tax understated,
    # which pushes computed_net above stated_net (residual negative).
    priced = {f.payment_id for f in fees}
    unpriced = [p.payment_id for p in payments if p.payment_id not in priced]
    if unpriced and residual < 0:
        return (
            f"fees suspect: payment(s) {unpriced} have no fee row, so fees "
            f"({_fmt_rupees(by_label['fees'].value_paise)}) and tax are understated and the "
            f"computed net overshoots the stated net. {ctx}"
        )

    # Structural: a refund tied to a payment outside this settlement's payment
    # set -- the FK on the refund and the FK on its payment disagree.
    settled_pids = {p.payment_id for p in payments}
    orphan = [r.refund_id for r in refunds if r.payment_id not in settled_pids]
    if orphan:
        return (
            f"refunds suspect: refund(s) {orphan} reference a payment outside this settlement's "
            f"payment set, so refunds ({_fmt_rupees(by_label['refunds'].value_paise)}) cover the "
            f"wrong rows. {ctx}"
        )

    # Exact: the gap is one adjustment row applied twice or not at all.
    for a in adjustments:
        if a.amount_paise in (residual, -residual):
            return (
                f"adjustments suspect: the gap ({_fmt_rupees(abs(residual))}) exactly matches "
                f"adjustment {a.adjustment_id} ({a.kind}, {_fmt_rupees(a.amount_paise)}); it was "
                f"applied twice or not at all. {ctx}"
            )

    # Exact: the gap is one whole component total (double-counted or omitted).
    for label in _SUSPECT_ORDER:
        v = by_label[label].value_paise
        if v != 0 and residual in (v, -v):
            ids = [p.source_id for p in by_label[label].provenance]
            return (
                f"{label} suspect: the gap ({_fmt_rupees(abs(residual))}) equals the entire "
                f"{label} total ({_fmt_rupees(v)}) from rows {ids}; {label} was double-counted "
                f"or omitted. {ctx}"
            )

    # Fallback: blame the largest deduction; the residual sign gives direction.
    deductions = [(l, by_label[l].value_paise) for l in ("refunds", "tax", "fees")]
    if by_label["adjustments"].value_paise < 0:
        deductions.append(("adjustments", -by_label["adjustments"].value_paise))
    label, _ = max(deductions, key=lambda d: d[1], default=("gross", 0))
    ln = by_label[label]
    ids = [p.source_id for p in ln.provenance]
    direction = "over-deducted (the computed net is too low)" if residual > 0 else "under-deducted (the computed net is too high)"
    return (
        f"{label} suspect: it is the largest deduction ({_fmt_rupees(ln.value_paise)} from rows "
        f"{ids}) and the books are {direction} by {_fmt_rupees(abs(residual))}. {ctx}"
    )
