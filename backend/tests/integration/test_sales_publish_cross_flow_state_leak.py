"""Full REAL node chain for the SALES_PUBLISH_PRODUCT cross-flow state-leak bug
(2026-09-28) — même méthode que `test_recurring_need_state_leak.py` (nœuds réels,
ordre réel, reducers LangGraph réels, jamais un `dict.update()` naïf).

Scénario signalé (WhatsApp) :

    User: "je veux vendre mes boeufs"
    Agent: [demande si prêt maintenant ou plus tard]
    User: "1"
    Agent: [demande quel produit]
    User: "ce sont des boeufs"
    Agent: "Vente de 461 000 UNITE de boeufs à 461 000 FCFA/UNITE."

`quantity=461000`/`price=461000`/`unit=UNITE` n'ont JAMAIS été dits dans cette
conversation — ils survivaient d'une tentative de vente antérieure, abandonnée
avant d'avoir donné de produit (donc jamais bootstrap en `sales_publish_draft`,
seulement posés à cru dans `transaction_payload`), avec un tunnel encore actif
(`pending_interaction` non expiré). Root cause exacte : `interpreter/
goal_planner.py`, RÈGLE 1quater — voir son commentaire "incident réel —
contamination cross-flow SALES_PUBLISH_PRODUCT" pour la trace complète.

Chaîne : input_interpreter -> cognitive_guard -> goal_planner -> memory_update ->
validator, pour le tour qui reproduit le bug. `confirmation_gate`/
`producer_context_resolver` (farm/ensure_farm_node, MCP `create_product`) sont
volontairement omis : le bug est intégralement prouvé/corrigé à la frontière
`validator` (missing_fields/transaction_payload), et les inclure ajouterait des
dépendances (farm existante, résolveur produit) sans rapport avec le lifecycle
transactionnel audité ici — voir `test_recurring_need_state_leak.py`, qui fait le
même choix pour `buyer_context_resolver`.
"""
from __future__ import annotations

import json
import time
import typing
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, _Completion, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


