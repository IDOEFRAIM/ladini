"""Contrat d'action structurée producteur/palier/nombre de paquets.

Audit 2026-09-01 (2e passe, directive explicite utilisateur "LLM pour
l'interprétation, code pour l'exécution"). Reproduit et verrouille l'incident
LIVE rapporté ce jour :

    USER: je veux acheter du lait
    AGENT: 1. jojo — 450 FCFA/L
           2. jojo — 500 FCFA/L
    USER: 1
    AGENT: 1️⃣ 5 L (bidon) — 450 FCFA
           2️⃣ 1 L (bidon) — 800 FCFA
    USER: LE PREMIER, C'EST À DIRE 5 L
    AGENT: Veuillez choisir une option :        <-- BUG : re-montre le menu
           1. jojo...                               PRODUCTEUR

Root cause confirmée par audit (voir domain/selection_actions.py) :
`expected_candidates` (canal générique AG-UI, rempli par `ui_engine.py` au
moment du menu PRODUCTEUR) n'était jamais invalidé quand le menu de PALIERS
prenait le relais — un LLM voyant les deux contextes simultanément pouvait
mapper un ordinal ("le premier") sur la MAUVAISE liste. Même dans les cas où
le mapping du palier réussissait quand même, le même "5 L" se faisait aussi
capter comme une fausse quantité par le fallback regex, plantant la
résolution en aval.

Ce fichier prouve que le nouveau contrat (LLM → `agent_action` structuré →
validation contre le contexte RECONSTRUIT à cet instant → exécution) élimine
la classe de bug entière : le contexte n'est plus jamais lu depuis un canal
générique périmé, et un seul champ (`agent_action`) porte la décision — sans
quantité/unité concurrente possible dans le même tour.
"""
from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
)
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.ui_engine import ui_engine
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import ScriptedLLM, StubRuntime, run
from tests.integration.test_tier_selection_full_node_chain import apply_patch
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)


def _vendor_a() -> Dict[str, Any]:
    return {
        "product_id": "P-LAIT-1", "name": "lait", "price": 450.0, "unit": "LITRE",
        "vendor_name": "jojo", "producer_id": "PR-1", "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": [
            {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 450.0,
             "packaging": "bidon", "base_unit_quantity": 5.0, "min_order_quantity": 1},
            {"tier_id": "t1", "quantity": 1.0, "unit": "L", "price": 800.0,
             "packaging": "bidon", "base_unit_quantity": 1.0, "min_order_quantity": 1},
        ],
    }


def _vendor_b() -> Dict[str, Any]:
    return {
        "product_id": "P-LAIT-2", "name": "lait", "price": 500.0, "unit": "LITRE",
        "vendor_name": "jojo", "producer_id": "PR-2", "source_type": "DIRECT",
        "is_auction": False, "pricing_tiers": None,
    }


def _patch_two_vendors(monkeypatch) -> None:
    import ladini.graphs.agents.market_coach.services.domain.cart_service as m

    async def _fake(self, phone, product_name):
        return [_vendor_a(), _vendor_b()], True

    monkeypatch.setattr(m.CartDomainService, "resolve_product_vendors", _fake)


def _tb(state, runtime):
    state = apply_patch(state, run(state_cleaner_node(state, runtime)))
    return apply_patch(state, run(post_response_cleanup(state, runtime)))


def _open(runtime) -> Dict[str, Any]:
    return {
        "current_goal": "BUYER_ADD_TO_CART", "expected_input": "NONE",
        "status": "PROCESSING", "normalized_text": "je veux acheter du lait",
        "user_query": "je veux acheter du lait", "user_phone": "+22601479800",
        "transaction_payload": {"product": "lait"}, "working_memory": {},
        "active_cart": [],
    }


