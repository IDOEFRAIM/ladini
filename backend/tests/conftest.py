"""Socle commun à TOUTE la suite de tests Ladini.

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
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Rendre `ladini` importable sans installation editable.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# (Phase 2 hardening, commit 13) : `ladini.core.settings.settings` est un singleton
# construit à la PREMIÈRE importation du module, n'importe où dans la session pytest
# (voir le même constat dans `tests/unit/test_telemetry_worker_metrics.py`). Quelques
# tests architecturaux (`tests/architecture/test_entry_block_topology.py`,
# `test_cognitive_decisions_are_consumed_or_removed.py`) compilent le VRAI graphe SANS
# fournir de `mc_runtime`/`llm_client` explicite — `MarketRuntime.llm` retombe alors sur
# `get_llm()`, qui exige `GROQ_API_KEY`/`LADINI_APIKEY` pour construire le SDK Groq (aucun
# appel réseau à la construction, juste un client — la clé n'a jamais besoin d'être
# valide). `setdefault` : ne touche RIEN si une vraie clé est déjà fournie (CI de
# déploiement, tests qui veulent un vrai LLM) — cette valeur n'est qu'un repli pour que la
# suite STRUCTURELLE (jamais un appel LLM réel : ce module entier documente "AUCUN LLM
# réel") tourne sans variable d'environnement à poser manuellement. Posé ICI (avant tout
# import de `ladini.*`, au tout premier import de ce conftest par pytest) et nulle part en
# code de production — jamais une clé factice câblée dans `settings.py`/`get_llm.py`.
os.environ.setdefault("GROQ_API_KEY", "test-suite-placeholder-key")


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
    """Runtime minimal : LLM injectable + call_db qui enregistre les appels.

    `llm_gateway`/`profile_answer` (2026-09-02, ajout LLM Gateway) : miroir
    de `MarketRuntime.llm_gateway`/`profile_answer` (utils.py) — TOUJOURS un
    `LegacyOverrideGateway` qui appelle `self.llm` directement, JAMAIS le
    vrai Gateway (registry/health Redis/circuit breaker). Indispensable :
    des dizaines de tests injectent `StubRuntime(llm=ScriptedLLM(...))` et
    s'attendent à un appel synchrone déterministe sans réseau — le vrai
    Gateway interrogerait Redis à chaque décision de routage, ce qui violerait
    la philosophie "AUCUN réseau" de toute cette suite (voir docstring de
    fichier, tout en haut)."""

    def __init__(self, llm: Any = None, responses: Optional[Dict[str, Any]] = None) -> None:
        self.llm = llm
        self.model_answer = "test-model"
        self.calls: List[str] = []
        self._responses = responses or {}

    @property
    def profile_answer(self) -> Any:
        from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile

        return LLMProfile.REASONING

    @property
    def llm_gateway(self) -> Any:
        from ladini.graphs.agents.market_coach.llm_gateway import (
            LegacyOverrideGateway,
        )

        return LegacyOverrideGateway(self.llm, lambda: self.model_answer)

    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        self.calls.append(tool_name)
        if tool_name in self._responses:
            configured = self._responses[tool_name]
            # (2026-09-13) : une valeur CALLABLE reçoit les kwargs de CET
            # appel — nécessaire dès qu'un même tool_name est interrogé
            # plusieurs fois par tour avec des arguments différents (ex:
            # `get_producer_orders` filtré par `status` successivement) et
            # doit renvoyer une réponse distincte par appel. Un dict statique
            # (l'usage historique, toujours supporté) continue de répondre
            # identiquement à tous les appels.
            if callable(configured):
                return configured(**kwargs)
            return configured
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

def _pending_interaction_patch_for_legacy_expected_input(expected_input: str) -> Dict[str, Any]:
    """Traduit le raccourci de test `expected_input="PRODUCT"/"CONFIRMATION"/
    "SELECTION"/...` vers une écriture RÉELLE de `pending_interaction` (refonte
    architecturale 2026-09-02 — `PendingInteraction` est désormais la seule
    source canonique lue par le runtime). `expected_input` reste lisible dans
    les tests comme raccourci d'intention (plus court que construire l'objet
    à la main), mais sans cette traduction un test qui l'utilise seul
    figerait l'ANCIEN contrat au lieu du nouveau — voir
    tests/interpreter/test_goal_planner_state_machine.py pour le précédent."""
    from ladini.graphs.agents.market_coach.core.pending_interaction import (
        InteractionKind,
        clear_pending_interaction,
        set_pending_interaction,
    )
    from ladini.graphs.agents.market_coach.core.slots import _EXPECTED_INPUT_MAP

    category = str(expected_input or "NONE").upper().strip()
    if category in ("", "NONE"):
        return clear_pending_interaction("test_setup")
    if category == "CONFIRMATION":
        return set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, context_ref="confirmation"
        )
    if category == "SELECTION":
        return set_pending_interaction(InteractionKind.SELECTION_MENU)
    if category in ("OTP", "OTP_CODE"):
        return set_pending_interaction(InteractionKind.VERIFY_OTP)
    field_for_category = {v: k for k, v in reversed(list(_EXPECTED_INPUT_MAP.items()))}
    field = field_for_category.get(category)
    # Catégorie hors du registre canonique (ex: "ORDER_ID"/"CANCELLATION_REASON"/
    # "UPDATE_FIELD", des mini-flows dédiés — voir core/pending_interaction.py::
    # to_tunnel_category) : utilise directement le token en minuscule comme nom
    # de champ, `to_tunnel_category` le retrouvera par repli symétrique.
    return set_pending_interaction(
        InteractionKind.ENTER_FIELD, field_name=field or category.lower()
    )


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
    # Un appelant qui fixe `expected_input=` explicitement mais ne pose PAS
    # `pending_interaction` lui-même obtient quand même un état cohérent avec
    # le nouveau contrat — sans quoi ce test verrouillerait silencieusement
    # l'ANCIEN comportement (lecture directe de `expected_input`, supprimée
    # du runtime).
    if "expected_input" in overrides and "pending_interaction" not in overrides:
        base.update(
            _pending_interaction_patch_for_legacy_expected_input(base["expected_input"])
        )
    return base


@pytest.fixture()
def state_factory():
    return make_state


# =====================================================================
# PERSISTANCE DES DRAFTS DE BESOIN RÉCURRENT (frontière base de données)
# =====================================================================


@pytest.fixture(autouse=True)
def recurring_draft_table(request, monkeypatch):
    """Doublure en mémoire de `services/database/recurring_need_draft_store.py` pour toute
    la suite (même philosophie « aucune base » que le reste de ce fichier). Les tests
    `tests/schema/` exercent au contraire le VRAI store contre PostgreSQL."""
    if "/tests/schema/" in str(request.node.fspath).replace("\\", "/"):
        yield None
        return
    from tests.harness.recurring import InMemoryDraftTable, install_draft_table

    yield install_draft_table(InMemoryDraftTable(), monkeypatch.setattr)
