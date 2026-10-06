"""P1-3 (audit architectural 2026-09-08) : le garde-fou de plus haute
priorité de `interpreter/strategy.py::response_strategy` — celui qui doit
forcer une clarification quand l'extraction LLM structurée
(`services/domain/slot_enrichment.py::llm_extract_quantity_unit`) échoue
explicitement — lisait `state.get("slot_enrichment_force_clarification")`
en RACINE d'état, alors que l'écrivain le posait dans `transaction_payload`
(même mésadresse pour `clarification_reasons`). Deux adresses différentes :
ce mécanisme n'a jamais pu se déclencher en production.

Root cause exacte + correctif : `nodes/memory.py` est l'UNIQUE appelant de
`enrich_payload_from_text` — il relaie désormais explicitement les deux
champs vers la racine de l'état où `strategy.py` les cherche déjà, plutôt
que de créer une 2e source de vérité.

Ce fichier prouve la chaîne RÉELLE écrivain -> lecteur, pas une
réimplémentation : `memory_update` avec `enrich_payload_from_text` monkeypatché
pour lever `SlotValidationError` (le déclencheur réel), enchaîné avec le
vrai `response_strategy`."""
from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.interpreter.strategy import (
    response_strategy,
)
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from tests.conftest import StubRuntime, make_state, run
from tests.harness.state import clears


class TestMemoryUpdateRelaysTheFlagToStateRoot:
    def test_a_real_slot_validation_error_reaches_the_state_root(self, monkeypatch):
        """Reproduit le déclencheur RÉEL : `llm_extract_quantity_unit` lève
        `SlotValidationError` (ex: le LLM a halluciné "kg" comme produit,
        incident réel documenté dans ce module) — le drapeau doit atteindre
        la racine de l'état, pas seulement `transaction_payload`."""
        import ladini.graphs.agents.market_coach.services.domain.slot_enrichment as se_mod

        async def _raise_validation_error(mc_runtime, text):
            raise se_mod.SlotValidationError("42 kg' n'est pas un produit valide")

        monkeypatch.setattr(
            se_mod, "llm_extract_quantity_unit", _raise_validation_error
        )
        # Force l'appel LLM : `_needs_structured_extraction` doit renvoyer
        # True — un produit "sale" contenant une unité suffit à le garantir.
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="42 kg",
            user_query="42 kg",
            transaction_payload={"product": "42 kg"},
        )

        result = run(memory_update(state, StubRuntime()))

        assert result.get("slot_enrichment_force_clarification") is True, (
            "le drapeau doit atteindre la RACINE de l'état (result), pas "
            "seulement rester coincé dans transaction_payload"
        )
        assert result.get("clarification_reasons"), (
            "les raisons doivent suivre le même relais que le drapeau"
        )
        payload = result.get("transaction_payload") or {}
        assert clears(payload, "slot_enrichment_force_clarification"), (
            "le drapeau ne doit plus polluer transaction_payload une fois "
            "relayé — sinon il pourrait fuiter dans un récapitulatif ou un "
            "argument MCP résolu depuis ce dict"
        )
        assert clears(payload, "clarification_reasons")

    def test_no_validation_error_means_no_flag_at_all(self):
        """Non-régression : le chemin nominal (pas d'échec LLM) ne doit
        jamais poser ces clés — ni en racine ni dans transaction_payload."""
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="",
            user_query="",
            transaction_payload={"product": "maïs", "quantity": 50, "unit": "KG"},
        )
        result = run(memory_update(state, StubRuntime()))
        assert "slot_enrichment_force_clarification" not in result
        assert (result.get("transaction_payload") or {}).get(
            "slot_enrichment_force_clarification"
        ) is None


class TestFullChainMemoryUpdateThenResponseStrategy:
    """La preuve bout-en-bout : les DEUX vraies fonctions, dans l'ordre réel
    du graphe (memory_update -> ... -> response_strategy), sans
    réimplémentation de la logique de l'une ou l'autre."""

    def test_the_guard_actually_fires_end_to_end(self, monkeypatch):
        import ladini.graphs.agents.market_coach.services.domain.slot_enrichment as se_mod

        async def _raise_validation_error(mc_runtime, text):
            raise se_mod.SlotValidationError("quantité illisible")

        monkeypatch.setattr(
            se_mod, "llm_extract_quantity_unit", _raise_validation_error
        )
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="beaucoup kg",
            user_query="beaucoup kg",
            transaction_payload={"product": "beaucoup kg"},
        )

        mem_patch = run(memory_update(state, StubRuntime()))
        merged: Dict[str, Any] = {**state, **mem_patch}

        strat_patch = run(response_strategy(merged, mc_runtime=None))

        assert strat_patch["response_strategy"] == "CLARIFICATION", (
            "le garde-fou de plus haute priorité doit gagner — c'était "
            "exactement le mécanisme mort avant ce correctif"
        )
        assert strat_patch["status"] == "WAITING_INPUT"
