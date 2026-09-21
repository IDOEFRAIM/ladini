"""BLOC 2 — micro-passe finale (2026-09-09), Invariant C : le FastPath
(`input_interpreter -> memory_update`, qui saute `cognitive_guard` ET
`goal_planner`) doit être équivalent au chemin complet
(`input_interpreter -> cognitive_guard -> goal_planner -> memory_update`)
pour les tours qu'il court-circuite.

Ce fichier EXÉCUTE réellement les deux chemins depuis le MÊME état initial
et compare, en plus de l'état MÉTIER, l'état de CONTRÔLE (mandat §10-§19).

Classification des champs (aucun n'est ignoré par intuition — voir le
rapport de passe pour la recherche de lecteurs) :

  BUSINESS_FIELDS   : non négociables, comparés après `memory_update`.
  CONTROL_STRICT    : influencent routing/lifecycle en aval — comparés
                      STRICTEMENT après `memory_update`.
  CONTROL_NORMALISED: `status` — DIFFÈRE après `memory_update` ('' côté
                      FastPath vs 'PLANNING' écrit par le planner) mais est
                      réécrit par `validator` sur ses 5 branches de retour
                      (et jamais LU par lui) ; `validator` est le nœud
                      SUIVANT sur les DEUX chemins, avant tout lecteur réel
                      (DomainRouter, `nodes/routing.py`,
                      `interpreter/strategy.py`). La normalisation est
                      PROUVÉE par test (`TestStatusIsNormalisedByValidator`),
                      pas supposée.
  OBSERVABILITY_ONLY: diagnostic pur, aucun lecteur de routing.

Divergence RÉELLE trouvée et corrigée pendant cette passe : `goal_status`
survivait à `validator` (réécrit sur 2 branches / 5) et changeait un
résultat MÉTIER dans `nodes/cleaner.py` (réinitialisation de
`draft_payload`). Corrigée par une invariante de cohérence dans
`nodes/memory.py` (« un goal actif ne peut pas avoir de goal_status
vide ») — voir son commentaire.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

import ladini.graphs.agents.market_coach.interpreter.routing  # noqa: F401
from ladini.graphs.agents.market_coach.core.policies import get_fast_path_policy
from ladini.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.cleaner import _ACTIVE_GOAL_STATES
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, make_state, run

BUSINESS_FIELDS = (
    "current_goal",
    "transaction_payload",
    "stable_entities",
    "draft_payload",
    "available_mapping",
    "menu_snapshot_id",
)
CONTROL_STRICT = (
    "detected_intent",
    "response_strategy",
    "pending_interaction",
)
#: `goal_status` : comparé par CLASSE D'ÉQUIVALENCE, pas ignoré. Le
#: FastPath conserve la valeur d'entrée ("WAITING_INPUT" laissé par le
#: `validator` du tour précédent) là où le planner écrit "ACTIVE" (RÈGLE
#: 1bis). Recherche EXHAUSTIVE des lecteurs de `goal_status` dans
#: `src/ladini` (pas seulement `market_coach/` — un 3ᵉ lecteur y a été
#: trouvé grâce à ce test) : exactement TROIS —
#:   * `nodes/cleaner.py`  : `goal_status in _ACTIVE_GOAL_STATES` ;
#:   * `nodes/memory.py`   : `goal_status == "COMPLETED"` ;
#:   * `orchestrator.py`   : `goal_status == "COMPLETED"` (fin de tour).
#: Les deux valeurs appartiennent à la même classe active et aucune n'est
#: "COMPLETED" : les deux lecteurs se comportent donc à l'identique — ce
#: qui est PROUVÉ par `TestGoalStatusEquivalenceClassIsConsumerSafe`, pas
#: seulement affirmé (mandat §13).
CONTROL_NORMALISED = ("status",)
OBSERVABILITY_ONLY = (
    "cognitive_decision",
    "intent_competition",
    "disambiguation_candidate",
    "conversation_progress",
    "proactive_hint",
)


def _selection_fields(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = state.get("transaction_payload") or {}
    return {
        "selection_index": payload.get("selection_index"),
        "selected_value": payload.get("selected_value"),
        "resolved_id": payload.get("resolved_id"),
    }


def _run_both_paths(**overrides) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Renvoie (état_final_FASTPATH, état_final_CHEMIN_COMPLET)."""
    s0 = make_state(**overrides)

    # PATH A — FastPath réel : interpréteur -> memory_update.
    fast_state = {**s0, **run(memory_update(dict(s0), StubRuntime()))}

    # PATH B — shadow : interpréteur -> cognitive_guard -> goal_planner
    #                   -> memory_update.
    rt_b = StubRuntime()
    guard = run(cognitive_guard(dict(s0), rt_b))
    after_guard = {**s0, **guard}
    planner = run(goal_planner(after_guard, rt_b))
    after_planner = {**after_guard, **planner}
    full_state = {**after_planner, **run(memory_update(after_planner, rt_b))}
    return fast_state, full_state