class TestLiveIncidentProducerThenTierByFreeText:
    """Le scénario exact rapporté en direct : jamais de retour au menu
    producteur pour "LE PREMIER, C'EST À DIRE 5 L"."""

    def test_full_sequence_never_reshows_the_producer_menu(self, monkeypatch):
        _patch_two_vendors(monkeypatch)
        interpreter = make_input_interpreter("BUYER")
        runtime = StubRuntime(responses={
            "validate_stock_availability_atomic": {
                "status": "success", "available_quantity": 500,
                "unit": "LITRE", "unit_price": 450.0,
            },
        })

        # --- Turn 1: "je veux acheter du lait" -> menu PRODUCTEUR --------
        state = _open(runtime)
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = apply_patch(state, run(ui_engine(state, runtime)))
        assert "jojo — 450.0 FCFA/LITRE" in state["final_response"]
        assert "jojo — 500.0 FCFA/LITRE" in state["final_response"]
        assert build_selection_context(state).expected_action == ActionType.SELECT_PRODUCER
        state = _tb(state, runtime)

        # --- Turn 2: "1" (bare digit) -> producer #1 selected ------------
        state["normalized_text"] = "1"
        state["user_query"] = "1"
        interp2 = run(interpreter(state, runtime))
        assert interp2["extracted_entities"].get("agent_action") == "SELECT_PRODUCER"
        assert interp2["extracted_entities"].get("action_producer_id") == "PR-1"
        assert interp2["raw_analysis"]["path"] == "fast_path_selection_action"
        state = apply_patch(state, interp2)
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = apply_patch(state, run(ui_engine(state, runtime)))
        assert "5.0 L" in state["final_response"] and "1.0 L" in state["final_response"]
        assert build_selection_context(state).expected_action == ActionType.SELECT_PRICING_TIER
        state = _tb(state, runtime)

        # --- Turn 3: "LE PREMIER, C'EST À DIRE 5 L" (free text) ----------
        # (2026-09-12, Incrément D) : ce tour route désormais vers le
        # micro-prompt STRUCTURED_ACTION dédié
        # (`interpreter/structured_action_micro.py`) — il ne voit QUE des
        # labels humains numérotés (JAMAIS `tier_id`), et répond avec
        # "selection_index" (1-based) — c'est PYTHON qui résout l'index
        # (1) vers le VRAI tier_id ("t5", 5 L — premier de la liste
        # construite par `build_selection_context` depuis
        # `_vendor_a().pricing_tiers`). On scripte donc la sortie humaine
        # attendue de ce nouveau contrat, pas l'ancien format à base d'id.
        runtime.llm = ScriptedLLM({
            "disposition": "ACTION",
            "action": "SELECT_PRICING_TIER",
            "selection_index": 1,
            "confidence": 0.97,
        })
        state["normalized_text"] = "LE PREMIER, C'EST À DIRE 5 L"
        state["user_query"] = "LE PREMIER, C'EST À DIRE 5 L"
        interp3 = run(interpreter(state, runtime))
        state = apply_patch(state, interp3)
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = apply_patch(state, run(ui_engine(state, runtime)))

        # LE CRITÈRE DE SUCCÈS : jamais le menu producteur.
        assert "jojo — 450" not in state["final_response"]
        assert "jojo — 500" not in state["final_response"]
        assert "Veuillez choisir une option" not in state["final_response"]
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert "5.0 L" in state["final_response"]
        assert not state["active_cart"], "aucune quantité n'a encore été donnée"
        assert state["vendor_selection_context"]["resolved_tier_id"] == "t5"
        state = _tb(state, runtime)

        # --- Turn 4: "28" -> 28 bidons de 5 L -----------------------------
        runtime.llm = None  # chiffre nu -> chemin déterministe
        state["normalized_text"] = "28"
        state["user_query"] = "28"
        interp4 = run(interpreter(state, runtime))
        assert interp4["extracted_entities"].get("agent_action") == "SET_PACKAGE_COUNT"
        assert interp4["extracted_entities"].get("action_package_count") == 28.0
        state = apply_patch(state, interp4)
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert state["status"] == "COMPLETED", state.get("final_response")
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t5"
        assert line["quantity"] == 28
        assert line["base_unit_quantity"] == 140.0  # 28 x 5L
        assert line["line_total"] == 12600.0  # 28 x 450 FCFA