class _DeviationThenNewTaskLLM:
    """Un tunnel `ENTER_FIELD` encore actif fait TOUJOURS router le message d'abord
    vers le micro-prompt ACTIVE_SLOT (contrat DEVIATION/ANSWER/UPDATE/REJECT/UNKNOWN
    — jamais NEW_TASK directement, voir `interpreter/state_router.py`) ; ce n'est
    QUE si ACTIVE_SLOT répond `DEVIATION` que l'interpréteur reclasse via le
    micro-prompt NEW_TASK. Un seul appel LLM scripté doit donc honorer les DEUX
    contrats dans le même tour — même technique que `tests/integration/
    test_conversation_characterization.py::_deviate_then_new_task`, adaptée ici à
    l'interface `chat.completions.create` de `StubRuntime` (au lieu du callable
    `llm=` du harnais `ConversationHarness`)."""

    def __init__(self, new_task_payload: Dict[str, Any]) -> None:
        self._new_task_payload = new_task_payload
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages") or []
        if "DEVIATION" in json.dumps(messages):
            payload = {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9}
        else:
            payload = self._new_task_payload
        return _Completion(json.dumps(payload), model=kwargs.get("model"))


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `patch` into `state` using each field's REAL LangGraph reducer — voir
    `test_recurring_need_state_leak.py::apply_patch` (même fonction, dupliquée ici
    pour ne pas créer de dépendance inter-fichiers de test)."""
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        if reducer is None:
            new_state[key] = value
            continue
        new_state[key] = reducer(state.get(key), value)
    return new_state


async def _run_turn(state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str) -> Dict[str, Any]:
    """Un tour : interpreter -> cognitive_guard -> goal_planner -> memory_update ->
    validator. `cognitive_guard` est délibérément INCLUS (contrairement au helper le
    plus simple de `test_recurring_need_state_leak.py`) : c'est LUI qui décide de ne
    jamais promouvoir un NEW_TASK de même intention que le goal courant en
    INTERRUPTION (`nodes/cognitive.py`, `detected_intent not in {"UNKNOWN",
    current_goal}`) — la racine exacte du bug passe par cette décision."""
    from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
        goal_planner,
    )

    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    interp = await interpreter(state, runtime)
    state = apply_patch(state, interp)

    cg = await cognitive_guard(state, runtime)
    state = apply_patch(state, cg)

    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    return state


def _abandoned_sales_publish_state() -> Dict[str, Any]:
    """Reproduit l'état laissé par une PRÉCÉDENTE tentative SALES_PUBLISH_PRODUCT qui a
    donné quantité+prix mais jamais de produit avant d'être abandonnée : `quantity`/
    `price`/`unit` vivent à cru dans `transaction_payload` — jamais dans
    `sales_publish_draft` (bootstrap UNIQUEMENT une fois `product`/`quantity`/`price`
    déjà tous réunis, voir `nodes/confirmation_gate.py::
    _resolve_sales_draft_based_confirmation`) — avec un tunnel `pending_interaction`
    encore ACTIF (dans la fenêtre TTL, `created_at` récent — `time.time()`,
    jamais un epoch arbitraire : `is_pending_expired` compare contre l'horloge
    réelle, un `created_at` trop ancien ferait passer PAR LA RÈGLE 5, pas la
    RÈGLE 1quater visée ici)."""
    return {
        "current_goal": "SALES_PUBLISH_PRODUCT",
        "status": "WAITING_INPUT",
        "user_phone": "+22670000099",
        "user_role": "PRODUCER",
        "transaction_payload": {"quantity": 461000.0, "price": 461000.0, "unit": "UNITE"},
        "extracted_entities": {},
        "working_memory": {"active_goal": "SALES_PUBLISH_PRODUCT"},
        "sales_publish_draft": None,
        "pending_interaction": {
            "kind": "ENTER_FIELD",
            "goal": "SALES_PUBLISH_PRODUCT",
            "field": "product",
            "context_ref": None,
            "candidates": [],
            "created_at": time.time() - 5.0,
            "status": "ACTIVE",
            "target": None,
        },
    }


class TestSalesPublishCrossFlowStateLeak:
    def test_a_fresh_new_task_never_inherits_a_stale_quantity_and_price(self):
        """Le bug exact : `quantity`/`price` d'une tentative abandonnée ne doivent
        jamais réapparaître dans la confirmation d'une vente totalement différente."""
        state = _abandoned_sales_publish_state()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _DeviationThenNewTaskLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "SALES_PUBLISH_PRODUCT",
                "confidence": 0.95,
                "entities": {"product": "boeufs"},
            }
        )

        state = run(_run_turn(state, interpreter, runtime, text="je veux vendre mes boeufs"))

        payload = state.get("transaction_payload") or {}
        assert payload.get("quantity") != 461000.0, (
            f"la quantité de la vente abandonnée a survécu : {payload!r}"
        )
        assert payload.get("price") != 461000.0, (
            f"le prix de la vente abandonnée a survécu : {payload!r}"
        )
        assert payload.get("unit") != "UNITE" or "quantity" not in payload, (
            f"l'unité périmée a survécu avec la quantité périmée : {payload!r}"
        )
        # `quantity`/`price` redeviennent des champs MANQUANTS — la conséquence
        # observable du bug (la question n'était jamais reposée) est ce qui doit
        # disparaître.
        missing = state.get("missing_fields") or []
        assert "quantity" in missing, f"quantity doit redevenir manquant : missing={missing!r}"
        assert "price" in missing, f"price doit redevenir manquant : missing={missing!r}"
        assert state.get("sales_publish_draft") is None, (
            "aucun draft ne doit être bootstrap avec des montants jamais dits dans ce tour"
        )

    def test_pilot_acceptance_scenario_boeufs_asks_next_question_without_premature_confirmation(self):
        """Critère d'acceptation du pilote (`AGENT_PILOT_RUNBOOK.md`, test manuel
        post-déploiement) : « je veux vendre mes boeufs » → product=boeufs,
        unit=TETE, quantity+price MANQUANTS, question suivante = quantity, AUCUN
        draft, AUCUNE confirmation. Vérifié pour un état vierge ET pour l'état
        périmé 461000 — les deux doivent converger vers le MÊME résultat
        (invariant I1 : un nouveau flow ne réutilise pas les slots d'un ancien)."""
        for label, initial in (
            ("stale", _abandoned_sales_publish_state()),
            (
                "clean",
                {
                    "user_phone": "+22670000099",
                    "user_role": "PRODUCER",
                    "extracted_entities": {},
                    "working_memory": {},
                },
            ),
        ):
            runtime = StubRuntime()
            interpreter = make_input_interpreter("PRODUCER")
            runtime.llm = _DeviationThenNewTaskLLM(
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.95,
                    "entities": {"product": "boeufs"},
                }
            )

            state = run(_run_turn(initial, interpreter, runtime, text="je veux vendre mes boeufs"))

            payload = state.get("transaction_payload") or {}
            assert payload.get("product") == "boeufs", f"[{label}] {payload!r}"
            assert payload.get("unit") == "TETE", f"[{label}] {payload!r}"
            for stale_key, stale_value in (("quantity", 461000.0), ("price", 461000.0)):
                assert payload.get(stale_key) != stale_value, f"[{label}] {payload!r}"
            assert set(state.get("missing_fields") or []) == {"quantity", "price"}, (
                f"[{label}] {state.get('missing_fields')!r}"
            )
            pending = state.get("pending_interaction") or {}
            assert pending.get("kind") == "ENTER_FIELD" and pending.get("field") == "quantity", (
                f"[{label}] la question suivante doit porter sur la quantité : {pending!r}"
            )
            assert state.get("status") == "WAITING_INPUT", f"[{label}] {state.get('status')!r}"
            assert state.get("sales_publish_draft") is None, f"[{label}] draft prématuré"
            assert not state.get("confirmation_summary_payload"), (
                f"[{label}] confirmation prématurée"
            )
            assert not state.get("execution_result"), f"[{label}] exécution prématurée"

    def test_a_correction_of_an_already_open_draft_is_preserved(self):
        """Garde-fou symétrique : une fois qu'un `sales_publish_draft` existe déjà
        (une VRAIE instance ouverte), un NEW_TASK même-but qui ne restate qu'UNE
        partie des champs doit rester une correction de CE draft — jamais un
        nouveau départ qui en perdrait le reste."""
        state = _abandoned_sales_publish_state()
        state["sales_publish_draft"] = {
            "draft_id": "already-open",
            "product": "maïs",
            "quantity": 300.0,
            "price": 5000.0,
            "unit": "SAC",
            "status": "DRAFT",
        }
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _DeviationThenNewTaskLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "SALES_PUBLISH_PRODUCT",
                "confidence": 0.95,
                "entities": {"quantity": 350.0},
            }
        )

        state = run(_run_turn(state, interpreter, runtime, text="finalement plutôt 350 sacs"))

        draft = state.get("sales_publish_draft")
        assert draft is not None and draft.get("draft_id") == "already-open", (
            f"un draft déjà ouvert ne doit jamais être remplacé par une simple correction : {draft!r}"
        )