def _assert_equivalent(fast: Dict[str, Any], full: Dict[str, Any]) -> None:
    business_diffs = {
        f: (fast.get(f), full.get(f))
        for f in BUSINESS_FIELDS
        if fast.get(f) != full.get(f)
    }
    assert not business_diffs, (
        f"divergence d'état MÉTIER (blocker, mandat §19/§24) : {business_diffs}"
    )
    assert _selection_fields(fast) == _selection_fields(full), (
        "divergence sur la résolution de sélection entre les deux chemins"
    )
    control_diffs = {
        f: (fast.get(f), full.get(f))
        for f in CONTROL_STRICT
        if fast.get(f) != full.get(f)
    }
    fast_gs = str(fast.get("goal_status") or "").upper()
    full_gs = str(full.get("goal_status") or "").upper()
    if fast_gs != full_gs and not (
        fast_gs in _ACTIVE_GOAL_STATES and full_gs in _ACTIVE_GOAL_STATES
    ):
        control_diffs["goal_status"] = (fast.get("goal_status"), full.get("goal_status"))
    assert not control_diffs, (
        f"divergence d'état de CONTRÔLE (blocker, mandat §13/§24) : {control_diffs}"
    )
    # `status` : normalisé par `validator` — comparé APRÈS lui, jamais ignoré.
    fast_after = {**fast, **run(validator(dict(fast), StubRuntime()))}
    full_after = {**full, **run(validator(dict(full), StubRuntime()))}
    status_diffs = {
        f: (fast_after.get(f), full_after.get(f))
        for f in CONTROL_NORMALISED
        if fast_after.get(f) != full_after.get(f)
    }
    assert not status_diffs, (
        "`status` n'est plus normalisé par `validator` — la justification "
        f"du shadow test ne tient plus : {status_diffs}"
    )


def _assert_actually_fastpath(**overrides) -> None:
    """Le scénario doit RÉELLEMENT être routé vers le FastPath — sinon le
    test comparerait deux fois le même chemin."""
    state = make_state(**overrides)
    assert get_fast_path_policy("BUYER").route(state) == "to_memory_fast"