class TestSelectProducerRejectsAWrongIntentDuringTierSelection:
    """Cas 4 du cahier des charges : une action SELECT_PRODUCER reçue
    pendant SELECT_PRICING_TIER (contexte périmé / LLM en défaut) doit être
    rejetée — jamais exécutée aveuglément."""

    def test_a_producer_action_is_rejected_once_a_tier_menu_is_active(
        self, monkeypatch
    ):
        _patch_two_vendors(monkeypatch)
        runtime = StubRuntime()
        state = _open(runtime)
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = apply_patch(state, run(ui_engine(state, runtime)))
        state = _tb(state, runtime)

        interpreter = make_input_interpreter("BUYER")
        state["normalized_text"] = "1"
        state["user_query"] = "1"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = apply_patch(state, run(ui_engine(state, runtime)))
        assert build_selection_context(state).expected_action == ActionType.SELECT_PRICING_TIER
        state = _tb(state, runtime)

        # Un LLM en défaut (ou périmé) renvoie une action SELECT_PRODUCER —
        # structurellement incohérente avec l'étape actuelle.
        runtime.llm = ScriptedLLM({
            "interpreted_event": "SELECTION",
            "detected_intent": "BUYER_ADD_TO_CART",
            "confidence": 0.9,
            "extracted_entities": {
                "agent_action": "SELECT_PRODUCER",
                "action_producer_id": "PR-2",
            },
        })
        state["normalized_text"] = "n'importe quoi"
        state["user_query"] = "n'importe quoi"
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        before_vctx = dict(state.get("vendor_selection_context") or {})
        state = apply_patch(state, run(cart_management(state, runtime)))

        # Rejetée : le vendeur choisi (PR-1) ne doit PAS avoir changé pour
        # PR-2, et aucun palier ne doit avoir été silencieusement résolu.
        chosen = (state.get("vendor_selection_context") or {}).get("chosen_vendor") or {}
        assert chosen.get("producer_id") == "PR-1"
        assert not state["active_cart"]


class TestUnknownIdIsNeverTrusted:
    """Règle 10 : même une action syntaxiquement valide portant un id qui
    n'appartient pas au contexte ACTIF ne doit rien modifier."""

    def test_a_hallucinated_tier_id_is_rejected(self, monkeypatch):
        import ladini.graphs.agents.market_coach.services.domain.cart_service as m

        async def _fake(self, phone, product_name):
            return [_vendor_a()], False

        monkeypatch.setattr(m.CartDomainService, "resolve_product_vendors", _fake)

        runtime = StubRuntime()
        state = _open(runtime)
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = apply_patch(state, run(ui_engine(state, runtime)))
        assert build_selection_context(state).expected_action == ActionType.SELECT_PRICING_TIER
        state = _tb(state, runtime)

        runtime.llm = ScriptedLLM({
            "interpreted_event": "SELECTION",
            "detected_intent": "BUYER_ADD_TO_CART",
            "confidence": 0.9,
            "extracted_entities": {
                "agent_action": "SELECT_PRICING_TIER",
                "action_pricing_tier_id": "invented-id-999",
            },
        })
        state["normalized_text"] = "celui-là"
        state["user_query"] = "celui-là"
        interpreter = make_input_interpreter("BUYER")
        state = apply_patch(state, run(interpreter(state, runtime)))
        state = apply_patch(state, run(memory_update(state, runtime)))
        state = apply_patch(state, run(validator(state, runtime)))
        state = apply_patch(state, run(cart_management(state, runtime)))

        assert (state.get("vendor_selection_context") or {}).get("resolved_tier_id") is None
        assert not state["active_cart"]
