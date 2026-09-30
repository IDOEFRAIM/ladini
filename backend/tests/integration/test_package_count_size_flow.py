"""Réplique réaliste du flow SALES_PUBLISH_PRODUCT (Étape 3, 2026-09-30) sur
la VRAIE chaîne de nœuds (input_interpreter -> cognitive_guard -> goal_planner
-> memory_update -> validator) — même méthode que
`test_sales_publish_product_conflict_preservation.py` (Étape 1).

    User: "je veux vendre mon miel"
    Agent: (demande la quantité)
    User: "j'ai 50 pots de 4 litre"

Attendu : product=miel, PackageDefinition(count=50, label=POT, content=4/LITRE),
InventoryQuantity(200, LITRE) — jamais quantity=4L, jamais perte du "50", aucun
pricing tier créé à ce stade (§9 du mandat).
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
from tests.conftest import ForbiddenLLM, StubRuntime, make_state, run

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
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        new_state[key] = value if reducer is None else reducer(state.get(key), value)
    return new_state


def _mid_sales_publish_asking_quantity(product: str = "miel") -> Dict[str, Any]:
    return make_state(
        expected_input="QUANTITY",
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload={"product": product},
        user_role="PRODUCER",
        user_phone="+22670000099",
        sales_publish_draft=None,
    )


async def _run_turn(state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str) -> Dict[str, Any]:
    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    interp = await interpreter(state, runtime)
    state = apply_patch(state, interp)

    cg = await cognitive_guard(state, runtime)
    state = apply_patch(state, cg)

    from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
        goal_planner,
    )

    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    return state


class TestGenericPackageCountSizeFlow:
    def test_50_pots_de_4_litre_derives_200L_never_4L_never_loses_50(self):
        state = _mid_sales_publish_asking_quantity()
        runtime = StubRuntime()
        # Preuve déterministe : le nouveau fast-path résout ce message sans
        # jamais consulter le LLM.
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 50 pots de 4 litre"))

        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("package_count") == 50
        assert payload.get("package_label") == "POT"
        assert payload.get("package_size") == 4.0
        assert payload.get("package_unit") == "LITRE"

        offer = payload.get("commercial_offer")
        assert offer is not None, f"aucune commercial_offer construite : payload={payload!r}"
        assert offer["inventory_quantity"]["amount"] == 200.0
        assert offer["inventory_quantity"]["unit"] == "LITRE"
        assert offer["package"]["count"] == 50
        assert offer["package"]["package_type"] == "POT"
        assert offer["package"]["content_amount"] == 4.0

        # Interdictions explicites du mandat (§20) :
        assert payload.get("quantity") != 4.0, "jamais quantity=4 L (la taille d'UN pot)"
        assert payload.get("quantity") != 50.0, "jamais quantity=50 L (le compte pris pour des litres)"
        assert offer["inventory_quantity"]["amount"] != 4.0
        assert offer["inventory_quantity"]["amount"] != 50.0

        # "quantité" désormais connue pour le validator (missing_fields).
        missing = state.get("missing_fields") or []
        assert "quantity" not in missing

        # Aucun pricing tier créé à ce stade (§9 : ce n'est pas un tarif).
        assert not payload.get("pricing_tiers")
        assert offer.get("pricing") is None

        # Aucune publication prématurée.
        draft = state.get("sales_publish_draft")
        assert draft is None or draft.get("status") not in ("EXECUTING", "PUBLISHED")

    def test_control_bare_60L_unaffected(self):
        """Cas de contrôle §11 : le cas simple reste inchangé."""
        state = _mid_sales_publish_asking_quantity()
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="60 L"))

        payload = state.get("transaction_payload") or {}
        assert payload.get("quantity") == 60.0
        assert payload.get("unit") == "LITRE"
        assert payload.get("package_count") is None
        missing = state.get("missing_fields") or []
        assert "quantity" not in missing


class TestOtherPackagedReplays:
    def test_20_sacs_de_50kg(self):
        state = _mid_sales_publish_asking_quantity(product="mais")
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="20 sacs de 50 kg"))

        payload = state.get("transaction_payload") or {}
        offer = payload.get("commercial_offer")
        assert offer is not None
        assert offer["inventory_quantity"]["amount"] == 1000.0
        assert offer["inventory_quantity"]["unit"] == "KG"
        assert offer["package"]["count"] == 20
        assert offer["package"]["package_type"] == "SAC"

    def test_10_cageots_de_15kg(self):
        state = _mid_sales_publish_asking_quantity(product="tomates")
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        state = run(_run_turn(state, interpreter, runtime, text="10 cageots de 15 kg"))

        payload = state.get("transaction_payload") or {}
        offer = payload.get("commercial_offer")
        assert offer is not None
        assert offer["inventory_quantity"]["amount"] == 150.0
        assert offer["inventory_quantity"]["unit"] == "KG"
        assert offer["package"]["count"] == 10
        assert offer["package"]["package_type"] == "CAGEOT"
