"""The single command that proves Hisaab works.

    python -m eval.run --seed 42 --questions eval/questions.yaml

Builds the ledger from the generator, runs every question through the
engine + llm pipeline, scores against ground truth, prints a report and
writes eval/report.md.

Contract this expects the other terminals to expose (imported lazily so
this module still loads while they're incomplete):

    hisaab.generate.build_ledger(seed: int) -> Ledger
    hisaab.llm.parse_intent(question: str) -> Intent   # Intent has .intent: str
    hisaab.engine.explain(ledger, intent) -> Explanation
    hisaab.llm.narrate(explanation: Explanation) -> str

A question is "refused" when parse_intent returns intent == "unsupported"
or explain() returns an Explanation with resolved == False.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from eval.metrics import QResult, Report, exit_code, render_report, unsupported_numbers

_NOT_READY = (ImportError, AttributeError, NotImplementedError)
REPORT_PATH = Path(__file__).with_name("report.md")


def _load_questions(path: str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise SystemExit(f"{path}: expected a non-empty YAML list of questions")
    return data


def _run_one(q: dict, ledger) -> QResult:
    """One question through the full pipeline. Never raises for a pipeline
    that IS ready — a per-question failure is recorded as its exception_reason.
    Re-raises _NOT_READY so the caller can abort with one clear message."""
    from hisaab.engine import explain
    from hisaab.llm import narrate, parse_intent

    qid = str(q["id"])
    question = str(q["question"])
    answerable = bool(q.get("answerable", True))
    expected_paise = q.get("expected_paise")

    t0 = time.perf_counter()
    got_intent = None
    got_paise = None
    resolved = True
    exception_reason = None
    try:
        intent = parse_intent(question)
        got_intent = getattr(intent, "intent", None)
        explanation = explain(ledger, intent)
        resolved = bool(explanation.resolved)
        got_paise = explanation.total.value_paise
        narration = narrate(explanation)
        unsupported = unsupported_numbers(narration, explanation)
        if not resolved:
            exception_reason = explanation.exception_reason or "unresolved (no reason given)"
    except _NOT_READY:
        raise
    except Exception as exc:  # real per-question bug: record, keep going
        resolved = False
        unsupported = []
        exception_reason = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - t0) * 1000

    return QResult(
        qid=qid,
        question=question,
        expected_intent=str(q.get("intent", "")),
        got_intent=got_intent,
        answerable=answerable,
        expected_paise=expected_paise,
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
        "The engine + llm pipeline is still being written. This command will\n"
        "pass once these callables exist and match the contract:\n\n"
        "  hisaab.generate.build_ledger(seed) -> Ledger\n"
        "  hisaab.llm.parse_intent(question) -> Intent   # .intent: str\n"
        "  hisaab.engine.explain(ledger, intent) -> Explanation\n"
        "  hisaab.llm.narrate(explanation) -> str\n"
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
        from hisaab.generate import build_ledger

        ledger = build_ledger(args.seed)
        results = [_run_one(q, ledger) for q in questions]
    except _NOT_READY as exc:
        _not_ready_exit(exc)
        return

    rep = Report(seed=args.seed, n_settlements=len(ledger.settlements), results=results)
    text = render_report(rep)
    print(text)
    REPORT_PATH.write_text(text, encoding="utf-8")
    raise SystemExit(exit_code(rep))


if __name__ == "__main__":
    main()
