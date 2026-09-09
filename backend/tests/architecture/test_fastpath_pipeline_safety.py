"""FastPath — preuve PIPELINE (mandat de gel du Bloc 1, §14) : le bypass
`input_interpreter -> memory_update` (skip `cognitive_guard`) pour
`event ∈ {ANSWER, SELECTION}` sur un goal de tunnel acheteur
(`core/policies.py::FastPathPolicy.for_buyer`) ne doit JAMAIS dépendre d'un
accident de nomenclature — il doit reposer sur une propriété STRUCTURELLE
prouvée sur le VRAI pipeline (`make_input_interpreter` réel, pas un état
artificiel construit à la main).

Verdict : FASTPATH PROVEN SAFE AND FROZEN — voir le rapport de clôture du
Bloc 1 pour les deux invariants qui le garantissent :

    I1. `cognitive_guard::_classify_nominal_action` (et les branches
        INTERRUPT/RECOVER/ABANDON qui la précèdent) ne testent JAMAIS
        `event in {"ANSWER", "SELECTION"}` dans une condition de bascule —
        seuls NEW_TASK/UNKNOWN/OUT_OF_SCOPE/REJECT y figurent. Un tour
        ANSWER/SELECTION qui atteindrait quand même `cognitive_guard`
        retomberait donc TOUJOURS sur CONTINUE_ACTIVE_GOAL (goal verrouillé
        garanti par la policy — voir goals ci-dessous), jamais sur une
        action d'arbitrage. Prouvé par lecture directe du code (pas une
        supposition) : voir `test_cognitive_guard_never_branches_on_answer_or_selection`.
    I2. `input_interpreter` garantit qu'un événement ANSWER survivant sur un
        slot de type SLOT_FILLING_INPUTS ne porte JAMAIS un produit ou une
        intention distincte du goal verrouillé — toute bascule réelle est
        promue en NEW_TASK avant même d'atteindre le router (correction
        symétrique du 2026-09-08 : le garde existant ne couvrait que le
        sens NEW_TASK->ANSWER, pas ANSWER direct — voir
        `interpreter/routing.py`, juste après le calcul de
        `_is_different_product`/`_is_different_goal`). Prouvé par un test
        PIPELINE ci-dessous — passe par le VRAI `make_input_interpreter`,
        pas par `cognitive_guard` seul.

Ce fichier construit le pipeline réel — `make_input_interpreter("BUYER")`,
`FastPathPolicy.for_buyer()`, et `cognitive_guard` en mode SHADOW (exécuté
en plus, jamais à la place, pour vérifier qu'il aurait été d'accord) — sur
les 7 formulations du mandat, dans un tunnel acheteur actif."""
from __future__ import annotations

import ast
import inspect
import json
from typing import Any, Dict

import pytest

from agriconnect.graphs.agents.market_coach.core.goals import ALL_BUYER_TUNNEL_GOALS
from agriconnect.graphs.agents.market_coach.core.policies import FastPathPolicy
from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from agriconnect.graphs.agents.market_coach.nodes import cognitive as cognitive_mod
from agriconnect.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import make_state, run

_TUNNEL_GOAL = "BUYER_PREORDER_INIT"
assert _TUNNEL_GOAL in ALL_BUYER_TUNNEL_GOALS


class _ScriptedCompletion:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.choices = [
            type(
                "Choice",
                (),
                {"message": type("Msg", (), {"content": json.dumps(payload)})()},
            )()
        ]
        self.model = "scripted-model"


