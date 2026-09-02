"""Adversarial narrations: prove the gate rejects invented numbers."""

from hisaab.domain.models import Explanation, Provenance, TracedValue
from hisaab.llm.gate import format_rupees, verify


def _tv(value: int, label: str) -> TracedValue:
    return TracedValue(
        value_paise=value,
        label=label,
        provenance=[Provenance(source_table="t", source_id="1", field="f")],
    )


def _exp(question: str = "How is the net of settlement setl_1 (138460 paise) built up?") -> Explanation:
    lines = [
        _tv(150_000, "gross"),
        _tv(3_000, "fees"),
        _tv(540, "tax"),
        _tv(5_000, "refunds"),
        _tv(-3_000, "adjustments"),
    ]
    return Explanation(
        settlement_id="setl_1",
        question=question,
        lines=lines,
        total=_tv(138_460, "computed_net"),
        residual_paise=0,
        resolved=True,
    )


def test_faithful_narration_passes_unchanged():
    good = (
        "Settlement setl_1 nets ₹1,384.60: gross ₹1,500.00 less fees ₹30.00, tax ₹5.40 "
        "and refunds ₹50.00, plus adjustments -₹30.00. Residual ₹0.00."
    )
    out, violations = verify(good, _exp())
    assert violations == []
    assert out == good


def test_invented_number_is_caught_and_narration_replaced():
    bad = "You received ₹1,384.60 after a special ₹9,999.00 platform charge."
    out, violations = verify(bad, _exp())
    assert violations == ["₹9,999.00"]
    assert out != bad
    assert "can't stand behind" in out


def test_wrong_value_for_a_real_component_is_caught():
    bad = "Fees on this settlement were ₹40.00."  # real fees are ₹30.00
    out, violations = verify(bad, _exp())
    assert violations == ["₹40.00"]
    assert "₹40.00" in out  # refusal names the offending token


def test_paise_denominated_invention_is_caught():
    bad = "The residual is 500 paise, so something is off."  # residual is 0
    _, violations = verify(bad, _exp())
    assert violations == ["500"]


def test_multiple_inventions_all_listed():
    bad = "Gross ₹1,500.00, mystery fee ₹12.34, hidden levy ₹77.00."
    _, violations = verify(bad, _exp())
    assert violations == ["₹12.34", "₹77.00"]


def test_sign_and_formatting_tolerated():
    # -₹30.00 matches abs(-3000); bare 138460 paise matches; 0 always allowed
    ok = "Adjustments were -₹30.00; net 138460 paise; residual 0."
    _, violations = verify(ok, _exp())
    assert violations == []


def test_digits_inside_identifiers_are_not_numbers():
    ok = "Settlement setl_1 with UTR utr_9 reconciled at ₹1,384.60."
    _, violations = verify(ok, _exp())
    assert violations == []


def test_date_stated_by_the_engine_is_allowed():
    exp = _exp(question="Which settlement settled on 2026-01-11 IST?")
    ok = "The settlement that landed on 2026-01-11 nets ₹1,384.60."
    _, violations = verify(ok, exp)
    assert violations == []


def test_date_not_in_trace_is_still_caught():
    ok_exp = _exp()  # question has no date
    bad = "This settled on 2027-05-05 and nets ₹1,384.60."
    _, violations = verify(bad, ok_exp)
    assert any("2027" in v for v in violations)


def test_format_rupees():
    assert format_rupees(138_460) == "₹1,384.60"
    assert format_rupees(-3_000) == "-₹30.00"
    assert format_rupees(0) == "₹0.00"
    assert format_rupees(5) == "₹0.05"
