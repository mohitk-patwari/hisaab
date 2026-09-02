"""Synthetic Razorpay-shaped ledger generator.

Builds a ledger where every settlement's net is exactly:

    net = sum(gross) - sum(fees) - sum(tax) - sum(refunds) + sum(adjustments)

by construction. GroundTruth records the exact component totals and the
exact contributing record ids per settlement -- the answer key. The engine
must never see it; it exists only to score the engine's own decomposition.

Run with --edge-cases to inject 8 known-tricky settlements (see
EDGE_CASE_TAGS) on top of the normal random data, each tagged in
GroundTruth.by_settlement[...].edge_cases so eval can report per-case.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from hisaab.domain.models import (
    Adjustment,
    AdjustmentKind,
    Fee,
    Ledger,
    Payment,
    Refund,
    Settlement,
    to_paise,
)

# IST has no DST, so a fixed offset is exact (and avoids relying on a
# system/tzdata zoneinfo database being installed).
IST = timezone(timedelta(hours=5, minutes=30))

METHODS = ["card", "upi", "netbanking", "wallet"]

ADJUSTMENT_KINDS: list[AdjustmentKind] = [
    "chargeback_debit",
    "chargeback_reversal",
    "reserve_hold",
    "reserve_release",
    "dispute_fee",
    "manual_correction",
]
# sign convention: +1 credits the merchant, -1 debits them. manual_correction
# can go either way, chosen per instance.
ADJUSTMENT_SIGN: dict[str, int] = {
    "chargeback_debit": -1,
    "chargeback_reversal": 1,
    "reserve_hold": -1,
    "reserve_release": 1,
    "dispute_fee": -1,
    "manual_correction": 0,
}

EDGE_CASE_TAGS = [
    "zero_net",
    "negative_net",
    "reserve_hold_release",
    "chargeback_debit_reversal",
    "tz_straddle",
    "same_day_same_net",
    "late_refund",
    "rounding_333_33",
]


@dataclass(frozen=True)
class SettlementTruth:
    settlement_id: str
    payment_ids: list[str]
    refund_ids: list[str]
    adjustment_ids: list[str]
    gross_total_paise: int
    fee_total_paise: int
    tax_total_paise: int
    refund_total_paise: int
    adjustment_total_paise: int
    net_paise: int
    edge_cases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GroundTruth:
    by_settlement: dict[str, SettlementTruth]


def _pct(amount_paise: int, pct: str) -> int:
    """amount_paise * pct% rounded to the nearest paisa (Decimal, never float)."""
    exact = Decimal(amount_paise) * Decimal(pct) / Decimal(100)
    return int(exact.to_integral_value(rounding=ROUND_HALF_UP))


def _business_days(start: date, count: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d += timedelta(days=1)
    return out


def _random_gross_paise(rng: random.Random) -> int:
    """Log-normal-ish rupee amount, clamped to ₹99..₹250,000."""
    rupees = rng.lognormvariate(mu=7.3, sigma=1.0)  # median ~ ₹1480
    rupees = min(max(rupees, 99.0), 250_000.0)
    return round(rupees * 100)


def generate(
    seed: int, n_settlements: int = 250, inject_edge_cases: bool = False
) -> tuple[Ledger, GroundTruth]:
    rng = random.Random(seed)

    payments: list[Payment] = []
    refunds: list[Refund] = []
    fees: list[Fee] = []
    adjustments: list[Adjustment] = []
    settlements: list[Settlement] = []
    truths: dict[str, SettlementTruth] = {}

    payment_seq = refund_seq = adjustment_seq = 0

    for i, sdate in enumerate(_business_days(date(2024, 1, 1), n_settlements)):
        settlement_id = f"stl_{i:04d}"
        settled_at_utc = datetime(
            sdate.year, sdate.month, sdate.day, 10, 0, tzinfo=IST
        ).astimezone(timezone.utc)

        payment_ids: list[str] = []
        refund_ids: list[str] = []
        gross_total = fee_total = tax_total = refund_total = 0

        for _ in range(rng.randint(15, 40)):
            payment_seq += 1
            payment_id = f"pay_{payment_seq:06d}"

            capture_date = sdate - timedelta(days=rng.choice([1, 2]))
            captured_at_utc = datetime(
                capture_date.year, capture_date.month, capture_date.day,
                rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
                tzinfo=IST,
            ).astimezone(timezone.utc)

            gross_paise = _random_gross_paise(rng)
            payments.append(Payment(
                payment_id=payment_id,
                order_id=f"order_{payment_seq:06d}",
                captured_at_utc=captured_at_utc,
                gross_paise=gross_paise,
                method=rng.choice(METHODS),
            ))
            payment_ids.append(payment_id)
            gross_total += gross_paise

            fee_paise = _pct(gross_paise, "2")
            tax_paise = _pct(fee_paise, "18")  # GST on the fee
            fees.append(Fee(payment_id=payment_id, fee_paise=fee_paise, tax_paise=tax_paise))
            fee_total += fee_paise
            tax_total += tax_paise

            if rng.random() < 0.08:  # ~8% of payments get refunded
                refund_seq += 1
                refund_id = f"rfn_{refund_seq:06d}"
                if rng.random() < 0.6:
                    amount_paise = gross_paise
                else:
                    amount_paise = max(100, round(gross_paise * rng.uniform(0.1, 0.9)))
                amount_paise = min(amount_paise, gross_paise)

                span_seconds = max((settled_at_utc - captured_at_utc).total_seconds(), 1)
                created_at_utc = captured_at_utc + timedelta(seconds=rng.uniform(0, span_seconds))
                refunds.append(Refund(
                    refund_id=refund_id, payment_id=payment_id,
                    created_at_utc=created_at_utc, amount_paise=amount_paise,
                ))
                refund_ids.append(refund_id)
                refund_total += amount_paise

        adjustment_ids: list[str] = []
        adjustment_total = 0
        if rng.random() < 0.15:  # most settlements have none
            for _ in range(rng.randint(1, 2)):
                adjustment_seq += 1
                adjustment_id = f"adj_{adjustment_seq:06d}"
                kind = rng.choice(ADJUSTMENT_KINDS)
                magnitude = max(100, round(gross_total * rng.uniform(0.005, 0.05)))
                sign = ADJUSTMENT_SIGN[kind] or rng.choice([-1, 1])
                amount_paise = sign * magnitude
                adjustments.append(Adjustment(
                    adjustment_id=adjustment_id, settlement_id=settlement_id,
                    kind=kind, amount_paise=amount_paise, note=f"synthetic {kind}",
                ))
                adjustment_ids.append(adjustment_id)
                adjustment_total += amount_paise

        net_paise = gross_total - fee_total - tax_total - refund_total + adjustment_total
        settlements.append(Settlement(
            settlement_id=settlement_id,
            utr=f"UTR{seed:04d}{i:06d}",
            settled_at_utc=settled_at_utc,
            net_paise=net_paise,
            status="processed",
        ))
        truths[settlement_id] = SettlementTruth(
            settlement_id=settlement_id,
            payment_ids=payment_ids, refund_ids=refund_ids, adjustment_ids=adjustment_ids,
            gross_total_paise=gross_total, fee_total_paise=fee_total, tax_total_paise=tax_total,
            refund_total_paise=refund_total, adjustment_total_paise=adjustment_total,
            net_paise=net_paise,
        )

    if inject_edge_cases:
        _inject_edge_cases(payments, refunds, fees, adjustments, settlements, truths)

    ledger = Ledger(payments=payments, refunds=refunds, fees=fees,
                     adjustments=adjustments, settlements=settlements)
    return ledger, GroundTruth(by_settlement=truths)


# --------------------------------------------------------------------------
# Edge-case injection. Runs as a second pass over the already-generated
# lists/dict (still plain, mutable Python containers at this point -- the
# Ledger/GroundTruth freeze happens after this returns), so each case either
# adds records on top of an existing settlement (_bump) or throws out an
# existing settlement's random content and replaces it outright
# (_replace_settlement). Amounts are hand-picked, not randomised: the point
# of each case is a specific shape, not another random sample.
# --------------------------------------------------------------------------

def _capture_before(settled_at_utc: datetime, days_before: int, hour: int = 12) -> datetime:
    ist_settle_date = settled_at_utc.astimezone(IST).date()
    capture_date = ist_settle_date - timedelta(days=days_before)
    return datetime(
        capture_date.year, capture_date.month, capture_date.day, hour, 0, tzinfo=IST
    ).astimezone(timezone.utc)


def _bump(
    truths: dict[str, SettlementTruth], settlements: list[Settlement], idx: int, *,
    add_gross: int = 0, add_fee: int = 0, add_tax: int = 0, add_refund: int = 0,
    add_adjustment: int = 0,
    add_payment_ids: list[str] = (), add_refund_ids: list[str] = (),
    add_adjustment_ids: list[str] = (), tag: str | None = None,
) -> None:
    """Add records on top of settlement[idx]'s existing content, recomputing
    its net_paise and (optionally) tagging it with an edge case."""
    settlement = settlements[idx]
    old = truths[settlement.settlement_id]
    gross = old.gross_total_paise + add_gross
    fee = old.fee_total_paise + add_fee
    tax = old.tax_total_paise + add_tax
    refund = old.refund_total_paise + add_refund
    adjustment = old.adjustment_total_paise + add_adjustment
    net = gross - fee - tax - refund + adjustment

    truths[settlement.settlement_id] = replace(
        old,
        payment_ids=old.payment_ids + list(add_payment_ids),
        refund_ids=old.refund_ids + list(add_refund_ids),
        adjustment_ids=old.adjustment_ids + list(add_adjustment_ids),
        gross_total_paise=gross, fee_total_paise=fee, tax_total_paise=tax,
        refund_total_paise=refund, adjustment_total_paise=adjustment, net_paise=net,
        edge_cases=old.edge_cases + [tag] if tag else old.edge_cases,
    )
    settlements[idx] = settlement.model_copy(update={"net_paise": net})


def _replace_settlement(
    payments: list[Payment], refunds: list[Refund], fees: list[Fee],
    adjustments: list[Adjustment], settlements: list[Settlement],
    truths: dict[str, SettlementTruth], idx: int, tag: str, *,
    new_payments: list[Payment], new_fees: list[Fee],
    new_refunds: list[Refund] = (), new_adjustments: list[Adjustment] = (),
    settled_at_utc: datetime | None = None,
) -> None:
    """Throw out settlement[idx]'s randomly-generated content and replace it
    with a hand-built scenario. Used when the case needs the *whole*
    settlement's net to be a specific value, not just a component nudged."""
    old_settlement = settlements[idx]
    old_truth = truths[old_settlement.settlement_id]
    drop_payment_ids = set(old_truth.payment_ids)
    drop_refund_ids = set(old_truth.refund_ids)
    drop_adjustment_ids = set(old_truth.adjustment_ids)

    payments[:] = [p for p in payments if p.payment_id not in drop_payment_ids]
    fees[:] = [f for f in fees if f.payment_id not in drop_payment_ids]
    refunds[:] = [r for r in refunds if r.refund_id not in drop_refund_ids]
    adjustments[:] = [a for a in adjustments if a.adjustment_id not in drop_adjustment_ids]

    payments.extend(new_payments)
    fees.extend(new_fees)
    refunds.extend(new_refunds)
    adjustments.extend(new_adjustments)

    gross = sum(p.gross_paise for p in new_payments)
    fee = sum(f.fee_paise for f in new_fees)
    tax = sum(f.tax_paise for f in new_fees)
    refund = sum(r.amount_paise for r in new_refunds)
    adjustment = sum(a.amount_paise for a in new_adjustments)
    net = gross - fee - tax - refund + adjustment

    truths[old_settlement.settlement_id] = SettlementTruth(
        settlement_id=old_settlement.settlement_id,
        payment_ids=[p.payment_id for p in new_payments],
        refund_ids=[r.refund_id for r in new_refunds],
        adjustment_ids=[a.adjustment_id for a in new_adjustments],
        gross_total_paise=gross, fee_total_paise=fee, tax_total_paise=tax,
        refund_total_paise=refund, adjustment_total_paise=adjustment,
        net_paise=net, edge_cases=[tag],
    )
    update = {"net_paise": net}
    if settled_at_utc is not None:
        update["settled_at_utc"] = settled_at_utc
    settlements[idx] = old_settlement.model_copy(update=update)


