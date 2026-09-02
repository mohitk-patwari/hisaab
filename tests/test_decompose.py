"""Hand-built tiny ledgers exercising decompose() and the query handlers."""

from datetime import date, datetime, timezone

import pytest

from hisaab.domain.models import (
    Adjustment,
    Fee,
    Ledger,
    Payment,
    Refund,
    Settlement,
)
from hisaab.engine.decompose import decompose
from hisaab.engine import queries


def _dt(day, hour=12):
    return datetime(2026, 1, day, hour, 0)


def _pay(pid, day, gross, settlement_id=None):
    return Payment(payment_id=pid, order_id=f"ord_{pid}", captured_at_utc=_dt(day), gross_paise=gross,
                   method="upi", settlement_id=settlement_id)


def _settle(sid, day, net, hour=23):
    return Settlement(settlement_id=sid, utr=f"utr_{sid}", settled_at_utc=_dt(day, hour), net_paise=net, status="processed")


# --- a ledger that reconciles exactly -----------------------------------------

def _clean_ledger():
    payments = [_pay("pay_1", 10, 100_000, "setl_1"), _pay("pay_2", 10, 50_000, "setl_1")]
    fees = [
        Fee(payment_id="pay_1", fee_paise=2_000, tax_paise=360, settlement_id="setl_1"),
        Fee(payment_id="pay_2", fee_paise=1_000, tax_paise=180, settlement_id="setl_1"),
    ]
    refunds = [Refund(refund_id="rfnd_1", payment_id="pay_1", created_at_utc=_dt(10, 15), amount_paise=5_000,
                       settlement_id="setl_1")]
    adjustments = [
        Adjustment(adjustment_id="adj_1", settlement_id="setl_1", kind="reserve_hold", amount_paise=-3_000, note="hold"),
    ]
    # 150000 - 3000 - 540 - 5000 + (-3000) = 138460
    settlements = [_settle("setl_1", 11, 138_460)]
    return Ledger(payments=payments, refunds=refunds, fees=fees, adjustments=adjustments, settlements=settlements)


def test_clean_settlement_resolves():
    exp = decompose(_clean_ledger(), "setl_1")
    assert exp.residual_paise == 0
    assert exp.resolved is True
    assert exp.exception_reason is None
    assert exp.total.value_paise == 138_460
    vals = {ln.label: ln.value_paise for ln in exp.lines}
    assert vals == {"gross": 150_000, "fees": 3_000, "tax": 540, "refunds": 5_000, "adjustments": -3_000}


def test_provenance_lists_every_contributing_row():
    exp = decompose(_clean_ledger(), "setl_1")
    prov = {ln.label: {p.source_id for p in ln.provenance} for ln in exp.lines}
    assert prov["gross"] == {"pay_1", "pay_2"}
    assert prov["fees"] == {"pay_1", "pay_2"}
    assert prov["refunds"] == {"rfnd_1"}
    assert prov["adjustments"] == {"adj_1"}
    # the aggregate total cites all of them plus the stated settlement row
    total_ids = {(p.source_table, p.source_id) for p in exp.total.provenance}
    assert ("settlements", "setl_1") in total_ids
    assert ("payments", "pay_1") in total_ids


def test_empty_component_still_has_provenance():
    led = _clean_ledger()
    led = Ledger(payments=led.payments, refunds=[], fees=led.fees, adjustments=led.adjustments, settlements=[_settle("setl_1", 11, 143_460)])
    exp = decompose(led, "setl_1")
    refunds_line = next(ln for ln in exp.lines if ln.label == "refunds")
    assert refunds_line.value_paise == 0
    assert len(refunds_line.provenance) >= 1  # TracedValue forbids empty provenance


# --- reconciliation failures name a specific component -----------------------

