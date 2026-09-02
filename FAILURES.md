# Failures

Honest log of what didn't work, what was cut, and what's known-broken at
submission time. Updated as we go, not backfilled at the end.

## eval can't produce scores yet — `hisaab.llm` not written

`python -m eval.run --seed 42 --questions eval/questions.yaml` exits 1 with
PIPELINE NOT READY. Done and tested: the 300-question set (`eval/questions.yaml`,
generated from the ledger's GroundTruth so it survives a seed change),
`eval/metrics.py` (intent / correct answer / **wrong answer** / **wrong refusal**
/ correct refusal / answered-the-unanswerable / unsupported-numbers, each with
its denominator). Blocked on:

- `hisaab.engine.explain(ledger, intent)` — a dispatcher over
  `hisaab.engine.queries.HANDLERS`. Not written.
- `hisaab.llm.parse_intent(question)` and `hisaab.llm.narrate(explanation)` —
  the whole llm layer. Not written.

## predicted failure mode once it does run — engine window vs generator construction

`hisaab/engine/decompose.py` assigns payments/refunds to a settlement by a
date window `(prev.settled_at, this.settled_at]`, because the domain model has
no `settlement_id` on `Payment`/`Refund`/`Fee`. The generator
(`hisaab/generate/ledger.py`) assigns them **by construction** with capture
times 1–2 days before the settlement at a random hour. The two do not agree:
a payment captured before 10:00 IST on the day before its settlement, or over a
Fri→Mon weekend, lands in the wrong window. So `decompose()` will carry a
non-zero residual for most settlements and `explain_settlement` questions
(expected: a clean `net_paise`) will score as **WRONG refusals** until a
`settlement_id` FK is added to the payment/refund/fee rows or the generator is
changed to keep captures inside the window. This is the number the eval exists
to surface.
