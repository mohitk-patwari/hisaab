"""Entrypoint: question -> intent -> deterministic query -> narrate -> gate -> print.

    python -m hisaab.cli "why did I only receive so little for setl_1?"

Loads data/ledger.json (pydantic Ledger dump) if present, else a built-in demo
ledger so the whole pipeline runs offline with no setup.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from hisaab.domain.models import Adjustment, Fee, Ledger, Payment, Refund, Settlement
from hisaab.engine import queries
from hisaab.llm.gate import format_rupees, verify
from hisaab.llm.intent import parse
from hisaab.llm.narrate import narrate

_HELP = (
    "Ask e.g.: 'explain settlement setl_1' | 'compare setl_1 and setl_2' | "
    "'fees of setl_1' | 'biggest deduction from setl_1' | 'settlement on 2026-01-11'"
)


def _demo_ledger() -> Ledger:
    def dt(day: int, hour: int = 12) -> datetime:
        return datetime(2026, 1, day, hour, 0)

    payments = [
        Payment(payment_id="pay_1", order_id="ord_1", captured_at_utc=dt(10), gross_paise=1_000_00, method="upi"),
        Payment(payment_id="pay_2", order_id="ord_2", captured_at_utc=dt(10, 14), gross_paise=500_00, method="card"),
        Payment(payment_id="pay_3", order_id="ord_3", captured_at_utc=dt(12), gross_paise=2_000_00, method="upi"),
    ]
    fees = [
        Fee(payment_id="pay_1", fee_paise=20_00, tax_paise=3_60),
        Fee(payment_id="pay_2", fee_paise=10_00, tax_paise=1_80),
        Fee(payment_id="pay_3", fee_paise=40_00, tax_paise=7_20),
    ]
    refunds = [Refund(refund_id="rfnd_1", payment_id="pay_1", created_at_utc=dt(10, 16), amount_paise=50_00)]
    adjustments = [
        Adjustment(adjustment_id="adj_1", settlement_id="setl_1", kind="reserve_hold", amount_paise=-30_00, note="hold"),
    ]
    settlements = [
        # 150000 - 3000 - 540 - 5000 + (-3000) = 138460
        Settlement(settlement_id="setl_1", utr="utr_1", settled_at_utc=dt(11, 10), net_paise=138_460, status="processed"),
        # 200000 - 4000 - 720 = 195280
        Settlement(settlement_id="setl_2", utr="utr_2", settled_at_utc=dt(13, 10), net_paise=195_280, status="processed"),
    ]
    return Ledger(payments=payments, refunds=refunds, fees=fees, adjustments=adjustments, settlements=settlements)


def _load_ledger() -> Ledger:
    p = Path("data/ledger.json")
    if p.exists():
        return Ledger.model_validate_json(p.read_text())
    return _demo_ledger()


def _print_trace(explanation) -> None:
    print("\n--- trace ---")
    for ln in explanation.lines:
        src = ", ".join(f"{pr.source_table}:{pr.source_id}.{pr.field}" for pr in ln.provenance)
        print(f"  {ln.label:<14}{format_rupees(ln.value_paise):>15}   <- {src}")
    print(f"  {'net':<14}{format_rupees(explanation.total.value_paise):>15}")
    print(f"  {'residual':<14}{format_rupees(explanation.residual_paise):>15}   resolved={explanation.resolved}")
    if explanation.exception_reason:
        print(f"  exception: {explanation.exception_reason}")


def main() -> None:
    try:  # the ₹ sign trips the default Windows console codepage
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    question = " ".join(sys.argv[1:]).strip() or "explain settlement setl_1"
    ledger = _load_ledger()

    intent = parse(question)
    if intent is None:
        print(f"Couldn't map that to a known query.\n{_HELP}")
        return

    explanation = queries.HANDLERS[intent.handler](ledger, intent.query_params())
    narration = narrate(explanation)
    final, violations = verify(narration, explanation)

    print(final)
    if violations:
        print(f"[gate blocked {len(violations)} unsupported number(s): {', '.join(violations)}]")
    _print_trace(explanation)


if __name__ == "__main__":
    main()