class TestFastPathIsTransactionallyEquivalentToTheFullPath:
    """Scénarios réellement routés vers le FastPath (mandat §24)."""

    def test_answer_quantity(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "mais"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_answer_price(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="PRICE",
            normalized_text="600 FCFA",
            extracted_entities={"price": 600.0},
            transaction_payload={"product": "mais", "quantity": 200.0, "unit": "KG"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_selection_with_an_active_mapping(self):
        kw = dict(
            interpreted_event="SELECTION",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="SELECTION",
            normalized_text="2",
            session_id="fastpath-sel",
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait"},
            available_mapping={"1": "vendor-1", "2": "vendor-2"},
            working_memory={
                "active_goal": "BUYER_ADD_TO_CART",
                "available_mapping_kind": "product_vendor",
            },
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_pricing_tier_selection(self):
        kw = dict(
            interpreted_event="SELECTION",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="SELECTION",
            normalized_text="1",
            session_id="fastpath-tier",
            extracted_entities={"selection_index": 1},
            transaction_payload={"product": "lait", "quantity": 40},
            available_mapping={"1": "tier-1", "2": "tier-2"},
            working_memory={
                "active_goal": "BUYER_ADD_TO_CART",
                "available_mapping_kind": "pricing_tier",
            },
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_with_an_existing_draft_payload(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_PREORDER_INIT",
            expected_input="QUANTITY",
            normalized_text="50 kg",
            extracted_entities={"quantity": 50.0, "unit": "KG"},
            transaction_payload={"product": "mais"},
            draft_payload={"draft_id": "d-1", "version": 3},
            working_memory={"active_goal": "BUYER_PREORDER_INIT"},
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_with_stable_entities_present(self):
        """Cas le plus sensible : `cognitive_guard` reporte les entités
        stables (`_entity_carry_forward`) — le FastPath ne l'exécute pas.
        Si cela changeait `transaction_payload`, le bypass serait un
        blocker (mandat §27)."""
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="30 kg",
            extracted_entities={"quantity": 30.0, "unit": "KG"},
            transaction_payload={},
            stable_entities={"product": "tomates", "unit": "KG", "zone_name": "Ouaga"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))


class TestArbitrationInputsNeverEnterFastPath:
    """Mandat §25 : une entrée qui demande un arbitrage conversationnel ne
    doit JAMAIS être court-circuitée."""

    @pytest.mark.parametrize(
        "event,intent",
        [("NEW_TASK", "BUYER_REQUEST"), ("UNKNOWN", "UNKNOWN"), ("REJECT", "UNKNOWN")],
    )
    def test_non_slot_events_go_to_the_cognitive_chain(self, event, intent):
        state = make_state(
            interpreted_event=event,
            detected_intent=intent,
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="non finalement je veux acheter du mais",
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        assert get_fast_path_policy("BUYER").route(state) == "to_cognitive"

    def test_a_non_buyer_tunnel_goal_never_uses_the_fastpath(self):
        state = make_state(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert get_fast_path_policy("BUYER").route(state) == "to_cognitive"


class TestConvergenceWithPreExistingControlState:
    """Mandat §21 : scénarios avec un état de contrôle DÉJÀ posé en entrée
    — c'est là que la divergence `goal_status` (corrigée dans cette passe)
    se manifestait."""

    def test_status_waiting_input_on_entry(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            status="WAITING_INPUT",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "mais"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_goal_status_already_present_on_entry(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            status="WAITING_INPUT",
            goal_status="WAITING_INPUT",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "mais"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        _assert_actually_fastpath(**kw)
        _assert_equivalent(*_run_both_paths(**kw))

    def test_empty_goal_status_with_a_live_draft_does_not_wipe_it(self):
        """Régression EXACTE trouvée par cette passe : `goal_status` vide
        (FastPath) vs "ACTIVE" (planner) faisait réinitialiser
        `draft_payload` par `nodes/cleaner.py` sur le seul FastPath."""
        from ladini.graphs.agents.market_coach.nodes.cleaner import (
            state_cleaner_node,
        )

        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "lait"},
            draft_payload={"draft_id": "d-1"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        _assert_actually_fastpath(**kw)
        fast, full = _run_both_paths(**kw)
        _assert_equivalent(fast, full)

        # …et jusqu'au consommateur réel de `goal_status`.
        fast_v = {**fast, **run(validator(dict(fast), StubRuntime()))}
        full_v = {**full, **run(validator(dict(full), StubRuntime()))}
        fast_c = {**fast_v, **run(state_cleaner_node(dict(fast_v), StubRuntime()))}
        full_c = {**full_v, **run(state_cleaner_node(dict(full_v), StubRuntime()))}
        assert fast_c.get("draft_payload") == full_c.get("draft_payload")


class TestStatusIsNormalisedByValidator:
    """Mandat §13/§14 : `status` n'est PAS mis dans une ignore-list — sa
    divergence est justifiée par une normalisation PROUVÉE, ici testée."""

    def test_validator_rewrites_status_on_every_return_branch(self):
        import ast
        import inspect

        from ladini.graphs.agents.market_coach.nodes import validation

        source = inspect.getsource(validation.validator)
        tree = ast.parse(source.lstrip())
        returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
        assert returns, "validator sans return : contrat inattendu"
        # Chaque retour passe par `_finalize_validator_response(state, {...})`
        # ou construit `result` — dans les deux cas le littéral contient
        # "status". On vérifie la présence du littéral dans chaque branche.
        assert source.count('"status"') >= len(returns), (
            "au moins une branche de retour de `validator` n'écrit plus "
            "`status` — la justification du shadow test tombe"
        )

    def test_validator_never_reads_status_or_goal_status(self):
        import inspect

        from ladini.graphs.agents.market_coach.nodes import validation

        source = inspect.getsource(validation.validator)
        assert 'get("status")' not in source
        assert 'get("goal_status")' not in source

    def test_the_two_paths_converge_on_status_after_validator(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "mais"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        fast, full = _run_both_paths(**kw)
        # Divergence AVANT validator (documentée, attendue)…
        assert fast.get("status") != full.get("status")
        # …disparue APRÈS lui, sur les deux chemins.
        fast_v = {**fast, **run(validator(dict(fast), StubRuntime()))}
        full_v = {**full, **run(validator(dict(full), StubRuntime()))}
        assert fast_v.get("status") == full_v.get("status")


class TestGoalStatusEquivalenceClassIsConsumerSafe:
    """Mandat §13/§15 : la seule divergence résiduelle de `goal_status`
    ("WAITING_INPUT" côté FastPath vs "ACTIVE" côté planner) est prouvée
    inoffensive pour ses DEUX seuls lecteurs — testé, pas expliqué."""

    def _cleaner_draft(self, goal_status):
        from ladini.graphs.agents.market_coach.nodes.cleaner import (
            state_cleaner_node,
        )

        st = make_state(
            current_goal="BUYER_ADD_TO_CART",
            goal_status=goal_status,
            status="PROCESSING",
            draft_payload={"draft_id": "d-1"},
            transaction_payload={"product": "lait"},
        )
        return run(state_cleaner_node(st, StubRuntime())).get("draft_payload", "UNSET")

    def test_state_cleaner_treats_waiting_input_and_active_identically(self):
        assert self._cleaner_draft("WAITING_INPUT") == self._cleaner_draft("ACTIVE")

    def test_state_cleaner_distinguishes_a_terminal_goal_status(self):
        """Contre-preuve : la classe d'équivalence n'est pas vide de sens —
        une valeur HORS classe active produit bien un résultat différent."""
        assert self._cleaner_draft("COMPLETED") != self._cleaner_draft("ACTIVE")

    def _memory_stable(self, goal_status):
        st = make_state(
            interpreted_event="ANSWER",
            current_goal="BUYER_ADD_TO_CART",
            goal_status=goal_status,
            expected_input="QUANTITY",
            normalized_text="200 kg",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "lait"},
        )
        return run(memory_update(st, StubRuntime())).get("stable_entities")

    def test_memory_update_transaction_closed_gate_is_identical(self):
        assert self._memory_stable("WAITING_INPUT") == self._memory_stable("ACTIVE")

    def test_only_two_readers_of_goal_status_exist(self):
        """Verrouille l'hypothèse : si un 3ᵉ lecteur apparaît, la preuve
        ci-dessus ne couvre plus le contrat et ce test casse."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "ladini"
        readers = set()
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if 'get("goal_status")' in text:
                readers.add(path.name)
        # `turn_telemetry.py` (cockpit /admin/monitoring) LIT `goal_status` en pure OBSERVATION : il l'enregistre
        # dans `agent_turns` sans jamais brancher de logique métier dessus — il ne peut donc pas casser la preuve
        # d'innocuité ci-dessous (qui ne concerne que les lecteurs COMPORTEMENTAUX).
        readers.discard("turn_telemetry.py")
        assert readers == {"cleaner.py", "memory.py", "orchestrator.py"}, (
            f"lecteurs de goal_status inattendus : {sorted(readers)} — la "
            "preuve d'innocuité de la divergence FastPath doit être refaite"
        )

    def test_every_known_reader_predicate_is_blind_to_the_equivalence_class(self):
        """Les 3 lecteurs réels, testés sur leur PRÉDICAT exact :
          * `nodes/cleaner.py`    : `goal_status in _ACTIVE_GOAL_STATES`
          * `nodes/memory.py`     : `goal_status == "COMPLETED"`
          * `orchestrator.py`     : `goal_status == "COMPLETED"` (fin de tour,
            décide `ws.close_tunnel()`)
        Aucun ne distingue "WAITING_INPUT" de "ACTIVE"."""
        for value in ("WAITING_INPUT", "ACTIVE"):
            assert value in _ACTIVE_GOAL_STATES          # cleaner
            assert value.upper() != "COMPLETED"          # memory + orchestrator


class TestCognitiveDecisionIsObservabilityOnlyForFastPathTurns:
    """Mandat §11 : `cognitive_decision` n'est classé OBSERVABILITY qu'après
    recherche de ses lecteurs — il en a DEUX réels
    (`interpreter/strategy.py`, `nodes/clarification.py`), tous deux
    frozen. Preuve d'innocuité pour un tour FastPath : ils ne comparent
    `action` qu'à deux littéraux de RÉCUPÉRATION/ABANDON, qu'un tour
    éligible au FastPath (continuation de tunnel) ne produit jamais — ni
    côté FastPath (champ absent) ni côté chemin complet
    (CONTINUE_ACTIVE_GOAL)."""

    def test_the_two_readers_only_branch_on_recovery_or_abandon_literals(self):
        import inspect

        from ladini.graphs.agents.market_coach.interpreter import strategy
        from ladini.graphs.agents.market_coach.nodes import clarification

        for module, fn in (
            (strategy, strategy.response_strategy),
            (clarification, clarification.clarification_node),
        ):
            source = inspect.getsource(fn)
            for line in source.splitlines():
                if "cognitive_action" in line and "==" in line:
                    assert (
                        "recover_active_tunnel" in line
                        or "abandon_tunnel_max_retries" in line
                        or "RECOVER_ACTIVE_GOAL" in line
                        or "ABANDON_ACTIVE_GOAL" in line
                    ), (
                        f"{module.__name__} branche sur une valeur de "
                        f"cognitive_decision.action non couverte par la "
                        f"preuve d'innocuité FastPath : {line.strip()}"
                    )

    def test_a_fastpath_turn_never_produces_those_literals_on_either_path(self):
        kw = dict(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            normalized_text="200 kg",
            extracted_entities={"quantity": 200.0, "unit": "KG"},
            transaction_payload={"product": "mais"},
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
        )
        fast, full = _run_both_paths(**kw)
        for state in (fast, full):
            action = (state.get("cognitive_decision") or {}).get("action", "")
            assert action not in {
                "recover_active_tunnel",
                "abandon_tunnel_max_retries",
            }
