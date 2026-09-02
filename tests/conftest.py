"""Unit tests exercise the deterministic paths only -- never a live LLM.

Without this, a local .env with a real GEMINI_API_KEY makes tests/test_adversarial.py
fire real network calls (intent.parse is not patched there) and the suite hangs.
"""

import pytest

from hisaab.llm import providers


@pytest.fixture(autouse=True, scope="session")
def _force_offline_llm():
    providers.force_offline(True)
    yield
