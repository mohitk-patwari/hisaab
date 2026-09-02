"""Rebuild a settlement's net from first principles, with full provenance.

Pure integer arithmetic. No LLM, no network, no randomness.

    net = gross - fees - tax - refunds + adjustments

The frozen domain contract has no explicit payment/refund -> settlement foreign
key (only Adjustment carries settlement_id). So payments and refunds are
assigned to the earliest settlement that settled at or after the row's own
timestamp, i.e. this settlement owns rows in the half-open window
(previous_settlement.settled_at_utc, this_settlement.settled_at_utc].

# ponytail: date-window assignment, because the contract lacks a settlement_id
# on Payment/Refund/Fee. If those columns get added, replace _window() with a
# direct index lookup and delete the ordering dance.
"""

from __future__ import annotations

from datetime import datetime

from hisaab.domain.models import (
    Explanation,
    Ledger,
    Provenance,
    TracedValue,
)

_COMPONENTS = ("gross", "fees", "tax", "refunds", "adjustments")
# Deductions first: sign / rounding bugs live in the subtracted components more
# often than in gross. Order is also the deterministic tie-break for _suspect().
_SUSPECT_ORDER = ("adjustments", "refunds", "tax", "fees", "gross")


def _ordered_settlements(ledger: Ledger):
    return sorted(ledger.settlements, key=lambda s: s.settled_at_utc)


def _window(ledger: Ledger, settlement_id: str):
    """Return (settlement, lo_exclusive_or_None, hi_inclusive)."""
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


def _prov(table: str, ids, field: str) -> list[Provenance]:
    return [Provenance(source_table=table, source_id=str(i), field=field) for i in ids]


def _traced(value: int, label: str, provs: list[Provenance], settlement_id: str) -> TracedValue:
    if not provs:
        # A genuinely empty component still needs provenance: cite the absence.
        provs = [Provenance(source_table="settlements", source_id=settlement_id, field=f"(no {label} rows)")]
    return TracedValue(value_paise=value, label=label, provenance=provs)


def decompose(ledger: Ledger, settlement_id: str) -> Explanation:
    this, lo, hi = _window(ledger, settlement_id)

    payments = [p for p in ledger.payments if _in_window(p.captured_at_utc, lo, hi)]
    refunds = [r for r in ledger.refunds if _in_window(r.created_at_utc, lo, hi)]
    fee_index = ledger.fee_by_payment_id
    fees = [fee_index[p.payment_id] for p in payments if p.payment_id in fee_index]
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

    return Explanation(
        settlement_id=settlement_id,
        question=f"How is the net of settlement {settlement_id} ({stated_net} paise) built up?",
        lines=lines,
        total=total,
        residual_paise=residual,
        resolved=resolved,
        exception_reason=reason,
    )


def _suspect(residual, stated, computed, lines, payments, refunds, fees, adjustments) -> str:
    """Name the single component most likely to carry the discrepancy.

    Never generic: always cites a component, and where possible the specific
    source rows and the direction of the gap. Order of checks: structural gaps
    in the row set first, then exact-value matches, then the largest deduction.
    """
    by_label = {ln.label: ln for ln in lines}
    ctx = f"stated_net={stated}, computed_net={computed}, residual={residual:+d} paise."

    if all(by_label[l].value_paise == 0 for l in _SUSPECT_ORDER):
        return (
            f"gross suspect: stated_net={stated} paise but no payments, fees, refunds or "
            f"adjustments resolve to this settlement; its contributing row set is empty."
        )

    # Structural: window payments that carry no fee row -> fees/tax understated,
    # which pushes computed_net above stated_net (residual negative).
    priced = {f.payment_id for f in fees}
    unpriced = [p.payment_id for p in payments if p.payment_id not in priced]
    if unpriced and residual < 0:
        return (
            f"fees suspect: payment(s) {unpriced} have no fee row, so fees "
            f"({by_label['fees'].value_paise} paise) and tax are understated and computed_net "
            f"overshoots stated_net. {ctx}"
        )

    # Structural: a refund in the window not tied to any payment in the window
    # -> the refund window is misaligned with the payment window.
    window_pids = {p.payment_id for p in payments}
    orphan = [r.refund_id for r in refunds if r.payment_id not in window_pids]
    if orphan:
        return (
            f"refunds suspect: refund(s) {orphan} reference a payment outside this settlement's "
            f"payment set, so refunds ({by_label['refunds'].value_paise} paise) covers the wrong "
            f"window. {ctx}"
        )

    # Exact: the gap is one adjustment row applied twice or not at all.
    for a in adjustments:
        if a.amount_paise in (residual, -residual):
            return (
                f"adjustments suspect: residual {residual:+d} paise exactly matches adjustment "
                f"{a.adjustment_id} ({a.kind}, {a.amount_paise:+d} paise); it was applied twice or "
                f"not at all. {ctx}"
            )

    # Exact: the gap is one whole component total (double-counted or omitted).
    for label in _SUSPECT_ORDER:
        v = by_label[label].value_paise
        if v != 0 and residual in (v, -v):
            ids = [p.source_id for p in by_label[label].provenance]
            return (
                f"{label} suspect: residual {residual:+d} paise equals the entire {label} total "
                f"({v} paise) from rows {ids}; {label} was double-counted or omitted. {ctx}"
            )

    # Fallback: blame the largest deduction; the residual sign gives direction.
    deductions = [(l, by_label[l].value_paise) for l in ("refunds", "tax", "fees")]
    if by_label["adjustments"].value_paise < 0:
        deductions.append(("adjustments", -by_label["adjustments"].value_paise))
    label, _ = max(deductions, key=lambda d: d[1], default=("gross", 0))
    ln = by_label[label]
    ids = [p.source_id for p in ln.provenance]
    direction = "over-deducted (computed_net too low)" if residual > 0 else "under-deducted (computed_net too high)"
    return (
        f"{label} suspect: it is the largest deduction ({ln.value_paise} paise from rows {ids}) and "
        f"the books are {direction} by {abs(residual)} paise. {ctx}"
    )
