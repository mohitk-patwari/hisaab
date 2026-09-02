# Failures

Honest log of everything that broke, was cut, or is known-wrong at submission
time. One entry per failure — Symptom / Hypothesis / Fix / Metric delta —
kept whether or not it is fixed, and whether or not it is flattering. Numbers
are the real offline `python -m eval.run --seed 42` output (`eval/report.md`).

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

**Symptom.** `python -m eval.run` exited 1 with `ImportError: cannot import name
'build_ledger' from 'hisaab.generate'`, then `... 'explain' from
'hisaab.engine'`, for two commits. No questions ran.

**Hypothesis.** The eval was built before the generator, engine, and llm
modules existed, so `run.py` imported names guessed from the README
(`hisaab.generate.build_ledger`, `hisaab.engine.explain`,
`hisaab.llm.parse_intent`).

**Fix.** Once the other terminals landed, re-pointed `run.py` at the real API:
`hisaab.generate.ledger.generate(seed) -> (Ledger, GroundTruth)`,
`hisaab.engine.queries.HANDLERS[handler](ledger, params)`,
`hisaab.llm.intent.parse`, `hisaab.llm.narrate.narrate`,
`hisaab.llm.gate.verify`. Imports stayed lazy (inside `_run_one`) so `eval/`
still imports while any piece is missing, with a "PIPELINE NOT READY" message
that prints the exact expected contract.

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

## domain contract unfrozen once — settlement_id FK (2026-09-02)

Fixes the root cause of the window/construction mismatch above, not the
symptom. Added `settlement_id: str | None` to `Payment`, `Refund`, `Fee`
(`None` = genuinely unsettled, not missing data) and `Ledger.payments_for` /
`refunds_for` / `fees_for` (cached indexed lookups — built once via
`functools.cached_property`, not rescanned per call; verified pydantic's
`frozen=True` doesn't block `cached_property`, and the cache doesn't affect
model equality). `hisaab/generate/ledger.py` now stamps `settlement_id` on
every row at construction, matching the membership its own `GroundTruth`
already recorded — the same information was already sitting in `truths[
sid].payment_ids`, just not on the row itself. All 8 edge cases updated to
stamp it too; the two "late refund" cases (2 and 7) stamp the refund with
the *later* settlement's id, not the original payment's, which is the
entire point of those cases.

Also added `_unsettle_some_payments`: ~2% of payments (independent 2%
draw per payment, so "~2%" not exactly 2%) are pulled back to
`settlement_id=None` after generation, along with their fee and any refund,
with the settlement they left recomputed exactly. Runs before edge-case
injection so the two passes can't collide. `GroundTruth.unsettled_payment_ids`
carries the list.