def _inject_edge_cases(
    payments: list[Payment], refunds: list[Refund], fees: list[Fee],
    adjustments: list[Adjustment], settlements: list[Settlement],
    truths: dict[str, SettlementTruth],
) -> None:
    if len(settlements) < 45:
        raise ValueError("--edge-cases needs at least 45 settlements to place all 8 cases")

    def settled_at(idx: int) -> datetime:
        return settlements[idx].settled_at_utc

    # 1. Zero-net settlement: one payment, fee waived, refunded in full.
    idx = 5
    pay = Payment(payment_id="pay_edge_zero", order_id="order_edge_zero",
                  captured_at_utc=_capture_before(settled_at(idx), 1),
                  gross_paise=50_000, method="card")
    fee = Fee(payment_id=pay.payment_id, fee_paise=0, tax_paise=0)
    rfn = Refund(refund_id="rfn_edge_zero", payment_id=pay.payment_id,
                 created_at_utc=pay.captured_at_utc + timedelta(hours=2), amount_paise=50_000)
    _replace_settlement(payments, refunds, fees, adjustments, settlements, truths, idx,
                         "zero_net", new_payments=[pay], new_fees=[fee], new_refunds=[rfn])

    # 2. Negative-net settlement: a payment already settled elsewhere is
    # refunded in full here, dwarfing this settlement's own small gross.
    idx_old, idx_neg = 3, 10
    old_pay = Payment(payment_id="pay_edge_neg_old", order_id="order_edge_neg_old",
                       captured_at_utc=_capture_before(settled_at(idx_old), 1),
                       gross_paise=500_000, method="card")
    old_fee_paise = _pct(500_000, "2")
    old_tax_paise = _pct(old_fee_paise, "18")
    old_fee = Fee(payment_id=old_pay.payment_id, fee_paise=old_fee_paise, tax_paise=old_tax_paise)
    payments.append(old_pay)
    fees.append(old_fee)
    _bump(truths, settlements, idx_old, add_gross=500_000, add_fee=old_fee_paise,
          add_tax=old_tax_paise, add_payment_ids=[old_pay.payment_id])

    small_pay = Payment(payment_id="pay_edge_neg_small", order_id="order_edge_neg_small",
                         captured_at_utc=_capture_before(settled_at(idx_neg), 1),
                         gross_paise=100_000, method="upi")
    small_fee_paise = _pct(100_000, "2")
    small_tax_paise = _pct(small_fee_paise, "18")
    small_fee = Fee(payment_id=small_pay.payment_id, fee_paise=small_fee_paise, tax_paise=small_tax_paise)
    late_refund = Refund(refund_id="rfn_edge_neg", payment_id=old_pay.payment_id,
                          created_at_utc=settled_at(idx_neg) - timedelta(hours=6),
                          amount_paise=old_pay.gross_paise)
    _replace_settlement(payments, refunds, fees, adjustments, settlements, truths, idx_neg,
                         "negative_net", new_payments=[small_pay], new_fees=[small_fee],
                         new_refunds=[late_refund])

    # 3. Reserve hold, then a partial release two settlements later.
    idx_hold, idx_release = 15, 17
    hold_amt, release_amt = 300_000, 200_000  # holds ₹3,000, releases ₹2,000 of it
    hold_adj = Adjustment(adjustment_id="adj_edge_hold", settlement_id=settlements[idx_hold].settlement_id,
                           kind="reserve_hold", amount_paise=-hold_amt, note="synthetic reserve hold")
    release_adj = Adjustment(adjustment_id="adj_edge_release", settlement_id=settlements[idx_release].settlement_id,
                              kind="reserve_release", amount_paise=release_amt, note="synthetic partial release")
    adjustments.append(hold_adj)
    adjustments.append(release_adj)
    _bump(truths, settlements, idx_hold, add_adjustment=-hold_amt,
          add_adjustment_ids=[hold_adj.adjustment_id], tag="reserve_hold_release")
    _bump(truths, settlements, idx_release, add_adjustment=release_amt,
          add_adjustment_ids=[release_adj.adjustment_id], tag="reserve_hold_release")

    # 4. Chargeback debit, reversed three settlements later.
    idx_cb, idx_cb_rev = 20, 23
    cb_amt = 400_000  # ₹4,000
    debit_adj = Adjustment(adjustment_id="adj_edge_cb_debit", settlement_id=settlements[idx_cb].settlement_id,
                            kind="chargeback_debit", amount_paise=-cb_amt, note="synthetic chargeback")
    reversal_adj = Adjustment(adjustment_id="adj_edge_cb_reversal", settlement_id=settlements[idx_cb_rev].settlement_id,
                               kind="chargeback_reversal", amount_paise=cb_amt, note="synthetic chargeback reversal")
    adjustments.append(debit_adj)
    adjustments.append(reversal_adj)
    _bump(truths, settlements, idx_cb, add_adjustment=-cb_amt,
          add_adjustment_ids=[debit_adj.adjustment_id], tag="chargeback_debit_reversal")
    _bump(truths, settlements, idx_cb_rev, add_adjustment=cb_amt,
          add_adjustment_ids=[reversal_adj.adjustment_id], tag="chargeback_debit_reversal")

    # 5. Timezone straddle: captured 23:50 UTC, which is already the next
    # calendar day in IST -- settles on that next IST business day.
    idx_tz = 27
    ist_capture_date = settled_at(idx_tz).astimezone(IST).date() - timedelta(days=1)
    utc_capture_date = ist_capture_date - timedelta(days=1)
    tz_captured_at_utc = datetime(
        utc_capture_date.year, utc_capture_date.month, utc_capture_date.day, 23, 50, tzinfo=timezone.utc
    )
    assert tz_captured_at_utc.date() != tz_captured_at_utc.astimezone(IST).date(), "not a tz straddle"
    assert tz_captured_at_utc.astimezone(IST).date() == ist_capture_date
    tz_pay = Payment(payment_id="pay_edge_tz", order_id="order_edge_tz",
                      captured_at_utc=tz_captured_at_utc, gross_paise=75_000, method="upi")
    tz_fee_paise = _pct(75_000, "2")
    tz_tax_paise = _pct(tz_fee_paise, "18")
    tz_fee = Fee(payment_id=tz_pay.payment_id, fee_paise=tz_fee_paise, tax_paise=tz_tax_paise)
    payments.append(tz_pay)
    fees.append(tz_fee)
    _bump(truths, settlements, idx_tz, add_gross=75_000, add_fee=tz_fee_paise, add_tax=tz_tax_paise,
          add_payment_ids=[tz_pay.payment_id], tag="tz_straddle")

    # 6. Two settlements on the same IST day with an identical net: clone
    # settlement idx_a's content into idx_b under fresh ids, same day.
    idx_a, idx_b = 30, 31
    a_settlement = settlements[idx_a]
    a_truth = truths[a_settlement.settlement_id]
    payments_by_id = {p.payment_id: p for p in payments}
    fees_by_id = {f.payment_id: f for f in fees}
    refunds_by_id = {r.refund_id: r for r in refunds}
    adjustments_by_id = {a.adjustment_id: a for a in adjustments}

    id_map: dict[str, str] = {}
    clone_payments, clone_fees = [], []
    for n, pid in enumerate(a_truth.payment_ids):
        src = payments_by_id[pid]
        new_id = f"pay_edge_clone_{n:03d}"
        id_map[pid] = new_id
        clone_payments.append(Payment(payment_id=new_id, order_id=f"order_edge_clone_{n:03d}",
                                       captured_at_utc=src.captured_at_utc, gross_paise=src.gross_paise,
                                       method=src.method))
        src_fee = fees_by_id[pid]
        clone_fees.append(Fee(payment_id=new_id, fee_paise=src_fee.fee_paise, tax_paise=src_fee.tax_paise))
    clone_refunds = [
        Refund(refund_id=f"rfn_edge_clone_{n:03d}", payment_id=id_map[refunds_by_id[rid].payment_id],
               created_at_utc=refunds_by_id[rid].created_at_utc, amount_paise=refunds_by_id[rid].amount_paise)
        for n, rid in enumerate(a_truth.refund_ids)
    ]
    clone_adjustments = [
        Adjustment(adjustment_id=f"adj_edge_clone_{n:03d}", settlement_id=settlements[idx_b].settlement_id,
                   kind=adjustments_by_id[aid].kind, amount_paise=adjustments_by_id[aid].amount_paise,
                   note=adjustments_by_id[aid].note)
        for n, aid in enumerate(a_truth.adjustment_ids)
    ]
    _replace_settlement(payments, refunds, fees, adjustments, settlements, truths, idx_b,
                         "same_day_same_net", new_payments=clone_payments, new_fees=clone_fees,
                         new_refunds=clone_refunds, new_adjustments=clone_adjustments,
                         settled_at_utc=a_settlement.settled_at_utc + timedelta(hours=4))
    _bump(truths, settlements, idx_a, tag="same_day_same_net")
    assert truths[settlements[idx_a].settlement_id].net_paise == truths[settlements[idx_b].settlement_id].net_paise
    assert settled_at(idx_a).astimezone(IST).date() == settled_at(idx_b).astimezone(IST).date()

    # 7. A refund issued after its payment already settled, landing in a
    # later settlement instead of the one that paid the gross out.
    idx_old2, idx_late = 33, 36
    old_pay2 = Payment(payment_id="pay_edge_late_old", order_id="order_edge_late_old",
                        captured_at_utc=_capture_before(settled_at(idx_old2), 1),
                        gross_paise=200_000, method="card")
    old_fee2_paise = _pct(200_000, "2")
    old_tax2_paise = _pct(old_fee2_paise, "18")
    old_fee2 = Fee(payment_id=old_pay2.payment_id, fee_paise=old_fee2_paise, tax_paise=old_tax2_paise)
    payments.append(old_pay2)
    fees.append(old_fee2)
    _bump(truths, settlements, idx_old2, add_gross=200_000, add_fee=old_fee2_paise,
          add_tax=old_tax2_paise, add_payment_ids=[old_pay2.payment_id])

    late_refund2 = Refund(refund_id="rfn_edge_late", payment_id=old_pay2.payment_id,
                           created_at_utc=settled_at(idx_late) - timedelta(hours=6), amount_paise=120_000)
    refunds.append(late_refund2)
    _bump(truths, settlements, idx_late, add_refund=120_000,
          add_refund_ids=[late_refund2.refund_id], tag="late_refund")

    # 8. Rounding: fee = 2% of ₹333.33. Hand-verified: 33333 * 2% = 666.66
    # paise -> 667 (half-up); 667 * 18% = 120.06 paise -> 120. Assert it,
    # to catch any future drift in _pct's Decimal rounding.
    idx_round = 40
    gross_333 = to_paise("333.33")
    fee_333 = _pct(gross_333, "2")
    tax_333 = _pct(fee_333, "18")
    assert (gross_333, fee_333, tax_333) == (33_333, 667, 120), "paise rounding drifted"
    round_pay = Payment(payment_id="pay_edge_round", order_id="order_edge_round",
                         captured_at_utc=_capture_before(settled_at(idx_round), 1),
                         gross_paise=gross_333, method="card")
    round_fee = Fee(payment_id=round_pay.payment_id, fee_paise=fee_333, tax_paise=tax_333)
    payments.append(round_pay)
    fees.append(round_fee)
    _bump(truths, settlements, idx_round, add_gross=gross_333, add_fee=fee_333, add_tax=tax_333,
          add_payment_ids=[round_pay.payment_id], tag="rounding_333_33")


