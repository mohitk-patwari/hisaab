HISAAB EVAL — PIPELINE NOT READY

  ImportError: cannot import name 'build_ledger' from 'hisaab.generate' (C:\dev\razorpay_buildathon\hisaab-t3\hisaab\generate\__init__.py)

The engine + llm pipeline is still being written. This command will
pass once these callables exist and match the contract:

  hisaab.generate.build_ledger(seed) -> Ledger
  hisaab.llm.parse_intent(question) -> Intent   # .intent: str
  hisaab.engine.explain(ledger, intent) -> Explanation
  hisaab.llm.narrate(explanation) -> str
