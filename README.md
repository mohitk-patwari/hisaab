# Hisaab

Ask a plain-English question about a merchant's Razorpay settlements and get an
answer that shows exactly which payments, fees, refunds and adjustments it was
built from — down to the paisa.

## Why

Settlement reconciliation is usually a spreadsheet nobody trusts. Hisaab
answers questions like "why did I only receive ₹9,412 for this order?" with a
traced decomposition instead of a black-box number.

## Layout

- `hisaab/domain` — frozen data contracts (`models.py`). Money is always
  integer paise, never float.
- `hisaab/generate` — synthetic Razorpay-shaped data (payments, refunds,
  fees, adjustments, settlements).
- `hisaab/engine` — deterministic settlement math. No LLM calls in here.
- `hisaab/llm` — the only place that talks to an LLM: parsing a question into
  a structured intent, and narrating a computed `Explanation` in English.
- `hisaab/cli.py` — entrypoint. Ask a question, print the traced answer.
- `eval/` — scored questions with expected answers, for honest measurement.
- `tests/` — unit tests for domain and engine.
- `data/` — generated CSVs (gitignored, regenerate with `hisaab.generate`).

## Setup

```
make install
```

## Run

```
make run
```

## Test

```
make test
```

## Design rule

The engine computes; the LLM only parses questions and narrates answers it is
handed. Every number in an `Explanation` traces back to a source row via
`Provenance`. See `hisaab/domain/models.py` for the contracts.
