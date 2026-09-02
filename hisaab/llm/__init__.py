"""LLM layer: intent parsing (words -> query) and narration (result -> words).

Both go through hisaab.llm.providers.complete(); nothing here or in intent.py /
narrate.py opens a socket itself. With no provider configured, each falls back
to a deterministic stub so the whole pipeline still runs offline.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # make .env visible to providers._decide() before it reads os.environ
