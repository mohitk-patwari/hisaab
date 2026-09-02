HISAAB EVAL — PIPELINE NOT READY

  ImportError: cannot import name 'explain' from 'hisaab.engine' (C:\dev\razorpay_buildathon\hisaab-t3\hisaab\engine\__init__.py)

The llm layer is still being written. This command will pass once
these callables exist and match the contract:

  hisaab.generate.ledger.generate(seed) -> (Ledger, GroundTruth)   [ready]
  hisaab.llm.parse_intent(question) -> Intent   # .intent: str
  hisaab.engine.explain(ledger, intent) -> Explanation
  hisaab.llm.narrate(explanation) -> str
