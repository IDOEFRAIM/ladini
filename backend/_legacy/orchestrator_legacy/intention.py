"""Small shim to provide AgriScopeChecker for tests and lightweight runs.

This module provides a minimal, dependency-free implementation used by unit
tests when the full LLM stack is not available. It is intentionally simple
and keyword-based so it is deterministic for CI and local dev without heavy
external dependencies.
"""
from typing import Dict, Any


class AgriScopeChecker:
    """Simple scope checker using keyword heuristics.

    The real implementation relies on an LLM; this shim mirrors the public
    interface sufficiently for unit tests.
    """
    AGRI_KEYWORDS = [
        "météo", "météo", "sem", "semis", "engrais", "maïs", "coton",
        "agro", "chenille", "traiter", "prix", "bobo", "ouaga", "ouagadougou",
    ]

    def __init__(self, llm_client: Any = None):
        self.llm = llm_client

    def check_scope(self, text: str) -> Dict[str, Any]:
        """Return a dict with `is_agricultural`, `confidence`, and `reason`."""
        t = text.lower()
        score = 0
        for k in self.AGRI_KEYWORDS:
            if k in t:
                score += 1
        is_agri = score > 0
        confidence = min(0.5 + 0.1 * score, 0.99) if is_agri else 0.15
        reason = "keyword-match" if is_agri else "no-keywords-found"
        return {"is_agricultural": is_agri, "confidence": confidence, "reason": reason}
