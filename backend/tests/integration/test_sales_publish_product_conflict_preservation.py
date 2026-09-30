"""SALES_PUBLISH_PRODUCT — préservation du produit explicite face au fast-path
numérique (2026-09-30).

Rejoue, sur la VRAIE chaîne de nœuds (input_interpreter -> cognitive_guard ->
goal_planner -> memory_update -> validator — même méthode que
`test_sales_publish_cross_flow_state_leak.py`), l'incident réel :

    User: "je veux vendre mon lait"
    Agent: [demande la quantité]           <- état de départ simulé ici
    User: "J'ai 60 L de miel"
    AVANT ce correctif : "miel" disparaissait silencieusement (fast-path
        `fast_path_slot_numeric_answer`), `cognitive_guard::_entity_carry_forward`
        réinjectait "lait", et le système continuait comme si l'utilisateur
        avait proposé 60 L de LAIT.

Deux scénarios :
  * le cas bogué ("60 L de miel") -> le fast-path doit s'abstenir, le LLM
    (micro-prompt ACTIVE_SLOT) doit être consulté, et son extraction
    `product=miel` doit atteindre la logique de conflit déjà correcte de
    `nodes/memory.py::_apply_slot` — jamais de draft "60 L de lait".
  * le cas de contrôle ("60 L", pas de produit explicite) -> comportement
    INCHANGÉ : fast-path utilisé, LLM jamais appelé (`ForbiddenLLM`), product
    reste "lait" par carry-forward normal, quantity=60L.
"""
from __future__ import annotations

import typing
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `patch` dans `state` via le VRAI reducer LangGraph de chaque champ
    (même fonction que `test_sales_publish_cross_flow_state_leak.py::apply_patch`,
    dupliquée ici à dessein — voir sa docstring)."""
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        new_state[key] = value if reducer is None else reducer(state.get(key), value)
    return new_state


def _mid_sales_publish_asking_quantity() -> Dict[str, Any]:
    """État après « je veux vendre mon lait » : produit déjà connu, quantité en
    attente — exactement le point où l'incident réel se produit."""
    return make_state(
        expected_input="QUANTITY",
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload={"product": "lait"},
        user_role="PRODUCER",
        user_phone="+22670000099",
        sales_publish_draft=None,
    )


async def _run_turn(state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str) -> Dict[str, Any]:
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


class TestExplicitProductNeverSilentlyReplacedByFastPath:
    def test_explicit_different_product_reaches_conflict_logic_never_a_lait_draft(self):
        state = _mid_sales_publish_asking_quantity()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        # Le fast-path s'abstient (voir tests/interpreter/
        # test_fastpath_product_preservation.py) -> le tour route vers le
        # micro-prompt ACTIVE_SLOT, qui, correctement instruit, classe ceci
        # comme une réponse complète au slot QUANTITY portant EN PLUS un
        # produit explicite (voir `active_slot_prompts.py` : extraction
        # multi-champs légitime pour ANSWER).
        runtime.llm = ScriptedLLM(
            {
                "disposition": "ANSWER",
                "extracted_entities": {
                    "quantity": 60.0,
                    "unit": "LITRE",
                    "product": "miel",
                },
                "confidence": 0.9,
            }
        )

        state = run(_run_turn(state, interpreter, runtime, text="J'ai 60 L de miel"))

        assert runtime.llm.calls == 1, (
            "le fast-path aurait dû s'abstenir et laisser la main au LLM "
            "(micro-prompt ACTIVE_SLOT) — sinon 'miel' n'a jamais pu être vu"
        )

        payload = state.get("transaction_payload") or {}
        stable = state.get("stable_entities") or {}
        # L'invariant central : jamais "lait" avec 60 L attaché.
        assert not (payload.get("product") == "lait" and payload.get("quantity") == 60.0), (
            f"BUG reproduit : 60 L attaché à 'lait' au lieu de 'miel' — payload={payload!r}"
        )
        # "miel" n'a pas été perdu : il doit apparaître quelque part dans l'état
        # métier résultant (payload courant, ou trace de correction/mémoire
        # stable) — la logique de conflit de `_apply_slot` a AU MOINS vu la
        # valeur, qu'elle l'accepte, la clarifie ou la bloque.
        seen_miel = payload.get("product") == "miel" or stable.get("product") == "miel"
        recent = state.get("recent_corrections") or []
        seen_miel = seen_miel or any(
            isinstance(c, dict) and c.get("field") == "product" and c.get("to") == "miel"
            for c in recent
        )
        assert seen_miel, (
            f"'miel' semble avoir disparu sans laisser de trace — payload={payload!r} "
            f"stable={stable!r} recent_corrections={recent!r}"
        )
        # Aucune publication prématurée tant que le conflit n'est pas résolu :
        # pas de draft déjà en confirmation/exécution avec les valeurs de ce tour.
        draft = state.get("sales_publish_draft")
        assert draft is None or draft.get("status") not in ("EXECUTING", "PUBLISHED"), (
            f"publication prématurée malgré le conflit produit non résolu : draft={draft!r}"
        )

    def test_control_bare_quantity_unaffected_fast_path_still_used_no_llm(self):
        state = _mid_sales_publish_asking_quantity()
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        # Preuve déterministe que le chemin reste rapide : tout appel LLM ici
        # ferait échouer le tour (voir `ForbiddenLLM`).
        runtime.llm = ForbiddenLLM()

        state = run(_run_turn(state, interpreter, runtime, text="60 L"))

        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "lait", (
            f"le produit courant ne doit pas bouger sur une réponse sans produit explicite : {payload!r}"
        )
        assert payload.get("quantity") == 60.0
        assert payload.get("unit") == "LITRE"
        missing = state.get("missing_fields") or []
        assert "quantity" not in missing, f"quantity aurait dû être résolue : missing={missing!r}"


