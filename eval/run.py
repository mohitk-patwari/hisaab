"""The single command that proves Hisaab works.

    python -m eval.run --seed 42 --questions eval/questions.yaml

Builds the ledger from the generator, runs every question through the real
pipeline (intent.parse -> engine query -> narrate -> gate.verify), scores
against the ground truth baked into questions.yaml, prints a report and
writes eval/report.md.

Contract this expects (imported lazily so this module still loads while the
llm layer is incomplete):

    hisaab.generate.ledger.generate(seed: int) -> (Ledger, GroundTruth)   [ready]
    hisaab.llm.intent.parse(question: str) -> Intent | None   # Intent has .handler: str
    hisaab.engine.queries.HANDLERS[intent.handler](ledger, intent.query_params()) -> Explanation
    hisaab.llm.narrate.narrate(explanation: Explanation) -> str

Ground truth is NOT taken from GroundTruth here — it is baked into
eval/questions.yaml by eval.gen_questions, so the engine is never scored
against a key it could also see.

A question is "refused" when parse() returns None (mapped to intent
"unsupported") or the handler returns an Explanation with resolved == False.
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
    from hisaab.engine.queries import HANDLERS
    from hisaab.llm.intent import parse
    from hisaab.llm.narrate import narrate

    qid, question = str(q["id"]), str(q["question"])
    answerable = bool(q.get("answerable", True))

    t0 = time.perf_counter()
    got_intent = None
    got_paise = None
    resolved = False
    exception_reason = None
    unsupported: list[int] = []
    try:
        intent = parse(question)
        got_intent = intent.handler if intent is not None else "unsupported"
        if intent is None:
            resolved = False
            exception_reason = "question did not map to a known query"
        else:
            explanation = HANDLERS[intent.handler](ledger, intent.query_params())
            resolved = bool(explanation.resolved)
            got_paise = explanation.total.value_paise
            narration = narrate(explanation)
            unsupported = unsupported_numbers(narration, explanation)
            if not resolved:
                exception_reason = explanation.exception_reason or "unresolved (no reason given)"
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
        "A required module is missing or doesn't match the contract this eval\n"
        "expects:\n\n"
        "  hisaab.generate.ledger.generate(seed) -> (Ledger, GroundTruth)   [ready]\n"
        "  hisaab.llm.intent.parse(question) -> Intent | None   # .handler: str\n"
        "  hisaab.engine.queries.HANDLERS[handler](ledger, params) -> Explanation\n"
        "  hisaab.llm.narrate.narrate(explanation) -> str\n"
    )
    print(msg)
    REPORT_PATH.write_text(msg, encoding="utf-8")
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    try:  # the ₹ sign and em-dash trip the default Windows console codepage
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="eval.run")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--questions", default="eval/questions.yaml")
    ap.add_argument("--offline", action="store_true", help="force the regex stub, ignore any provider key")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N questions (0 = all); for a smoke test")
    args = ap.parse_args(argv)

    from hisaab.llm import providers

    if args.offline:
        providers.force_offline(True)
    providers.log_decision(f"  |  seed {args.seed}  |  {args.questions}")

    questions = _load_questions(args.questions)
    if args.limit > 0:
        questions = questions[: args.limit]
        print(f"[hisaab] smoke: first {len(questions)} questions only", file=sys.stderr)

    try:
        from hisaab.generate.ledger import generate

        ledger, _ground_truth = generate(args.seed)  # key unused; see module docstring
        results = [_run_one(q, ledger) for q in questions]
    except _NOT_READY as exc:
        _not_ready_exit(exc)
        return

    rep = Report(seed=args.seed, n_settlements=len(ledger.settlements), results=results)
    text = render_report(rep)
    text += (
        f"\nPath                        {providers.describe()}"
        f"\nRate-limit fallbacks        {providers.rate_limit_fallbacks()}/{len(results)}"
        "   (429/5xx: retried, then stub)"
        f"\nConfig-error fallbacks      {providers.config_errors()}/{len(results)}"
        "   (4xx / empty response: not retried, see stderr)\n"
    )
    try:
        print(text)
    except UnicodeEncodeError:  # ₹ vs the default Windows console codepage
        print(text.encode("ascii", "replace").decode())
    REPORT_PATH.write_text(text, encoding="utf-8")
    raise SystemExit(exit_code(rep))


if __name__ == "__main__":
    main()
