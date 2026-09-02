import pytest

from hisaab.domain.models import Explanation, Provenance, TracedValue, to_paise


def test_to_paise_exact():
    assert to_paise("123.45") == 12345
    assert to_paise("100") == 10000


def test_to_paise_rejects_sub_paise():
    with pytest.raises(ValueError):
        to_paise("1.005")


def test_traced_value_requires_provenance():
    with pytest.raises(Exception):
        TracedValue(value_paise=100, label="x", provenance=[])


def _traced(value: int) -> TracedValue:
    return TracedValue(
        value_paise=value,
        label="x",
        provenance=[Provenance(source_table="t", source_id="1", field="f")],
    )


def test_explanation_cannot_be_resolved_with_nonzero_residual():
    with pytest.raises(Exception):
        Explanation(
            settlement_id="s1",
            question="q",
            lines=[_traced(100)],
            total=_traced(100),
            residual_paise=5,
            resolved=True,
        )


def test_explanation_resolved_with_zero_residual_is_fine():
    e = Explanation(
        settlement_id="s1",
        question="q",
        lines=[_traced(100)],
        total=_traced(100),
        residual_paise=0,
        resolved=True,
    )
    assert e.resolved