class _ScriptedLLM:
    """Un seul appel scripté — chaque phrase de ce test instancie la sienne."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        return _ScriptedCompletion(self._payload)


def _runtime(llm=None):
    return type("RT", (), {"llm": llm, "model_answer": "scripted-model"})()


async def _interpret(text: str, expected_input: str, *, llm_payload=None, **state_kwargs):
    interp = make_input_interpreter("BUYER")
    state = make_state(
        user_query=text,
        normalized_text=text,
        current_goal=_TUNNEL_GOAL,
        expected_input=expected_input,
        user_role="BUYER",
        **state_kwargs,
    )
    llm = _ScriptedLLM(llm_payload) if llm_payload is not None else None
    rt = _runtime(llm=llm)
    patch = await interp(state, rt)
    merged = {**state, **patch}
    return merged, llm


def _fastpath_route(state: Dict[str, Any]) -> str:
    return FastPathPolicy.for_buyer().route(state)


class TestFastPathPipelineOnTheSevenMandatedPhrases:
    """Chaque cas : (a) fait tourner le VRAI `input_interpreter`, (b) lit
    `interpreted_event`, (c) applique `FastPathPolicy`, (d) si bypass, fait
    tourner `cognitive_guard` en SHADOW sur le même état final pour vérifier
    qu'il n'aurait produit aucune action d'arbitrage."""

    @pytest.mark.asyncio
    async def test_deterministic_quantity_answer_is_safely_bypassed(self):
        """"200 kg" — fast-path déterministe (aucun LLM), réponse au slot QUANTITY."""
        state, llm = await _interpret(
            "200 kg", "QUANTITY", transaction_payload={"product": "riz"}
        )
        assert state["interpreted_event"] == "ANSWER"
        route = _fastpath_route(state)
        assert route == "to_memory_fast"
        shadow = await cognitive_guard(state, None)
        assert shadow["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"

    @pytest.mark.asyncio
    async def test_a_correction_during_slot_filling_is_safely_bypassed(self):
        """"non, 500 kg" — correction chiffrée d'un slot déjà répondu, même
        produit : reste une ANSWER légitime."""
        state, llm = await _interpret(
            "non, 500 kg", "QUANTITY", transaction_payload={"product": "riz"}
        )
        assert state["interpreted_event"] == "ANSWER"
        route = _fastpath_route(state)
        assert route == "to_memory_fast"
        shadow = await cognitive_guard(state, None)
        assert shadow["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"

    @pytest.mark.asyncio
    async def test_a_free_text_new_request_is_never_bypassed(self):
        """"en fait je veux acheter" — le LLM classe une nouvelle demande
        (intent différent du goal verrouillé, confiance suffisante) :
        NEW_TASK survit, jamais rabaissé en ANSWER — donc jamais bypassé."""
        state, llm = await _interpret(
            "en fait je veux acheter",
            "QUANTITY",
            transaction_payload={"product": "riz"},
            llm_payload={
                "interpreted_event": "NEW_TASK",
                "detected_intent": "BUYER_REQUEST",
                "interpreter_confidence": 0.85,
                "validation_status": "VALID",
                "extracted_entities": {},
            },
        )
        assert llm.calls == 1
        assert state["interpreted_event"] == "NEW_TASK"
        route = _fastpath_route(state)
        assert route == "to_cognitive", (
            "un NEW_TASK ne doit JAMAIS emprunter le bypass — FastPathPolicy "
            "ne déclenche que sur ANSWER/SELECTION"
        )

    @pytest.mark.asyncio
    async def test_a_product_switch_disguised_as_answer_is_promoted_and_never_bypassed(
        self,
    ):
        """"je change pour du maïs" — cas central du mandat §13. Le LLM
        classe (à tort, du point de vue du contrat ANSWER) ce message
        directement comme ANSWER, avec un produit DIFFÉRENT du produit
        suivi. Preuve de la correction du 2026-09-08 (garde symétrique) :
        l'interpréteur promeut cet ANSWER en NEW_TASK avant que
        FastPathPolicy ne puisse le voir."""
        state, llm = await _interpret(
            "je change pour du mais",
            "QUANTITY",
            transaction_payload={"product": "riz"},
            llm_payload={
                "interpreted_event": "ANSWER",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.6,
                "validation_status": "VALID",
                "extracted_entities": {"product": "mais"},
            },
        )
        assert llm.calls == 1
        assert state["interpreted_event"] == "NEW_TASK", (
            "un changement de produit ne doit JAMAIS rester classé ANSWER — "
            "voir le garde symétrique ajouté dans interpreter/routing.py"
        )
        route = _fastpath_route(state)
        assert route == "to_cognitive"

    @pytest.mark.asyncio
    async def test_a_structured_numeric_selection_is_safely_bypassed(self):
        """"2" — index de menu, fast-path déterministe (aucun LLM)."""
        state, llm = await _interpret(
            "2", "SELECTION", expected_candidates=["Option A", "Option B"]
        )
        assert state["interpreted_event"] == "SELECTION"
        route = _fastpath_route(state)
        assert route == "to_memory_fast"
        shadow = await cognitive_guard(state, None)
        assert shadow["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"

    @pytest.mark.asyncio
    async def test_a_free_text_selection_is_bypassed_but_provably_equivalent(self):
        """"la deuxième option" — sélection en langage libre. Le LLM la
        classe SELECTION (légitime) : bypassée. La preuve de sûreté ne
        repose PAS sur la qualité de cette classification (risque LLM
        générique, hors de portée de `cognitive_guard` de toute façon —
        voir I1 : aucune branche de `cognitive_guard` ne traite SELECTION
        différemment de ANSWER)."""
        state, llm = await _interpret(
            "la deuxieme option",
            "SELECTION",
            expected_candidates=["Option A", "Option B"],
            llm_payload={
                "interpreted_event": "SELECTION",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.9,
                "validation_status": "VALID",
                "extracted_entities": {"selected_value": "la deuxieme option"},
            },
        )
        assert state["interpreted_event"] == "SELECTION"
        route = _fastpath_route(state)
        assert route == "to_memory_fast"
        shadow = await cognitive_guard(state, None)
        assert shadow["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"

    @pytest.mark.asyncio
    async def test_genuine_confusion_is_never_bypassed(self):
        """"je ne comprends pas" — confusion réelle : reste UNKNOWN, jamais
        bypassé (FastPathPolicy ne déclenche que sur ANSWER/SELECTION) —
        atteint `cognitive_guard`, qui décide RECOVER (tunnel actif)."""
        state, llm = await _interpret(
            "je ne comprends pas",
            "QUANTITY",
            transaction_payload={"product": "riz"},
            llm_payload={
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.1,
                "validation_status": "INVALID_MISSING_UNIT",
                "extracted_entities": {},
            },
        )
        assert state["interpreted_event"] == "UNKNOWN"
        route = _fastpath_route(state)
        assert route == "to_cognitive"
        result = await cognitive_guard(state, None)
        assert result["cognitive_decision"]["action"] == "recover_active_tunnel"


def _mentions_answer_or_selection(test_node: ast.expr) -> bool:
    for node in ast.walk(test_node):
        if isinstance(node, ast.Constant) and node.value in ("ANSWER", "SELECTION"):
            return True
    return False


def _contains_return(body: list) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Return):
                return True
    return False


class TestCognitiveGuardNeverBranchesOnAnswerOrSelection:
    """Invariant I1, prouvé par inspection du CODE SOURCE (pas seulement par
    des exemples) : aucune condition de branchement qui DÉCIDE de l'action
    finale (interruption / recover / abandon / `_classify_nominal_action`)
    ne compare `event` à "ANSWER" ou "SELECTION". Un test par exemple ne
    peut jamais couvrir tous les cas ; cette preuve structurelle si.

    Portée volontairement restreinte aux `if` qui RETOURNENT directement
    (les 2 court-circuits INTERRUPT/RECOVER-ABANDON de `cognitive_guard`) —
    une annotation de télémétrie pure comme `decision["express_mode"]`
    (qui teste bien `event == "ANSWER"`, mais n'influence JAMAIS `action`
    ni le routage) n'est pas une violation de cet invariant : elle ne
    RETOURNE rien, elle ne fait qu'enrichir un champ de diagnostic."""

    def test_no_decision_bearing_branch_compares_event_to_answer_or_selection(self):
        tree = ast.parse(inspect.getsource(cognitive_mod.cognitive_guard))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _contains_return(node.body):
                if _mentions_answer_or_selection(node.test):
                    offenders.append(ast.dump(node.test))
        assert not offenders, (
            f"un branchement DÉCISIONNEL (qui retourne) de cognitive_guard "
            f"teste event contre ANSWER/SELECTION : {offenders} — l'invariant "
            f"I1 du FastPath est rompu, le bypass n'est plus prouvé sûr"
        )

    def test_classify_nominal_action_never_mentions_answer_or_selection(self):
        source = inspect.getsource(cognitive_mod._classify_nominal_action)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in ("ANSWER", "SELECTION"):
                pytest.fail(
                    "_classify_nominal_action référence ANSWER/SELECTION — "
                    "l'invariant I1 du FastPath est rompu"
                )
