"""Generate eval/questions.yaml from the ledger + ground truth.

Every expected answer is read straight from GroundTruth (the generator's
answer key), never from the engine — so the question set stays correct when
the seed changes. Only the genuinely un-templatable phrasings (vague,
relative-date, no-data-concept) are hand-written below.

    python -m eval.gen_questions --seed 42 --out eval/questions.yaml

Distribution: 180 straightforward · 60 edge · 40 ambiguous · 20 unanswerable.
~15% of phrasings are Hinglish.
"""

from __future__ import annotations

import argparse
from datetime import timedelta, timezone
from pathlib import Path

import yaml

from hisaab.generate.ledger import generate

IST = timezone(timedelta(hours=5, minutes=30))


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def _q(qid, question, bucket, intent, answerable, expected_paise, hinglish, reason=None):
    return {
        "id": qid,
        "question": question,
        "bucket": bucket,
        "intent": intent,
        "answerable": answerable,
        "expected_paise": expected_paise,
        "expected_reason": reason,
        "hinglish": hinglish,
    }


# (english, hinglish, intent, truth-key) — truth-key picked from SettlementTruth,
# except "net"/"largest"/"delta" which are computed below.
_STRAIGHT_TEMPLATES = [
    ("what were the fees on settlement {sid}", "{sid} pe fees kitni lagi bhai",
     "component_breakdown", "fee_total_paise"),
    ("how much GST did I pay on settlement {sid}", "{sid} ka GST kitna tha",
     "component_breakdown", "tax_total_paise"),
    ("total refunds in settlement {sid}", "{sid} me refund total kitna hua",
     "component_breakdown", "refund_total_paise"),
    ("what was the gross payment volume for settlement {sid}", "{sid} me total payments kitne the",
     "component_breakdown", "gross_total_paise"),
    ("what is the net amount of settlement {sid}", "{sid} ka net kitna aaya",
     "explain_settlement", "net_paise"),
    ("why did I only receive {net} in settlement {sid}", "bhai {sid} me sirf {net} kyun aaya",
     "explain_settlement", "net_paise"),
    ("what was the biggest deduction from settlement {sid}", "{sid} me sabse badi katauti kya thi",
     "largest_deduction", "largest"),
    ("what changed between settlement {sid_a} and settlement {sid_b}",
     "{sid_a} aur {sid_b} me kya farak tha", "explain_delta", "delta"),
]


def _largest_deduction_paise(ledger, truth) -> int:
    cands = [truth.fee_total_paise, truth.tax_total_paise, truth.refund_total_paise]
    cands += [-a.amount_paise for a in ledger.adjustments
              if a.settlement_id == truth.settlement_id and a.amount_paise < 0]
    return max(cands)