def test_tampered_fee_falls_back_to_largest_deduction():
    # gross 300000, fee 15000 (largest deduction), tax 2700, refund 4000, no adj.
    payments = [_pay("pay_x", 10, 300_000, "setl_1")]
    fees = [Fee(payment_id="pay_x", fee_paise=15_900, tax_paise=2_700, settlement_id="setl_1")]  # fee is 900 too high vs stated
    refunds = [Refund(refund_id="rfnd_x", payment_id="pay_x", created_at_utc=_dt(10, 15), amount_paise=4_000,
                       settlement_id="setl_1")]
    settlements = [_settle("setl_1", 11, 278_300)]  # = 300000 - 15000 - 2700 - 4000
    led = Ledger(payments=payments, refunds=refunds, fees=fees, settlements=settlements)
    exp = decompose(led, "setl_1")
    assert exp.residual_paise == 900
    assert exp.resolved is False
    assert exp.exception_reason.startswith("fees suspect")
    assert "over-deducted" in exp.exception_reason


def test_residual_matching_an_adjustment_names_that_adjustment():
    led = _clean_ledger()
    # add a second adjustment the stated net never accounted for
    adjs = list(led.adjustments) + [
        Adjustment(adjustment_id="adj_2", settlement_id="setl_1", kind="dispute_fee", amount_paise=-1_234, note="x")
    ]
    led = Ledger(payments=led.payments, refunds=led.refunds, fees=led.fees, adjustments=adjs, settlements=led.settlements)
    exp = decompose(led, "setl_1")
    assert exp.residual_paise == 1_234  # computed dropped 1234, stated unchanged
    assert exp.resolved is False
    assert "adjustments suspect" in exp.exception_reason
    assert "adj_2" in exp.exception_reason


def test_missing_fee_row_names_fees():
    payments = [_pay("pay_1", 10, 100_000, "setl_1"), _pay("pay_2", 10, 50_000, "setl_1")]
    fees = [Fee(payment_id="pay_1", fee_paise=2_000, tax_paise=0, settlement_id="setl_1")]  # pay_2 has no fee row
    settlements = [_settle("setl_1", 11, 145_000)]  # assumes both fees present -> too low
    led = Ledger(payments=payments, fees=fees, settlements=settlements)
    exp = decompose(led, "setl_1")
    assert exp.residual_paise < 0
    assert exp.exception_reason.startswith("fees suspect")
    assert "pay_2" in exp.exception_reason


def test_empty_settlement_with_nonzero_net_names_gross():
    led = Ledger(settlements=[_settle("setl_1", 11, 999)])
    exp = decompose(led, "setl_1")
    assert exp.residual_paise == 999
    assert exp.exception_reason.startswith("gross suspect")


def test_unknown_settlement_raises():
    with pytest.raises(KeyError):
        decompose(_clean_ledger(), "nope")


# --- settlement_id FK is the primary membership; date windowing is a fallback
# used only to diagnose null-FK rows, never to include them --------------------

def test_fk_assigns_payments_to_their_own_settlement():
    payments = [_pay("pay_early", 5, 40_000, "setl_a"), _pay("pay_late", 12, 60_000, "setl_b")]
    fees = [Fee(payment_id="pay_early", fee_paise=0, tax_paise=0, settlement_id="setl_a"),
            Fee(payment_id="pay_late", fee_paise=0, tax_paise=0, settlement_id="setl_b")]
    settlements = [_settle("setl_a", 8, 40_000), _settle("setl_b", 15, 60_000)]
    led = Ledger(payments=payments, fees=fees, settlements=settlements)

    a = decompose(led, "setl_a")
    b = decompose(led, "setl_b")
    assert {p.source_id for p in next(l for l in a.lines if l.label == "gross").provenance} == {"pay_early"}
    assert {p.source_id for p in next(l for l in b.lines if l.label == "gross").provenance} == {"pay_late"}
    assert a.resolved and b.resolved
    assert a.exception_reason is None and b.exception_reason is None


