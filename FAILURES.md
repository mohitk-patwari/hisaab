# Failures

Honest log of what didn't work, what was cut, and what's known-broken at
submission time. Updated as we go, not backfilled at the end.

## llm/ + cli wiring (t2-engine)

- Raw `httpx` POST to `/v1/messages` instead of the `anthropic` SDK — SDK isn't
  in requirements.txt and the scaffold ships `httpx`. One-shot, no streaming.
- No API key path is exercised in tests (no key available). The offline
  deterministic stubs are what the tests and `make run` cover.
- `cli.py` loads `data/ledger.json` if present, else a hardcoded demo ledger.
  Real CSV loading belongs to whoever owns data I/O, not the engine terminal.
- The gate extracts every money-shaped token. A narration that mentions a row
  *count* ("built from 2 payments") would be flagged as unsupported. Mitigated
  by instructing the narrator not to emit counts; not structurally prevented.

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

## `make eval` seam repair (2026-09-02) — merge of t1/t2/t3 into main

Four real bugs at the seams between terminals, found by actually running
`make eval` end to end. Fixed the first three; left the fourth alone on
purpose.

1. **FAILURES.md merge conflict.** t2 and t3 both appended to this file from
   a shared base; git left conflict markers. Resolved by keeping both
   sections (no content lost).

2. **`eval/run.py` imported a contract that was never the real one.**
   `_run_one` did `from hisaab.engine import explain` and
   `from hisaab.llm import narrate, parse_intent` — a placeholder shape
   written before t2's engine/llm existed. The real shape (already correct
   in `cli.py`, which t2 wired directly) is
   `hisaab.engine.queries.HANDLERS[intent.handler](ledger, intent.query_params())`,
   `hisaab.llm.intent.parse`, `hisaab.llm.narrate.narrate`. `make eval` was
   exiting 1 with `PIPELINE NOT READY` before a single question ran. Rewrote
   `_run_one` (and the stale docstring/error message) against the real
   contract.

3. **The offline intent parser couldn't see the generator's own settlement
   ids.** `hisaab/llm/intent.py`'s regex-based fallback (`_stub_parse`, used
   because no `ANTHROPIC_API_KEY` is configured) only recognized `setl_` /
   `settlement` / bare `s\d+`. `hisaab/generate/ledger.py` actually emits
   `stl_XXXX`. Every hand-built test ledger in `tests/test_decompose.py` and
   `tests/test_gate.py` (and `cli.py`'s demo ledger) happens to use
   `setl_`-style ids, so 31/31 unit tests were green while the real
   generator's ids parsed to *nothing* — every question in the eval set
   refused with "did not map to a known query". Added `stl` to the regex's
   prefix alternation.

4. **False "hallucinations" from a regex, not the LLM.** With (2) and (3)
   fixed, `UNSUPPORTED NUMBERS` still read 162/300. Root cause:
   `decompose()`'s `exception_reason` is debug-style text
   (`"stated_net=6247711, computed_net=10988721, ..."`), and when a
   settlement doesn't reconcile that text gets embedded verbatim in the
   narration. Both `eval/metrics.py`'s and `hisaab/llm/gate.py`'s
   money-token regexes used `\d[\d,]*` for "digits with thousands
   commas", which happily swallows a **sentence comma right after a bare
   number** — `6247711,` got misread as `6,247,711` and flagged as an
   invented figure never in the trace. Tightened both regexes so a comma
   only counts as grouping when exactly 3 digits follow it. After the fix,
   `UNSUPPORTED NUMBERS` is 0/300 — the gate was never actually leaking
   invented numbers; the scoring regex was inventing false positives.

**Left alone on purpose:** the window/construction mismatch predicted above
(#3 in the previous section). It fires exactly as predicted — see the eval
numbers below — and fixing it means either adding a `settlement_id` FK to
the frozen domain contract or changing how the generator assigns capture
times, both real design decisions, not seam repair. Flagging for a decision,
not fixing unilaterally.

### Eval results — seeds 42 / 7 / 1337 (`eval/questions.yaml` regenerated
per seed via `eval.gen_questions`, since expected answers are baked from
that seed's GroundTruth)

| metric | seed 42 | seed 7 | seed 1337 |
|---|---|---|---|
| Intent classification | 223/300 (74.3%) | 223/300 (74.3%) | 223/300 (74.3%) |
| Answer numerically correct | 36/240 (15.0%) | 43/240 (17.9%) | 42/240 (17.5%) |
| Wrong answer | 109/240 (45.4%) | 102/240 (42.5%) | 103/240 (42.9%) |
| WRONG refusal | 95/240 (39.6%) | 95/240 (39.6%) | 95/240 (39.6%) |
| UNSUPPORTED NUMBERS | 0/300 | 0/300 | 0/300 |
| Correct refusals | 60/60 (100%) | 60/60 (100%) | 60/60 (100%) |
| Answered the unanswerable | 0/60 | 0/60 | 0/60 |
| Mean latency | 3 ms | 2 ms | 2 ms |

**Stable across seeds** — intent classification and WRONG refusal are
*identical* to the digit at all three seeds; answer/wrong-answer vary by
only a few points (sampling noise across which settlements the question
generator happens to pick), and refusal correctness is perfect at all three.
The dominant number, WRONG refusal at ~40% of answerable questions, is not
seed variance — it is the window/construction mismatch above, reproducing
at essentially the same rate regardless of seed because it's a structural
property of every settlement's capture-time distribution, not a fluke of
one random draw. UNSUPPORTED NUMBERS at a clean 0/300 on every seed is the
one genuinely good news number: once the scoring regex bug was fixed, the
gate has never let an invented figure through in 900 question-runs.