def _spread(n: int, count: int) -> list[int]:
    """`count` indices spread across range(n), deterministic."""
    step = max(1, n // count)
    return [i for i in range(0, n, step)][:count]


def build(seed: int) -> list[dict]:
    ledger, gt = generate(seed)
    truths = gt.by_settlement
    sids = [s.settlement_id for s in ledger.settlements]
    by_sid = {s.settlement_id: s for s in ledger.settlements}
    n = len(sids)
    out: list[dict] = []
    hcount = 0

    def hinglish_now() -> bool:
        nonlocal hcount
        use = (len(out) % 7 == 0)
        hcount += use
        return use

    # ---- 180 straightforward ----
    picks = _spread(n, 23)
    for ti, (en, hi, intent, key) in enumerate(_STRAIGHT_TEMPLATES):
        for pi in picks:
            if len([q for q in out if q["bucket"] == "straightforward"]) >= 180:
                break
            sid = sids[pi]
            t = truths[sid]
            hg = hinglish_now()
            if key == "delta":
                sid_b = sids[min(pi + 7, n - 1)]
                if sid_b == sid:
                    sid_b = sids[max(pi - 7, 0)]
                tb = truths[sid_b]
                exp = tb.net_paise - t.net_paise
                text = (hi if hg else en).format(sid_a=sid, sid_b=sid_b)
            elif key == "largest":
                exp = _largest_deduction_paise(ledger, t)
                text = (hi if hg else en).format(sid=sid)
            elif key == "net_paise" and "{net}" in en:
                exp = t.net_paise
                text = (hi if hg else en).format(sid=sid, net=_rupees(t.net_paise))
            else:
                exp = getattr(t, key)
                text = (hi if hg else en).format(sid=sid)
            out.append(_q(f"Q{len(out) + 1:04d}", text, "straightforward", intent, True, exp, hg))

    # ---- 60 edge cases (predicate-selected settlements) ----
    adj_sids = [sid for sid in sids if truths[sid].adjustment_ids]
    pos_adj_sids = [sid for sid in sids if truths[sid].adjustment_total_paise > 0]
    zero_refund_sids = [sid for sid in sids if truths[sid].refund_total_paise == 0]
    monday_sids = [sid for sid in sids
                   if by_sid[sid].settled_at_utc.astimezone(IST).date().weekday() == 0]

    edge_specs = [
        # (sids, count, english, hinglish, intent, truth-fn, note)
        (adj_sids, 16,
         "why is settlement {sid}'s net not simply gross minus fees minus tax",
         "{sid} ka net gross - fees - tax se alag kyun hai",
         "explain_settlement", lambda t: t.net_paise, "adjustments must be applied"),
        (pos_adj_sids, 12,
         "how much did adjustments add to settlement {sid}",
         "{sid} me adjustments ne kitna paisa joda",
         "component_breakdown", lambda t: t.adjustment_total_paise, "adjustment credited, not debited"),
        (zero_refund_sids, 12,
         "how much was refunded in settlement {sid}",
         "{sid} me kitna refund hua",
         "component_breakdown", lambda t: t.refund_total_paise, "answer is 0, not a refusal"),
        (monday_sids, 12,
         "break down the fees for settlement {sid}",
         "{sid} ki fees ka hisaab do",
         "component_breakdown", lambda t: t.fee_total_paise, "payment window spans a weekend"),
        (sids, 8,
         "what is the exact fee total for settlement {sid}, to the paisa",
         "{sid} ki poori fee, ek ek paisa",
         "component_breakdown", lambda t: t.fee_total_paise, "sum of rounded per-payment fees != 2% of gross"),
    ]
    for pool, count, en, hi, intent, fn, note in edge_specs:
        for sid in pool[:count]:
            hg = hinglish_now()
            out.append(_q(f"Q{len(out) + 1:04d}", (hi if hg else en).format(sid=sid),
                          "edge", intent, True, fn(truths[sid]), hg, reason=note))

    # ---- 40 ambiguous — MUST be refused ----
    for spec in _AMBIGUOUS:
        out.append(_q(f"Q{len(out) + 1:04d}", spec["q"], "ambiguous", spec["intent"],
                      False, None, spec.get("hinglish", False), reason=spec["reason"]))

    # ---- 20 unanswerable — data genuinely absent, MUST be refused ----
    for i in range(6):  # nonexistent settlement ids
        out.append(_q(f"Q{len(out) + 1:04d}",
                      f"what were the fees on settlement stl_{9000 + i:04d}",
                      "unanswerable", "component_breakdown", False, None, False,
                      reason="no such settlement in the ledger"))
    for d in ["2024-01-06", "2024-01-07", "2024-05-04", "2023-12-25", "2030-06-01", "2019-07-01"]:
        out.append(_q(f"Q{len(out) + 1:04d}",
                      f"which settlement settled on {d}",
                      "unanswerable", "find_settlement_by_date", False, None, False,
                      reason=f"no settlement settled on {d}"))
    for spec in _NO_DATA:
        out.append(_q(f"Q{len(out) + 1:04d}", spec["q"], "unanswerable", "unsupported",
                      False, None, spec.get("hinglish", False), reason=spec["reason"]))

    # renumber sequentially (edge/ambiguous counts are fixed, but be safe)
    for i, q in enumerate(out, 1):
        q["id"] = f"Q{i:04d}"

    _check_distribution(out, hcount)
    return out


_AMBIGUOUS = [
    {"q": "why were my tuesday settlements lower than usual", "intent": "find_settlement_by_date",
     "reason": "50 settlements fall on a Tuesday, no single one to explain"},
    {"q": "bhai monday ka payout kam kyun aata hai", "intent": "find_settlement_by_date",
     "reason": "every Monday settlement matches, ambiguous", "hinglish": True},
    {"q": "why is wednesday always low", "intent": "find_settlement_by_date",
     "reason": "matches all Wednesday settlements"},
    {"q": "mangalwar wala settlement low kyun hai", "intent": "find_settlement_by_date",
     "reason": "Tuesday matches many settlements", "hinglish": True},
    {"q": "what happened to friday's money", "intent": "find_settlement_by_date",
     "reason": "no unique Friday settlement"},
    {"q": "why did thursday underperform", "intent": "find_settlement_by_date",
     "reason": "many Thursday settlements match"},
    {"q": "why were my january settlements short", "intent": "find_settlement_by_date",
     "reason": "23 settlements in January, none singled out"},
    {"q": "february me payout kam kyun tha", "intent": "find_settlement_by_date",
     "reason": "whole month is ambiguous", "hinglish": True},
    {"q": "why was march weak", "intent": "find_settlement_by_date", "reason": "many March settlements"},
    {"q": "explain the dip in april", "intent": "find_settlement_by_date", "reason": "many April settlements"},
    {"q": "why did may collect less", "intent": "find_settlement_by_date", "reason": "many May settlements"},
    {"q": "june ka hisaab kam kyun lag raha hai", "intent": "find_settlement_by_date",
     "reason": "whole month ambiguous", "hinglish": True},
    {"q": "why was last friday's settlement short", "intent": "find_settlement_by_date",
     "reason": "'last friday' is relative to an unknown 'now'"},
    {"q": "compare this tuesday to last tuesday", "intent": "explain_delta",
     "reason": "neither Tuesday resolves to a single settlement"},
    {"q": "is week ka settlement pichle week se kam kyun hai", "intent": "explain_delta",
     "reason": "relative week, no anchor date", "hinglish": True},
    {"q": "why is my latest week lower than my first week", "intent": "explain_delta",
     "reason": "'week' groups many settlements, not comparable one-to-one"},
    {"q": "which settlement had the big refund", "intent": "unsupported",
     "reason": "many settlements have large refunds, no unique referent"},
    {"q": "why is one of my payouts missing money", "intent": "unsupported",
     "reason": "no settlement identified"},
    {"q": "the settlement with the chargeback, why so low", "intent": "unsupported",
     "reason": "several settlements carry chargeback adjustments"},
    {"q": "bhai jo settlement kam aaya usme kya gadbad hai", "intent": "unsupported",
     "reason": "no settlement named", "hinglish": True},
    {"q": "why did my payout drop", "intent": "unsupported", "reason": "no settlement or period given"},
    {"q": "which day was worst for me", "intent": "unsupported",
     "reason": "no metric or settlement specified"},
    {"q": "why am I getting less than before", "intent": "unsupported", "reason": "no comparison anchor"},
    {"q": "settlement kam kyun aaya", "intent": "unsupported",
     "reason": "which settlement is unspecified", "hinglish": True},
    {"q": "what's wrong with my recent settlements", "intent": "unsupported",
     "reason": "'recent' spans many settlements"},
    {"q": "why do some fridays pay more than others", "intent": "find_settlement_by_date",
     "reason": "compares groups, not two settlements"},
    {"q": "explain the weekend gap in my payouts", "intent": "unsupported",
     "reason": "no settlements exist on weekends by design; nothing to explain"},
    {"q": "why is the start of the month always lower", "intent": "find_settlement_by_date",
     "reason": "'start of month' matches many settlements"},
    {"q": "which of my tuesdays lost the most to fees", "intent": "find_settlement_by_date",
     "reason": "ranking across all Tuesdays, no single settlement"},
    {"q": "kaunsa settlement sabse bekaar tha", "intent": "unsupported",
     "reason": "no ranking metric given", "hinglish": True},
    {"q": "why was that one big settlement so low", "intent": "unsupported",
     "reason": "'that one' is not identifiable"},
    {"q": "my numbers look off this quarter, why", "intent": "unsupported",
     "reason": "quarter spans ~65 settlements"},
    {"q": "why did the holiday week settle less", "intent": "find_settlement_by_date",
     "reason": "no holiday calendar; 'week' is ambiguous"},
    {"q": "compare my best and worst days", "intent": "explain_delta",
     "reason": "'best'/'worst' undefined, no settlements pinned"},
    {"q": "which settlement is the odd one out", "intent": "unsupported",
     "reason": "no criterion, no settlement identified"},
    {"q": "why is the mid-month payout smaller", "intent": "find_settlement_by_date",
     "reason": "'mid-month' matches many settlements"},
    {"q": "how come some weeks are lighter", "intent": "unsupported", "reason": "grouped by week, no anchor"},
    {"q": "why did last month feel low", "intent": "find_settlement_by_date",
     "reason": "'last month' relative and spans ~21 settlements"},
    {"q": "pichle hafte ka settlement kam tha kya", "intent": "explain_delta",
     "reason": "relative week, no anchor", "hinglish": True},
    {"q": "which tuesday should I worry about", "intent": "find_settlement_by_date",
     "reason": "all Tuesdays match, none singled out"},
]

_NO_DATA = [
    {"q": "which customer got the most refunds", "reason": "payments carry no customer identity"},
    {"q": "what card network did settlement stl_0042 use", "reason": "method is card/upi/etc, not network"},
    {"q": "how much interest did my reserve balance earn", "reason": "no interest data in the ledger"},
    {"q": "what is my lifetime GMV across every settlement", "reason": "no lifetime aggregate is tracked"},
    {"q": "what will my next settlement pay out", "reason": "future settlement does not exist yet"},
    {"q": "how many chargebacks are pending right now", "reason": "no pending/status field on adjustments"},
    {"q": "what settlement fee percentage is in my contract", "reason": "contract terms are not in the ledger"},
    {"q": "which city were the payments in settlement stl_0100 from", "reason": "no geography on payments"},
]


def _check_distribution(qs: list[dict], hcount: int) -> None:
    from collections import Counter

    buckets = Counter(q["bucket"] for q in qs)
    want = {"straightforward": 180, "edge": 60, "ambiguous": 40, "unanswerable": 20}
    assert dict(buckets) == want, f"distribution off: {dict(buckets)} != {want}"
    hg = sum(1 for q in qs if q["hinglish"])
    assert 38 <= hg <= 55, f"Hinglish share out of range: {hg}/300"
    assert len(qs) == 300, len(qs)
    ids = [q["id"] for q in qs]
    assert ids == sorted(ids) and len(set(ids)) == 300
    print(f"OK  300 questions  {dict(buckets)}  hinglish={hg} ({hg / 3:.0f}%)")


_HEADER = """\
# GENERATED by `python -m eval.gen_questions --seed {seed}` — do not hand-edit.
# Expected answers come from the generator's GroundTruth, so re-generate after
# any seed change. Hand-written phrasings live in eval/gen_questions.py.
#
# fields: id · question · bucket · intent (expected label) · answerable
#         expected_paise (null when not answerable) · expected_reason · hinglish
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="eval.gen_questions")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="eval/questions.yaml")
    args = ap.parse_args(argv)

    questions = build(args.seed)
    body = yaml.safe_dump(questions, sort_keys=False, allow_unicode=True, width=1000)
    Path(args.out).write_text(_HEADER.format(seed=args.seed) + body, encoding="utf-8")
    print(f"wrote {args.out}  ({len(questions)} questions, seed {args.seed})")


if __name__ == "__main__":
    main()