def test_unsettled_payment_in_window_is_reported_not_included():
    """A None settlement_id means genuinely unsettled: even though its own
    timestamp falls inside setl_a's date window, it must not be swept into
    setl_a's gross -- only reported via the fallback diagnostic."""
    settled = _pay("pay_settled", 5, 40_000, "setl_a")
    stray = _pay("pay_stray", 6, 999, settlement_id=None)  # in setl_a's window, but unsettled
    fees = [Fee(payment_id="pay_settled", fee_paise=0, tax_paise=0, settlement_id="setl_a")]
    settlements = [_settle("setl_a", 8, 40_000), _settle("setl_b", 15, 60_000)]
    led = Ledger(payments=[settled, stray], fees=fees, settlements=settlements)

    exp = decompose(led, "setl_a")
    assert {p.source_id for p in next(l for l in exp.lines if l.label == "gross").provenance} == {"pay_settled"}
    assert exp.total.value_paise == 40_000
    assert exp.resolved is True  # the FK-based net is exact
    assert "pay_stray" in exp.exception_reason
    assert "still unsettled" in exp.exception_reason


# --- query handlers -------------------------------------------------------

def test_explain_settlement_matches_decompose():
    led = _clean_ledger()
    assert queries.explain_settlement(led, {"settlement_id": "setl_1"}) == decompose(led, "setl_1")


def test_component_breakdown_returns_single_line():
    exp = queries.component_breakdown(_clean_ledger(), {"settlement_id": "setl_1", "component": "fees"})
    assert len(exp.lines) == 1
    assert exp.lines[0].label == "fees"
    assert exp.lines[0].value_paise == 3_000
    assert exp.resolved is True


def test_component_breakdown_rejects_bad_component():
    with pytest.raises(KeyError):
        queries.component_breakdown(_clean_ledger(), {"settlement_id": "setl_1", "component": "gremlins"})


def test_explain_delta():
    payments = [_pay("p1", 5, 40_000, "s_a"), _pay("p2", 12, 70_000, "s_b")]
    fees = [Fee(payment_id="p1", fee_paise=1_000, tax_paise=0, settlement_id="s_a"),
            Fee(payment_id="p2", fee_paise=2_000, tax_paise=0, settlement_id="s_b")]
    settlements = [_settle("s_a", 8, 39_000), _settle("s_b", 15, 68_000)]
    led = Ledger(payments=payments, fees=fees, settlements=settlements)
    exp = queries.explain_delta(led, {"settlement_id_a": "s_a", "settlement_id_b": "s_b"})
    vals = {ln.label: ln.value_paise for ln in exp.lines}
    assert vals["gross_delta"] == 30_000
    assert vals["fees_delta"] == 1_000
    assert exp.total.value_paise == 29_000  # 68000 - 39000
    assert exp.residual_paise == 0
    assert exp.resolved is True


def test_find_settlement_by_date_uses_ist():
    # 2026-01-10 20:00 UTC == 2026-01-11 01:30 IST
    settlements = [Settlement(settlement_id="setl_1", utr="u", settled_at_utc=datetime(2026, 1, 10, 20, 0), net_paise=0, status="processed")]
    led = Ledger(settlements=settlements)
    hit = queries.find_settlement_by_date(led, {"date_ist": date(2026, 1, 11)})
    assert hit.settlement_id == "setl_1"
    miss = queries.find_settlement_by_date(led, {"date_ist": date(2026, 1, 10)})
    assert miss.resolved is False
    assert "no settlement" in miss.exception_reason


def test_largest_deduction():
    led = _clean_ledger()  # fees 3000, tax 540, refunds 5000, adj -3000
    exp = queries.largest_deduction(led, {"settlement_id": "setl_1"})
    assert exp.lines[0].label == "refunds"
    assert exp.lines[0].value_paise == 5_000


def test_largest_deduction_picks_individual_adjustment():
    led = _clean_ledger()
    big = [Adjustment(adjustment_id="adj_big", settlement_id="setl_1", kind="chargeback_debit", amount_paise=-20_000, note="cb")]
    led = Ledger(payments=led.payments, refunds=led.refunds, fees=led.fees, adjustments=big, settlements=[_settle("setl_1", 11, 121_460)])
    exp = queries.largest_deduction(led, {"settlement_id": "setl_1"})
    assert exp.lines[0].label == "adjustment:chargeback_debit"
    assert exp.lines[0].value_paise == 20_000
