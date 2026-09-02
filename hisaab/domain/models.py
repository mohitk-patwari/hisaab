"""Frozen data contracts for Hisaab.

Money is ALWAYS integer paise. Never float. These models do not change
without telling both other terminals coding against them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Paise = int


def to_paise(rupees_str: str) -> Paise:
    """Parse a decimal rupee string ("123.45") into integer paise exactly.

    Uses Decimal so no float rounding ever touches money.
    """
    try:
        rupees = Decimal(rupees_str)
    except InvalidOperation as exc:
        raise ValueError(f"not a valid decimal amount: {rupees_str!r}") from exc
    paise = rupees * 100
    if paise != paise.to_integral_value():
        raise ValueError(f"amount has sub-paise precision: {rupees_str!r}")
    return int(paise)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Payment(_Frozen):
    payment_id: str
    order_id: str
    captured_at_utc: datetime
    gross_paise: Paise
    method: str


class Refund(_Frozen):
    refund_id: str
    payment_id: str
    created_at_utc: datetime
    amount_paise: Paise


class Fee(_Frozen):
    payment_id: str
    fee_paise: Paise
    tax_paise: Paise


AdjustmentKind = Literal[
    "chargeback_debit",
    "chargeback_reversal",
    "reserve_hold",
    "reserve_release",
    "dispute_fee",
    "manual_correction",
]


class Adjustment(_Frozen):
    adjustment_id: str
    settlement_id: str
    kind: AdjustmentKind
    amount_paise: Paise
    note: str


class Settlement(_Frozen):
    settlement_id: str
    utr: str
    settled_at_utc: datetime
    net_paise: Paise
    status: str


class Provenance(_Frozen):
    """Where a number came from: one source row and field."""

    source_table: str
    source_id: str
    field: str


class TracedValue(_Frozen):
    value_paise: Paise
    label: str
    provenance: list[Provenance]

    @field_validator("provenance")
    @classmethod
    def _require_provenance(cls, v: list[Provenance]) -> list[Provenance]:
        if not v:
            raise ValueError("TracedValue requires at least one Provenance entry")
        return v


class Explanation(_Frozen):
    settlement_id: str
    question: str
    lines: list[TracedValue]
    total: TracedValue
    residual_paise: Paise
    resolved: bool
    exception_reason: str | None = None

    @model_validator(mode="after")
    def _resolved_matches_residual(self) -> "Explanation":
        if self.residual_paise != 0 and self.resolved:
            raise ValueError("resolved must be False whenever residual_paise != 0")
        return self


class Ledger(BaseModel):
    """All records for a merchant, with index lookups by id/foreign key."""

    model_config = ConfigDict(frozen=True)

    payments: list[Payment] = Field(default_factory=list)
    refunds: list[Refund] = Field(default_factory=list)
    fees: list[Fee] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    settlements: list[Settlement] = Field(default_factory=list)

    @property
    def payments_by_id(self) -> dict[str, Payment]:
        return {p.payment_id: p for p in self.payments}

    @property
    def refunds_by_payment_id(self) -> dict[str, list[Refund]]:
        out: dict[str, list[Refund]] = {}
        for r in self.refunds:
            out.setdefault(r.payment_id, []).append(r)
        return out

    @property
    def fee_by_payment_id(self) -> dict[str, Fee]:
        return {f.payment_id: f for f in self.fees}

    @property
    def adjustments_by_settlement_id(self) -> dict[str, list[Adjustment]]:
        out: dict[str, list[Adjustment]] = {}
        for a in self.adjustments:
            out.setdefault(a.settlement_id, []).append(a)
        return out

    @property
    def settlements_by_id(self) -> dict[str, Settlement]:
        return {s.settlement_id: s for s in self.settlements}
