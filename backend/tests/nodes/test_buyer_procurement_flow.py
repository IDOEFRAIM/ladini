"""`flows/buyer/procurement.py` — escalade vers un appel d'offres (auction)
quand le catalogue n'a pas le produit, plus la gestion des offres reçues.

`cart_management` (import direct depuis `.cart`) est doublé par un stub :
ce fichier teste le ROUTAGE de procurement.py, pas cart.py (couvert
ailleurs). Les appels MCP passent par `StubRuntime` (tests/conftest.py).
"""
from __future__ import annotations

from datetime import datetime

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from tests.conftest import StubRuntime, make_state, run
from tests.harness.state import clears


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _patch_cart_management(monkeypatch, result=None):
    import ladini.graphs.agents.market_coach.flows.buyer.procurement as mod

    calls = []

    async def _fake_cart_management(state, mc_runtime):
        calls.append(state)
        return result or {"status": "COMPLETED", "final_response": "cart_management called"}

    monkeypatch.setattr(mod, "cart_management", _fake_cart_management)
    return calls


# =====================================================================
# build_procurement_escalation
# =====================================================================

class TestBuildProcurementEscalation:
    def test_missing_fields_computed_in_order(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        result = build_procurement_escalation({}, {}, "mais", "KG", "msg")
        assert result["missing_fields"] == ["price", "quantity"]
        assert result["last_missing_field"] == "price"
        assert to_tunnel_category(get_pending_interaction(result)) == "PRICE"

    def test_deadline_is_auto_filled_when_absent(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        result = build_procurement_escalation({}, {}, "mais", "KG", "msg")
        assert result["form_data"]["deadline"]
        datetime.fromisoformat(result["form_data"]["deadline"])  # doit être un ISO valide

    def test_deadline_already_in_payload_is_not_overwritten_with_a_default(self):
        """Le garde-fou ne force PAS un défaut dans `form_data` si le payload
        porte déjà une deadline — mais `missing_fields` se base sur
        `form_data` uniquement, donc le champ reste listé "manquant" tant que
        rien ne le recopie dans `form_data` (comportement actuel vérifié,
        pas un défaut à synchroniser dans ce fichier)."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        result = build_procurement_escalation({"deadline": "2030-01-01"}, {}, "mais", "KG", "msg")
        assert "deadline" not in result["form_data"]  # jamais forcé en doublon

    def test_existing_form_data_is_preserved_and_extended(self):
        """Réentrée dans un formulaire déjà actif : le progrès (prix, qty)
        déjà collecté ne doit JAMAIS être effacé."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        existing = {"price": 250, "quantity": 100, "product": "mais"}
        result = build_procurement_escalation({}, {}, "mais", "KG", "msg", existing_form_data=existing)
        assert result["form_data"]["price"] == 250
        assert result["form_data"]["quantity"] == 100
        assert result["missing_fields"] == ["deadline"] or result["missing_fields"] == []

    def test_selection_fields_are_always_reset(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        payload = {"selection_index": 3, "selected_value": "abc"}
        result = build_procurement_escalation(payload, {}, "mais", "KG", "msg")
        assert result["transaction_payload"]["selection_index"] is None
        assert result["transaction_payload"]["selected_value"] is None

    def test_auto_quantity_fill_flag_resets_quantity(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        payload = {"quantity": 50, "_auto_quantity_fill": True}
        result = build_procurement_escalation(payload, {}, "mais", "KG", "msg")
        assert result["transaction_payload"]["quantity"] is None
        assert clears(result["transaction_payload"], "_auto_quantity_fill")

    def test_working_memory_locks_the_goal_and_clears_stale_flags(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        wm = {"buyer_request_waiting_choice": True, "buyer_request_catalog_checked": True}
        result = build_procurement_escalation({}, wm, "mais", "KG", "msg")
        assert result["working_memory"]["active_goal"] == "PROCUREMENT_CREATE_REQUEST"
        assert result["working_memory"]["buyer_request_waiting_choice"] is None
        assert result["active_form"] == "AUCTION_CREATE"

    def test_no_missing_fields_yields_none_expected_input(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            build_procurement_escalation,
        )
        existing = {"price": 250, "quantity": 100, "deadline": "2030-01-01", "product": "mais"}
        result = build_procurement_escalation({}, {}, "mais", "KG", "msg", existing_form_data=existing)
        assert result["missing_fields"] == []
        assert to_tunnel_category(get_pending_interaction(result)) == "NONE"


# =====================================================================
# buyer_request_resolver
# =====================================================================

class TestBuyerRequestResolverEarlyExits:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        result = run(buyer_request_resolver(make_state(user_phone=""), rt()))
        assert result["response_strategy"] == "ERROR"

    def test_vendor_ctx_active_with_selection_routes_to_cart_management(self, monkeypatch):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        calls = _patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            vendor_selection_context={"product": "mais", "chosen_vendor": {"vendor_name": "Awa"}},
            transaction_payload={"selection_index": 1, "product": "mais"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["final_response"] == "cart_management called"
        assert calls[0]["current_goal"] == "BUYER_ADD_TO_CART"

    def test_vendor_ctx_active_chosen_vendor_no_selection_routes_to_cart_for_quantity(self, monkeypatch):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        calls = _patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            vendor_selection_context={"product": "mais", "chosen_vendor": {"vendor_name": "Awa"}},
            transaction_payload={"product": "mais"},
            normalized_text="",
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["final_response"] == "cart_management called"

    def test_stale_vendor_ctx_for_a_different_product_is_invalidated(self, monkeypatch):
        """Si l'acheteur change de produit, le vendor_ctx périmé (autre
        produit) ne doit PAS forcer un routage cart_management — le flux
        catalogue normal reprend avec le NOUVEAU produit."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        calls = _patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            vendor_selection_context={"product": "tomates", "chosen_vendor": {"vendor_name": "Awa"}},
            transaction_payload={"product": "oignons"},
        )
        runtime = rt({"search_products": {"status": "success", "results": []}})
        result = run(buyer_request_resolver(state, runtime))
        assert calls == [], "cart_management ne doit pas être appelé pour un produit différent"
        assert "oignons" in result["final_response"] or result["response_strategy"] == "ASK_MISSING_FIELD"


class TestBuyerRequestResolverEscalation:
    def test_escalate_keyword_with_known_product(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", normalized_text="appel", transaction_payload={"product": "mais"})
        result = run(buyer_request_resolver(state, rt()))
        assert result["active_form"] == "AUCTION_CREATE"

    def test_escalation_message_asks_for_a_ceiling_price_not_a_minimum(self):
        """Régression production (2026-08) : le message d'escalade demandait
        un « prix minimum » alors que le récap affiché juste après (même
        chiffre) et le champ métier réel désignent un « prix plafond »
        (enchère inversée — les producteurs doivent proposer EN DESSOUS).
        Le décalage de vocabulaire faisait croire à l'utilisateur que l'agent
        avait mal compris son prix, le poussant à rejeter la confirmation."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", normalized_text="appel", transaction_payload={"product": "mais"})
        result = run(buyer_request_resolver(state, rt()))
        assert "prix minimum" not in result["final_response"].lower()
        assert "prix plafond" in result["final_response"].lower()

    def test_re_entering_an_active_auction_create_form_preserves_progress(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(
            user_phone="+2260",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            active_form="AUCTION_CREATE",
            form_data={"price": 250, "product": "mais"},
            transaction_payload={"product": "mais"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["form_data"]["price"] == 250

    def test_waiting_choice_confirm_via_event(self):
        """(2026-09-03, refonte transactionnelle, mandat §10/§11) : ce choix
        est déjà posé via `PendingInteraction(kind=CONFIRM_ACTION)` — le
        SEUL signal canonique fiable est `interpreted_event`, produit par le
        même contrat fast-path/LLM que toute autre confirmation
        (`_CONFIRM_EXACT_PHRASES`, `interpreter/routing.py`). Un ancien
        2e moteur de reconnaissance oui/non par texte brut
        (`CONFIRM_KEYWORDS`/`DECLINE_KEYWORDS`, testé ici auparavant via
        `normalized_text="oui"`/`"non"` SANS `interpreted_event`) a été
        supprimé — ces deux tests couvraient exactement ce mécanisme retiré."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(
            user_phone="+2260",
            working_memory={"buyer_request_waiting_choice": True},
            interpreted_event="CONFIRM",
            transaction_payload={"product": "mais"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["active_form"] == "AUCTION_CREATE"

    def test_waiting_choice_reject_via_event_resets_and_completes(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(
            user_phone="+2260",
            working_memory={"buyer_request_waiting_choice": True},
            interpreted_event="REJECT",
            transaction_payload={"product": "mais"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["status"] == "COMPLETED"
        assert result["working_memory"]["buyer_request_waiting_choice"] is None
        assert result["transaction_payload"] == {"__reset__": True}


class TestWaitingChoiceConfirmRoutesToTheFlowNotTheExecutor:
    """Incident réel (2026-09-09) : « produit introuvable → *oui* pour l'appel
    d'offres » plantait avec `RuntimeError("Action non découverte. Migration
    incomplète.")`. Le `PendingInteraction(kind=CONFIRM_ACTION)` posé par
    `buyer_request_waiting_choice` faisait router le « oui » vers
    `confirmation_gate` → `mcp_tool_executor`, or `BUYER_REQUEST` n'a aucune
    action MCP. Ce goal doit rester sur le flux nominal (`context_resolver` →
    `buyer_request_resolver`, qui gère lui-même CONFIRM/REJECT)."""

    def _router(self):
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )
        return make_route_after_validator("BUYER")

    def test_buyer_request_confirm_action_pending_does_not_go_to_confirmation_gate(self):
        state = make_state(
            user_phone="+2260",
            current_goal="BUYER_REQUEST",
            interpreted_event="CONFIRM",
            expected_input="CONFIRMATION",
            working_memory={"buyer_request_waiting_choice": True},
            transaction_payload={"product": "tomates"},
        )
        assert self._router()(state) != "to_confirmation"
        assert self._router()(state) == "to_resolver"

    def test_a_real_write_goal_still_routes_to_confirmation_gate(self):
        state = make_state(
            user_phone="+2260",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            interpreted_event="CONFIRM",
            expected_input="CONFIRMATION",
            transaction_payload={"product": "mais", "price": 300, "quantity": 100},
        )
        assert self._router()(state) == "to_confirmation"


class TestBuyerRequestResolverCatalogFlow:
    def test_no_product_asks_for_it(self):
        # Texte vide : `infer_product_from_text` (repli déterministe LLM-absent)
        # retomberait sinon sur N'IMPORTE quel token non-vide du texte comme
        # "produit" — volontairement testé à vide pour isoler la vraie branche
        # "aucun produit résolu du tout".
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", transaction_payload={}, normalized_text="")
        result = run(buyer_request_resolver(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "PRODUCT"

    def test_product_and_quantity_bridges_to_cart_management(self, monkeypatch):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        calls = _patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            transaction_payload={"product": "mais", "quantity": 100},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["final_response"] == "cart_management called"
        assert calls[0]["current_goal"] == "BUYER_ADD_TO_CART"

    def test_single_vendor_asks_for_quantity(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", transaction_payload={"product": "mais"})
        runtime = rt({"search_products": {
            "status": "success",
            "results": [{"vendor_name": "Awa", "unit": "KG", "price": 250, "available_qty": 300}],
        }})
        result = run(buyer_request_resolver(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "QUANTITY"
        assert "Awa" in result["final_response"]
        assert result["current_goal"] == "BUYER_ADD_TO_CART"
        assert result["vendor_selection_context"]["chosen_vendor"]["vendor_name"] == "Awa"

    def test_single_multi_tier_vendor_bridges_to_cart_for_the_packaging_menu(self, monkeypatch):
        """Incident réel (2026-09-09) : « je veux acheter du lait » sur un
        produit multitarifaire (paquets de 5 L / 10 L) demandait « Quelle
        quantité ? » à l'aveugle. L'acheteur répondait « 11 L » sans avoir vu
        qu'on ne vend qu'en paliers, et l'interpréteur mappait ce « 11 L » en
        silence sur le palier de 10 L. Le flux doit désormais déléguer à
        `cart_management` (qui affiche le menu de conditionnements AVANT toute
        question de quantité)."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        calls = _patch_cart_management(monkeypatch)
        state = make_state(user_phone="+2260", transaction_payload={"product": "lait"})
        runtime = rt({"search_products": {
            "status": "success",
            "results": [{
                "vendor_name": "Mamadou", "unit": "LITRE", "price": 400, "available_qty": 600,
                "pricing_tiers": [
                    {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 400.0},
                    {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 700.0},
                ],
            }],
        }})
        result = run(buyer_request_resolver(state, runtime))
        assert result["final_response"] == "cart_management called"
        assert calls[0]["current_goal"] == "BUYER_ADD_TO_CART"
        # Les vendeurs déjà résolus sont transmis pour éviter un 2e
        # search_products identique dans cart_management (finding efficiency).
        assert calls[0]["_prefetched_vendors"], "vendors already resolved must be forwarded"
        assert calls[0]["_prefetched_vendors"][0]["vendor_name"] == "Mamadou"

    def test_multiple_vendors_shows_a_selection_menu(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", transaction_payload={"product": "mais"})
        # `id`/`producer_id` distincts requis : resolve_product_vendors déduplique
        # sur la clé "{id}:{producer_id}" — sans ça les deux entrées fusionnent
        # en 1 seul vendeur et le test bascule (à tort) sur la branche mono-vendeur.
        runtime = rt({"search_products": {
            "status": "success",
            "results": [
                {"id": "p1", "producer_id": "prod-1", "vendor_name": "Awa", "unit": "KG", "price": 250},
                {"id": "p2", "producer_id": "prod-2", "vendor_name": "Ali", "unit": "KG", "price": 230},
            ],
        }})
        result = run(buyer_request_resolver(state, runtime))
        assert result["current_goal"] == "BUYER_REQUEST"
        assert result["working_memory"]["buyer_request_catalog_checked"] is True

    def test_low_confidence_match_is_treated_as_not_found(self):
        """Incident réel (2026-08-14) : "oeufs" faisait correspondre "Bœuf"
        (486 000 FCFA/tête) via la recherche floue trigram, alors que ni
        œufs ni laitue n'existent en base. Suggérer "Bœuf, c'est bien ça ?"
        pour une recherche d'œufs est trompeur — le match est écarté et
        traité comme "produit introuvable" (propose un appel d'offres),
        pas comme une suggestion à confirmer. Voir
        [[buyer-search-fuzzy-match-safety-2026-08]]."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", transaction_payload={"product": "oeufs"})
        runtime = rt({
            "search_products": {
                "status": "success",
                "results": [{
                    "id": "offer1", "vendor_name": "jojo", "unit": "TETE",
                    "price": 486000, "name": "Bœuf (🌐 National)", "source_type": "FUTURE",
                }],
            },
            "record_demand_signal": {"status": "success"},
        })
        result = run(buyer_request_resolver(state, runtime))
        assert "Bœuf" not in result["final_response"]
        assert "introuvable" in result["final_response"].lower() or "Aucun produit" in result["final_response"]
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"
        assert result["working_memory"]["buyer_request_waiting_choice"] is True

    def test_a_second_product_mentioned_in_the_same_message_is_surfaced_not_lost(self):
        """L'interprète met le 1er produit dans `product` et le reste dans
        `additional_products` (jamais fusionnés en une chaîne — voir
        [[buyer-search-fuzzy-match-safety-2026-08]]). L'utilisateur doit être
        informé que "laitue" a été mis de côté, pas le voir disparaître."""
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(
            user_phone="+2260",
            transaction_payload={"product": "oeufs", "additional_products": ["laitue"]},
        )
        runtime = rt({"search_products": {
            "status": "success",
            "results": [{"vendor_name": "jojo", "unit": "KG", "price": 500, "name": "Oeufs"}],
        }})
        result = run(buyer_request_resolver(state, runtime))
        assert "laitue" in result["final_response"]

    def test_no_vendors_offers_procurement_and_logs_demand(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )
        state = make_state(user_phone="+2260", transaction_payload={"product": "produit_rare"})
        runtime = rt({
            "search_products": {"status": "success", "results": []},
            "record_demand_signal": {"status": "success"},
        })
        result = run(buyer_request_resolver(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"
        assert result["working_memory"]["buyer_request_waiting_choice"] is True
        assert "record_demand_signal" in runtime.calls

    def test_no_vendors_demand_signal_failure_is_non_blocking(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
            buyer_request_resolver,
        )

        class _BoomRuntime:
            llm = None

            async def call_db(self, tool_name, **kwargs):
                if tool_name == "record_demand_signal":
                    raise RuntimeError("demand signal boom")
                return {"status": "success", "results": []}

        state = make_state(user_phone="+2260", transaction_payload={"product": "produit_rare"})
        result = run(buyer_request_resolver(state, _BoomRuntime()))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"


# (2026-09-13, Deep Intent Architecture Cleanup) : `TestResolveReceivedBids`
# et `TestResolveBuyerBidPick` supprimées — `resolve_received_bids` et
# `resolve_buyer_bid_pick` (flows/buyer/procurement.py) ont été retirées,
# exclusivement rattachées à MARKET_GET_REQUEST_DETAIL/PROCUREMENT_SELECT_
# WINNER/PROCUREMENT_ACCEPT_OFFER, tous supprimés d'INTENT_CONFIG.


# =====================================================================
# _fmt_num
# =====================================================================

class TestFmtNum:
    def test_integer_floats_drop_trailing_zero(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import _fmt_num
        assert _fmt_num(225.0) == "225"

    def test_non_integer_floats_are_preserved(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import _fmt_num
        assert _fmt_num(225.5) == "225.5"

    def test_non_numeric_value_falls_back_to_str(self):
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import _fmt_num
        assert _fmt_num("n/a") == "n/a"
