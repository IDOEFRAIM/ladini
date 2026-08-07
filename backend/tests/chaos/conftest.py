"""Chaos suite — fixtures d'injection de pannes.

Philosophie : AUCUN accès réseau, AUCUN LLM réel, AUCUNE base de données.
Chaque fixture simule un mode de défaillance précis observé (ou anticipé)
en production : API LLM morte, transport MCP qui raise, enveloppes
corrompues, latence bloquante. Les tests prouvent que la tuyauterie Python
survit à TOUS ces modes sans exception brute ni état corrompu.

Exécution :
    cd backend && PYTHONPATH=src python -m pytest tests/chaos -q
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Rendre `agriconnect` importable sans installation editable.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def run(coro):
    """Exécute une coroutine dans une boucle fraîche (pas de pytest-asyncio)."""
    return asyncio.run(coro)


# =====================================================================
# LLM DÉFAILLANTS
# =====================================================================

class CrashingLLM:
    """Simule un 500/429 Groq : toute complétion lève immédiatement.

    Rupture prévenue : un incident fournisseur LLM ne doit JAMAIS remonter
    en exception brute jusqu'au webhook Twilio (= message perdu + retry
    Celery en boucle). L'agent doit dégrader en fallback statique.
    """

    class _Boom(Exception):
        pass

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self._exc = exc or CrashingLLM._Boom("APIStatusError: 500 upstream")

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        raise self._exc


class BlockingSleepLLM:
    """Simule la latence réseau : create() BLOQUE le thread (time.sleep).

    Rupture prévenue : un appel LLM synchrone exécuté SANS asyncio.to_thread
    gèlerait la boucle d'événements du worker Celery — tous les utilisateurs
    du process attendraient derrière un seul message. Le test heartbeat
    prouve que la boucle continue de tourner pendant l'appel.
    """

    def __init__(self, sleep_s: float = 0.6, reply: str = "Question générée ?") -> None:
        self.sleep_s = sleep_s
        self.reply = reply
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        time.sleep(self.sleep_s)

        class _Msg:
            content = self.reply

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


# =====================================================================
# RUNTIMES MCP DÉFAILLANTS
# =====================================================================

# Pré-étapes d'INFRASTRUCTURE déclenchées par l'exécuteur AVANT l'appel métier
# (validation d'identité par TaskHandler.ensure_profile). Elles ne font pas
# partie de ce que ces tests mesurent : les compteurs et les réponses scriptées
# portent sur l'appel OUTIL. Sans cette exclusion, chaque compteur est décalé
# de 1 et la 1re réponse scriptée est consommée par la pré-étape.
PRESTEP_TOOLS = frozenset({"identify_or_create_user"})


class FailingDBRuntime:
    """call_db lève une erreur transitoire (timeout/connection) à CHAQUE appel.

    Rupture prévenue : Postgres/MCP indisponible pendant un pic. L'exécuteur
    doit retenter (marqueurs transitoires) puis rendre un état ERROR propre,
    jamais un crash de nœud LangGraph.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self.exc = exc or ConnectionError("connection refused (chaos)")
        self.llm = None

    async def call_db(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        # La pré-étape échoue aussi (l'infra est morte) mais n'est pas comptée :
        # l'exécuteur la journalise et poursuit vers l'appel outil, qui porte la
        # logique de retry/backoff réellement testée ici.
        if tool_name not in PRESTEP_TOOLS:
            self.calls += 1
        raise self.exc


class EnvelopeDBRuntime:
    """call_db renvoie des enveloppes/formes arbitraires programmées par test.

    Rupture prévenue : le bug historique « Stock insuffisant » sans chiffres —
    une enveloppe {ok:False, data:{}} non déballée passait pour un succès vide.
    """

    def __init__(self, responses: List[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: List[str] = []
        self.llm = None

    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        # Les réponses scriptées ciblent l'appel OUTIL : la pré-étape
        # d'identité reçoit un succès neutre et ne consomme pas la file.
        if tool_name in PRESTEP_TOOLS:
            return {"status": "success", "data": {"id": "user-test"}}
        self.calls.append(tool_name)
        if self.responses:
            return self.responses.pop(0)
        return {"status": "success", "message": f"{tool_name} ok"}


class RecordingRuntime:
    """Runtime sain qui ENREGISTRE chaque appel — pour prouver qu'un chemin
    bloqué n'atteint JAMAIS la couche outil (zéro appel = preuve formelle)."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.llm = None

    async def call_db(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(tool_name)
        return {"status": "success", "message": f"{tool_name} ok"}


# =====================================================================
# FIXTURES
# =====================================================================

@pytest.fixture()
def crashing_llm() -> CrashingLLM:
    return CrashingLLM()


@pytest.fixture()
def blocking_llm() -> BlockingSleepLLM:
    return BlockingSleepLLM()


@pytest.fixture()
def failing_db_runtime() -> FailingDBRuntime:
    return FailingDBRuntime()


@pytest.fixture()
def recording_runtime() -> RecordingRuntime:
    return RecordingRuntime()


class _RuntimeShell:
    """Coque runtime minimale : porte un llm injecté + call_db stub."""

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm
        self.model_answer = "llama-3.1-8b-instant"

    async def call_db(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "success"}


@pytest.fixture()
def runtime_with(request):
    def _make(llm: Any = None) -> _RuntimeShell:
        return _RuntimeShell(llm=llm)
    return _make
