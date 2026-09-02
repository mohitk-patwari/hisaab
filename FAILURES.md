# Failures

Honest log of everything that broke, was cut, or is known-wrong at submission
time. One entry per failure — Symptom / Hypothesis / Fix / Metric delta —
kept whether or not it is fixed, and whether or not it is flattering. Numbers
are the real offline `python -m eval.run --seed 42` output (`eval/report.md`).

---

## F1 — eval written against a guessed pipeline contract

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

**Metric delta.** Eval went from *not executing at all* to running 300/300
questions.

---

## F2 — settlement-id regex didn't match the generator's id scheme

**Symptom.** First real run: `WRONG refusal 240/240 answerable`, `Intent
classification 28/300`. Every answerable question failed with
`intent.parse() returned None (question not mapped to a query)`.

**Hypothesis.** The offline stub parser's `_ID` regex was
`\b(?:setl|settlement)[_-]?\w+|\bs\d+\b` — the demo ledger's `setl_1` scheme.
The generator emits `stl_0000`. `"stl"` never matched `"setl"`, and
`"settlement stl_0010"` (word + space + id) matched neither branch, so no id was
ever extracted and the stub returned `None` for everything.

**Fix.** Broadened `_ID` to `\b(?:settlement\s+)?((?:se?tl|s)_?\d+)\b` and took
group 1 — now matches `stl_0000`, `setl_1`, `settlement stl_7`, `s3`. In
`hisaab/llm/intent.py` (the engine terminal's file; a one-line regex fix that
unblocked the entire eval).

**Metric delta.** `WRONG refusal` 240/240 → 72/240 · `Intent` 28/300 → 250/300 ·
`Answer numerically correct` 0 → 36/240. (Applied together with F3 in one run.)

---

## F3 — "GST" not recognised as the tax component

