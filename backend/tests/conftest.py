"""Socle commun à TOUTE la suite de tests AgriConnect.

Philosophie (identique à tests/chaos) : AUCUN réseau, AUCUN LLM réel, AUCUNE
base de données. Tout ce qui sort du process est simulé par des doublures
déterministes, pour que la suite soit rejouable en CI, hors ligne, en < 1 min.

Exécution :
    cd backend && python -m pytest tests -q
    cd backend && python -m pytest tests/unit -q          # rapide
    cd backend && python -m pytest tests -m "not slow" -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Rendre `agriconnect` importable sans installation editable.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def run(coro):
    """Exécute une coroutine dans une boucle fraîche (pas besoin de pytest-asyncio)."""
    return asyncio.run(coro)


# =====================================================================
# DOUBLURES LLM
# =====================================================================

class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str, model: Optional[str] = None) -> None:
        self.choices = [_Choice(content)]
        self.model = model


class ScriptedLLM:
    """LLM déterministe : renvoie TOUJOURS le payload JSON fourni.

    Sert à prouver le comportement du pipeline FACE À une sortie LLM donnée
    (y compris une sortie FAUSSE — cf. l'ancrage d'unité).

    Par défaut, `completion.model` fait écho au `model=` demandé par
    l'appelant (simule "le modèle principal a répondu", comme en prod hors
    repli Groq) — passer `respond_as_model=` pour simuler un repli dégradé
    (`interpreter/routing.py` compare `completion.model` au modèle demandé
    pour détecter ce cas — voir `nodes/memory.py::_degraded_model_response`).
    """

    def __init__(self, payload: Dict[str, Any], respond_as_model: Optional[str] = None) -> None:
        self.payload = payload
        self.calls = 0
        self._respond_as_model = respond_as_model

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        model = self._respond_as_model if self._respond_as_model is not None else kwargs.get("model")
        return _Completion(json.dumps(self.payload), model=model)


class ForbiddenLLM:
    """Lève si on l'appelle — prouve qu'un chemin déterministe NE consulte PAS le LLM."""

    class Called(AssertionError):
        pass

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        raise ForbiddenLLM.Called("Le LLM a été appelé alors que le chemin doit être déterministe")


# =====================================================================
# RUNTIMES
# =====================================================================

class StubRuntime:
    """Runtime minimal : LLM injectable + call_db qui enregistre les appels."""

    def __init__(self, llm: Any = None, responses: Optional[Dict[str, Any]] = None) -> None:
        self.llm = llm
        self.model_answer = "test-model"
        self.calls: List[str] = []
        self._responses = responses or {}

    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        self.calls.append(tool_name)
        if tool_name in self._responses:
            return self._responses[tool_name]
        return {"status": "success", "data": {}, "message": f"{tool_name} ok"}


@pytest.fixture()
def stub_runtime():
    def _make(llm: Any = None, responses: Optional[Dict[str, Any]] = None) -> StubRuntime:
        return StubRuntime(llm=llm, responses=responses)
    return _make


@pytest.fixture()
def scripted_llm():
    def _make(**payload: Any) -> ScriptedLLM:
        return ScriptedLLM(payload)
    return _make


@pytest.fixture()
def forbidden_llm() -> ForbiddenLLM:
    return ForbiddenLLM()


# =====================================================================
# HELPERS D'ÉTAT
# =====================================================================

def make_state(**overrides: Any) -> Dict[str, Any]:
    """État MarketCoach minimal et VALIDE, surchargeable par mot-clé."""
    base: Dict[str, Any] = {
        "normalized_text": "",
        "user_query": "",
        "expected_input": "NONE",
        "current_goal": None,
        "detected_intent": "UNKNOWN",
        "interpreted_event": "UNKNOWN",
        "interpreter_confidence": 0.0,
        "working_memory": {},
        "transaction_payload": {},
        "extracted_entities": {},
        "user_role": "PRODUCER",
        "user_phone": "+22670000000",
        "status": "",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def state_factory():
    return make_state
