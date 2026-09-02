"""Synthetic Razorpay-shaped ledger generator.

Builds a ledger where every settlement's net is exactly:

    net = sum(gross) - sum(fees) - sum(tax) - sum(refunds) + sum(adjustments)

by construction. GroundTruth records the exact component totals and the
exact contributing record ids per settlement -- the answer key. The engine
must never see it; it exists only to score the engine's own decomposition.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
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


def generate(seed: int, n_settlements: int = 250) -> tuple[Ledger, GroundTruth]:
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

    ledger = Ledger(payments=payments, refunds=refunds, fees=fees,
                     adjustments=adjustments, settlements=settlements)
    return ledger, GroundTruth(by_settlement=truths)


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
    ledger, truth = generate(seed=42)
    failures = verify_identity(ledger, truth)
    if failures:
        print(f"FAILED: {len(failures)}/{len(ledger.settlements)} settlements broke the identity:")
        for sid in failures:
            print(f"  {sid}")
        raise SystemExit(1)
    print(f"OK: identity holds for all {len(ledger.settlements)} settlements")

    ledger2, truth2 = generate(seed=42)
    assert ledger == ledger2, "generate(42) is not deterministic (Ledger differs)"
    assert truth == truth2, "generate(42) is not deterministic (GroundTruth differs)"
    print("OK: generate(seed=42) is byte-identical across two runs")