**Symptom.** ~22 straightforward questions ("how much GST did I pay on
settlement X") classified as `explain_settlement` instead of
`component_breakdown` / `tax`.

**Hypothesis.** The stub matched only the literal component tokens (`gross`,
`fees`, `tax`, `refunds`, `adjustments`). "GST" — the standard Indian term for
exactly that tax line — was not among them, so the question fell through to the
default handler.

**Fix.** Added `if "gst" in q: component = "tax"` ahead of the component loop in
`_stub_parse`.

**Metric delta.** Folded into F2's run, not independently measured; F2+F3
together took `Intent classification` from 28/300 to 250/300.

---

## F4 — engine window-assignment vs generator by-construction assignment *(not fixed)*

**Symptom.** 132 decompositions come back `resolved=False` with `computed_net`
roughly 1.5–3× `stated_net`. `explain_settlement` questions → 72 **WRONG
refusals**; `component_breakdown` questions → 93 **wrong answers**. The
`straightforward` bucket scores **15/180** correct answers. Representative row
from `eval/report.md`:

> `Q0094 "what is the net amount of settlement stl_0010" -> fees suspect: … the
> books are under-deducted (computed_net too high) by 9971155 paise.
> stated_net=5285012, computed_net=15256167, residual=-9971155 paise.`

**Hypothesis.** The frozen domain model puts `settlement_id` only on
`Adjustment`, not on `Payment`/`Fee`/`Refund`. So `hisaab/engine/decompose.py`
assigns a payment to the settlement whose half-open window
`(prev.settled_at_utc, this.settled_at_utc]` contains `captured_at_utc`. But
`hisaab/generate/ledger.py` assigns payments by construction, with
`captured_at = settlement_date - {1 or 2} days` at a **random hour**. A payment
captured before 10:00 IST the day before its settlement, or anywhere across a
Fri→Mon gap, lands in a neighbouring settlement's window. The engine's partition
of the payment set and the generator's partition differ, so almost every
`computed_net` is wrong and the residual is large.

**Fix.** *Not fixed.* The clean fix is a `settlement_id` foreign key on
`Payment`/`Fee`/`Refund` — then `_window()` collapses to an index lookup and the
ordering logic is deleted. Alternative: constrain the generator to keep every
capture time strictly inside its settlement's window. Either touches code owned
by all three terminals and was out of scope for the eval commit. The engine's
own `# ponytail:` comment already anticipates this.

**Metric delta.** Attributable: ~72 of 72 WRONG refusals and ~90 of 132 wrong
answers. Projected fix: `Answer numerically correct` 36/240 → ~200/240.

---

## F5 — diagnostic strings leak raw paise into the narration *(not fixed)*

**Symptom.** `UNSUPPORTED NUMBERS 57/300` — every one on an unresolved
decomposition, all in the `straightforward` bucket. Exit code 1.

**Hypothesis.** `_stub_narrate` (and the LLM narrator prompt) append
`explanation.exception_reason` to the narration. The engine's `_suspect()`
builds that string with bare integers: `"…by 9971155 paise. stated_net=5285012,
computed_net=15256167, residual=-9971155 paise."`. The gate scans the whole
narration string, finds integers that aren't any `value_paise` in the trace,
and flags them — correctly, by its own contract.

**Fix.** *Not fixed.* Options: `_suspect()` formats every figure as rupees that
are already trace lines; or `narrate()` receives a sanitized reason; or the gate
takes the reason string as an explicit allow-list. Independent of F4 — it would
persist even with perfect decomposition whenever a residual is non-zero.

**Metric delta.** This is the entire `UNSUPPORTED NUMBERS 57/300` and the reason
the run exits 1. Projected fix → ~0/300, exit 0.

---

## F6 — nonexistent settlement id raises instead of refusing *(not fixed)*

**Symptom.** 6 unanswerable questions naming `stl_9000`…`stl_9005` surface as
`KeyError: "no settlement 'stl_9000' in ledger"` rather than a structured
refusal.

**Hypothesis.** `decompose()` → `_window()` does `raise KeyError(...)` for an
unknown id and no query handler wraps it.

**Fix.** *Not fixed.* `run.py` catches the exception and records it, so the eval
outcome is still correct (6/6 correct refusals). A production `explain()` should
return an unresolved `Explanation` carrying the reason.

**Metric delta.** None on the score (`Correct refusals` stays 60/60);
robustness/UX only.

---

## F7 — offline stub can't parse temporal phrases *(not fixed)*

**Symptom.** ~25 ambiguous questions ("why were my tuesday settlements lower",
"why was january weak", "compare this tuesday to last tuesday") are classified
`unsupported` instead of `find_settlement_by_date` / `explain_delta`. `Intent
classification` in the `ambiguous` bucket: 15/40.

**Hypothesis.** The deterministic fallback only recognises an ISO `YYYY-MM-DD`
date. Weekday and month names are never mapped to a date, so the stub returns
`None`. The LLM path would likely handle them — but there is no API key in this
environment (F9).

**Fix.** *Not fixed.* Needs weekday/month/relative-week → date-range parsing.
The questions are still **refused correctly** (40/40); only the intent label and
the refusal *reason* are blunter than they should be.

**Metric delta.** Offline `Intent classification` ceiling is ~275/300; the last
~25 need the LLM or explicit date-phrase parsing.

---

## F8 — octopus merge left conflict markers in FAILURES.md

**Symptom.** `git merge t1-domain t2-engine` and a later `git merge t2-engine`
produced `<<<<<<< / ======= / >>>>>>>` markers in `FAILURES.md`; three branches
had each appended a section to the same trailing region.

**Hypothesis.** All three terminals edit the end of the same file from a shared
base commit, so any two non-trivial edits collide.

**Fix.** Resolved by rewriting the file (this document). Going forward: one
failure per commit, appended as its own `##` block, so git's 3-way merge can
place non-adjacent additions without a conflict.

**Metric delta.** None.

---

## F9 — the LLM path is never exercised *(cut)*

**Symptom.** Zero test coverage and zero eval coverage of the real
`/v1/messages` intent-parse and narration calls. Every number in
`eval/report.md` and the README is the deterministic offline fallback.

**Hypothesis.** No `ANTHROPIC_API_KEY` in the build environment;
`hisaab.llm.have_llm()` returns `False` and both `parse` and `narrate` take
their stub branch.

**Fix.** *Cut, not fixed.* Requires a key and a separate `eval.run` invocation.
The offline numbers are a floor, not a measurement of the product.

**Metric delta.** Unknown. LLM intent accuracy, narration quality, latency, and
token cost are all TODO in the README results table.

---

## F10 — raw httpx instead of the Anthropic SDK *(deliberate)*

**Symptom.** `hisaab/llm/__init__.py` hand-rolls a `httpx.post` to
`/v1/messages` with `anthropic-version` header parsing, instead of using the
`anthropic` SDK.

**Hypothesis.** The SDK is not in `requirements.txt`; the scaffold ships
`httpx`. Adding a dependency for one one-shot, non-streaming call wasn't worth
it.

**Fix.** Deliberate, left as is. If streaming, retries with backoff, or tool use
are ever needed, switch to the SDK.

**Metric delta.** None.

---

## F11 — the gate can't tell an amount from a count *(latent)*

**Symptom.** A narration such as "built from 2 payments" would have `2` flagged
as an unsupported number. Not observed in the current run.

**Hypothesis.** `gate.verify` extracts *every* money-shaped numeric token; a
bare integer that happens to be a row count is indistinguishable from a small
rupee amount.

**Fix.** Mitigated by instructing the narrator not to emit counts; not
structurally prevented. The stub narrator emits none, so the current run is
clean.

**Metric delta.** 0 observed; latent risk once the LLM narrator is in use.
