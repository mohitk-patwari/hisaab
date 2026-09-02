"""The single command that proves Hisaab works.

    python -m eval.run --seed 42 --questions eval/questions.yaml

Builds the ledger from the generator, runs every question through the real
pipeline (intent.parse -> engine query -> narrate -> gate.verify), scores
against the ground truth baked into questions.yaml, prints a report and
writes eval/report.md.

Ground truth is NOT read from the generator's GroundTruth here — it is baked
into eval/questions.yaml by eval.gen_questions, so the engine is never scored
against a key it could also see.

A question counts as "refused" when intent.parse() returns None (-> intent
"unsupported") or the Explanation comes back resolved == False. The gate's
leftover numbers are counted separately as UNSUPPORTED NUMBERS.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import yaml

from eval.metrics import QResult, Report, exit_code, render_report

_NOT_READY = (ImportError, AttributeError, NotImplementedError)
REPORT_PATH = Path(__file__).with_name("report.md")


def _load_questions(path: str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise SystemExit(f"{path}: expected a non-empty YAML list of questions")
    return data


def _run_one(q: dict, ledger) -> QResult:
    """One question through the full pipeline. A per-question failure is
    recorded as its exception_reason; only _NOT_READY re-raises so the caller
    can abort with one clear message."""
    from hisaab.engine import queries
    from hisaab.llm.gate import verify
    from hisaab.llm.intent import parse
    from hisaab.llm.narrate import narrate

    qid, question = str(q["id"]), str(q["question"])
    answerable = bool(q.get("answerable", True))

    t0 = time.perf_counter()
    got_intent = None
    got_paise = None
    resolved = False
    exception_reason = None
    unsupported: list = []
    try:
        intent = parse(question)
        if intent is None:
            got_intent = "unsupported"
            exception_reason = "intent.parse() returned None (question not mapped to a query)"
        else:
            got_intent = intent.handler
            explanation = queries.HANDLERS[intent.handler](ledger, intent.query_params())
            resolved = bool(explanation.resolved)
            got_paise = explanation.total.value_paise
            _, unsupported = verify(narrate(explanation), explanation)
            if not resolved:
                exception_reason = explanation.exception_reason or "unresolved (no reason given)"
            elif unsupported:
                exception_reason = f"gate blocked unsupported number(s): {', '.join(unsupported)}"
    except _NOT_READY:
        raise
    except Exception as exc:  # real per-question bug: record, keep going
        exception_reason = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - t0) * 1000

    return QResult(
        qid=qid,
        question=question,
        expected_intent=str(q.get("intent", "")),
        got_intent=got_intent,
        answerable=answerable,
        expected_paise=q.get("expected_paise"),
        got_paise=got_paise,
        resolved=resolved,
        exception_reason=exception_reason,
        latency_ms=latency_ms,
        unsupported=unsupported,
    )


def _not_ready_exit(exc: Exception) -> None:
    msg = (
        "HISAAB EVAL — PIPELINE NOT READY\n\n"
        f"  {type(exc).__name__}: {exc}\n\n"
        "This command needs the whole pipeline importable:\n\n"
        "  hisaab.generate.ledger.generate(seed) -> (Ledger, GroundTruth)\n"
        "  hisaab.llm.intent.parse(question) -> Intent | None\n"
        "  hisaab.engine.queries.HANDLERS[handler](ledger, params) -> Explanation\n"
        "  hisaab.llm.narrate.narrate(explanation) -> str\n"
        "  hisaab.llm.gate.verify(narration, explanation) -> (text, violations)\n"
    )
    print(msg)
    REPORT_PATH.write_text(msg, encoding="utf-8")
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="eval.run")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--questions", default="eval/questions.yaml")
    args = ap.parse_args(argv)

    questions = _load_questions(args.questions)

    try:
        from hisaab.generate.ledger import generate

        ledger, _ground_truth = generate(args.seed)  # key unused; see module docstring
        results = [_run_one(q, ledger) for q in questions]
    except _NOT_READY as exc:
        _not_ready_exit(exc)
        return

    rep = Report(seed=args.seed, n_settlements=len(ledger.settlements), results=results)
    text = render_report(rep)
    try:
        print(text)
    except UnicodeEncodeError:  # ₹ vs the default Windows console codepage
        print(text.encode("ascii", "replace").decode())
    REPORT_PATH.write_text(text, encoding="utf-8")
    raise SystemExit(exit_code(rep))


if __name__ == "__main__":
    main()
