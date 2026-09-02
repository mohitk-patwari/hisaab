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