Verified at seeds 42/7/1337, with and without `--edge-cases`: identity holds
for all 250 settlements at every combination (`verify_identity`, still
recomputing from ledger rows, not trusting GroundTruth's own totals);
`payments_for`/`refunds_for`/`fees_for` match `GroundTruth` membership
exactly for all 250 settlements at seed 42 (zero mismatches); null
`settlement_id` rate is 1.79-1.85% across the three seeds, both modes.

**Not done here (next prompt, per scope):** `hisaab/engine/decompose.py`
still assigns by date window and ignores the new FK entirely — it doesn't
yet use it, so the 39.6% WRONG-refusal rate from the eval results above is
unchanged until decompose() is rewritten to use `payments_for`/`refunds_for`
/`fees_for` instead of the window. `hisaab/engine/` and `eval/` were not
touched in this change, as instructed.

## decompose() switched to the settlement_id FK (2026-09-02)

**Symptom:** 39.6% WRONG refusal, `Answer numerically correct` 36/240, across
every seed — see the eval table above.

**Root cause:** the original domain contract had no `settlement_id` on
`Payment`/`Refund`/`Fee` (only `Adjustment` carried it). `decompose()` had to
*infer* membership from a date window `(prev.settled_at, this.settled_at]`,
which never agreed with how the generator actually assigned rows (capture
times 1-2 days before settlement, at a random hour, sometimes crossing a
Fri→Mon weekend) — a structural mismatch between two independent
assumptions about the same data, not a bug in either one on its own.

**Fix:** `decompose()` now takes primary membership from
`ledger.payments_for(settlement_id)` / `refunds_for` / `fees_for` — the FK
added in the prior entry. The date-window code (`_window`, `_in_window`,
`_ordered_settlements`) is kept, but demoted to an explicit fallback that
runs *only* over rows with `settlement_id=None`: if one falls inside the
settlement's window, it's reported in `exception_reason` ("N payment(s)
captured in this window are still unsettled ... excluded from this net
pending settlement") but never added to the computed total — a null FK means
genuinely unsettled, not "assign me by best guess," so the fallback narrates,
it doesn't launder the row back in.

Also rewrote `_suspect()` (the reconciliation-failure namer): it used to emit
debug text like `"stated_net=6247711, computed_net=10988721, residual=
-4741010 paise"` directly into `exception_reason`, which flows verbatim into
narrations — this is what caused the phantom-hallucination regex bug two
entries up. Every branch now speaks in ₹-formatted prose (`_fmt_rupees`, a
local 2-line duplicate of `gate.py`'s `format_rupees` — engine doesn't import
llm) with no bare paise integers anywhere in a string that can reach a
narration.

**Verification beyond pytest:** ran `decompose()` against a fresh `generate(
42)` for all 250 settlements directly (not through eval) — 0 mismatches, all
resolved, every computed net equals `GroundTruth.net_paise` exactly. 32/32
tests pass (`tests/test_decompose.py` fixtures updated to stamp
`settlement_id`, the old "date windowing" test replaced with one for FK
membership and one for the new unsettled-fallback diagnostic).

**A stale fixture, found and fixed along the way:** the first `make eval`
run after this change showed `Answer numerically correct` at only 137/240 —
short of the "well above 200/240" expected. Traced it to `eval/questions.yaml`
being stale: its `expected_paise` values were baked before the prior entry's
`_unsettle_some_payments` existed, so ~20% of them no longer matched what
`generate(42)` produces today for the same seed (spot check: 39/198 answers
already drifted). Regenerated it (`python -m eval.gen_questions --seed 42`,
its own documented command) and reran.

**What's left, confirmed NOT a decompose() bug:** after the stale-fixture
fix, `Answer numerically correct` is 191/240, not "well above 200/240" as
expected. Diagnosed rather than declared close-enough: checked all 49
remaining wrong answers directly against `HANDLERS[intent.handler]` — every
single one is `expected_intent != got_handler` (0 cases where the same
handler ran and returned a wrong value). All 49 are `hisaab/llm/intent.py`'s
offline regex stub misrouting the question to the wrong query handler before
decompose() ever runs — e.g. "GST" not recognized as the `tax` component,
Hinglish "katauti"/"aur" not in the largest-deduction/delta keyword lists,
"why is settlement X's net not simply gross minus fees minus tax" matching
the `fees`/`tax` keywords and getting routed to `component_breakdown`
instead of `explain_settlement`. Separately, "Answered the unanswerable"
moved from 0/60 to 2/60: two `_NO_DATA` questions ("what card network...",
"which city...") name a real settlement id with no recognizable field
keyword, so the stub defaults them to `explain_settlement`, which now
resolves cleanly and answers confidently instead of refusing — a
pre-existing stub gap that used to be masked by the window bug (an
`explain_settlement` call almost never resolved before, so it never got the
chance to be *confidently* wrong). Not touched: out of scope for this entry
(`hisaab/llm/intent.py`, not `hisaab/engine/decompose.py`), left for a
follow-up.

### Metric delta — seed 42

| metric | before (window-based decompose) | after (FK-based decompose) |
|---|---|---|
| Intent classification | 223/300 | 223/300 (unchanged — llm/intent.py not touched) |
| Answer numerically correct | 36/240 (15.0%) | **191/240 (79.6%)** |
| Wrong answer | 109/240 (45.4%) | 49/240 (20.4%) — all 49 are intent misroutes, 0 value bugs |
| WRONG refusal | 95/240 (39.6%) | **0/240 (0%)** |
| UNSUPPORTED NUMBERS | 0/300 | 0/300 (unchanged) |
| Correct refusals | 60/60 (100%) | 58/60 (96.7%) |
| Answered the unanswerable | 0/60 | 2/60 — pre-existing stub gap, unmasked, see above |
| Mean latency | 3 ms | 0 ms |