def verify_identity(ledger: Ledger, truth: GroundTruth) -> list[str]:
    """Recompute each settlement's net straight from ledger records (not from
    GroundTruth's own totals) and return the ids of any that don't match."""
    payments_by_id = ledger.payments_by_id
    fee_by_payment = ledger.fee_by_payment_id
    refund_by_id = {r.refund_id: r for r in ledger.refunds}
    adjustment_by_id = {a.adjustment_id: a for a in ledger.adjustments}

    failures = []
    for settlement in ledger.settlements:
        st = truth.by_settlement[settlement.settlement_id]
        gross = sum(payments_by_id[pid].gross_paise for pid in st.payment_ids)
        fee = sum(fee_by_payment[pid].fee_paise for pid in st.payment_ids)
        tax = sum(fee_by_payment[pid].tax_paise for pid in st.payment_ids)
        refund = sum(refund_by_id[rid].amount_paise for rid in st.refund_ids)
        adjustment = sum(adjustment_by_id[aid].amount_paise for aid in st.adjustment_ids)
        expected_net = gross - fee - tax - refund + adjustment
        if expected_net != settlement.net_paise:
            failures.append(settlement.settlement_id)
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge-cases", action="store_true",
                         help="inject the 8 known edge-case settlements, tagged in GroundTruth")
    args = parser.parse_args()

    ledger, truth = generate(seed=42, inject_edge_cases=args.edge_cases)
    failures = verify_identity(ledger, truth)
    if failures:
        print(f"FAILED: {len(failures)}/{len(ledger.settlements)} settlements broke the identity:")
        for sid in failures:
            print(f"  {sid}")
        raise SystemExit(1)
    print(f"OK: identity holds for all {len(ledger.settlements)} settlements")

    ledger2, truth2 = generate(seed=42, inject_edge_cases=args.edge_cases)
    assert ledger == ledger2, "generate(42) is not deterministic (Ledger differs)"
    assert truth == truth2, "generate(42) is not deterministic (GroundTruth differs)"
    print("OK: generate(seed=42) is byte-identical across two runs")

    if args.edge_cases:
        tagged = {sid: st.edge_cases for sid, st in truth.by_settlement.items() if st.edge_cases}
        print(f"Edge cases: {len(tagged)}/{len(ledger.settlements)} settlements contain at least one")
        by_tag: dict[str, list[str]] = {}
        for sid, tags in tagged.items():
            for t in tags:
                by_tag.setdefault(t, []).append(sid)
        for tag in EDGE_CASE_TAGS:
            print(f"  {tag}: {', '.join(sorted(by_tag.get(tag, [])))}")