class TestExplicitProductPreservationWithoutLLM:
    """(mandat 2026-09-30, §8) — mêmes deux scénarios que la classe ci-dessus,
    mais LLM RÉELLEMENT indisponible sur le runtime (`StubRuntime()` sans LLM
    scripté, `runtime.llm` reste `None`) : la sûreté métier ne doit PAS en
    dépendre."""

    def test_explicit_different_product_never_corrupted_when_llm_unavailable(self):
        state = _mid_sales_publish_asking_quantity()
        runtime = StubRuntime()  # llm=None : aucun LLM sur ce runtime.
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="J'ai 60 L de miel"))

        payload = state.get("transaction_payload") or {}
        # L'invariant central, sans LLM cette fois : jamais 60 L attaché à
        # "lait" simplement parce qu'aucun LLM n'était là pour voir "miel".
        assert not (payload.get("product") == "lait" and payload.get("quantity") == 60.0), (
            f"BUG reproduit SANS LLM : 60 L attaché à 'lait' — payload={payload!r}"
        )
        # Le fast-path s'est abstenu ET il n'y avait nulle part d'autre où
        # renvoyer le message (pas de LLM) -> repli sûr SANS MUTATION déjà
        # existant (`interpreted_event="UNKNOWN"`, voir `_input_interpreter_impl`) :
        # aucune quantité n'est appliquée du tout, `quantity` reste manquant,
        # `expected` ne progresse pas silencieusement vers PRICE.
        missing = state.get("missing_fields") or []
        assert "quantity" in missing, (
            f"quantity ne doit pas être résolue silencieusement sans LLM : missing={missing!r}"
        )
        assert "price" not in (state.get("transaction_payload") or {}), (
            "aucune progression silencieuse vers PRICE ne doit avoir lieu"
        )
        # Aucune publication prématurée.
        draft = state.get("sales_publish_draft")
        assert draft is None or draft.get("status") not in ("EXECUTING", "PUBLISHED"), (
            f"publication prématurée sans LLM disponible : draft={draft!r}"
        )
        # Le tunnel reste actif en attente sûre (WAITING_INPUT), pas d'état cassé.
        assert state.get("status") == "WAITING_INPUT", (
            f"l'état doit rester en attente sûre, pas progresser ni casser : status={state.get('status')!r}"
        )

    def test_control_bare_quantity_unaffected_when_llm_unavailable(self):
        """Cas de contrôle du §8 : même flow, mais l'utilisateur répond « 60 L »
        (pas de produit explicite) -> comportement normal conservé, LLM jamais
        nécessaire (le fast-path suffit)."""
        state = _mid_sales_publish_asking_quantity()
        runtime = StubRuntime()  # llm=None, mais ForbiddenLLM prouverait la même chose.
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="60 L"))

        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "lait"
        assert payload.get("quantity") == 60.0
        assert payload.get("unit") == "LITRE"
        missing = state.get("missing_fields") or []
        assert "quantity" not in missing
