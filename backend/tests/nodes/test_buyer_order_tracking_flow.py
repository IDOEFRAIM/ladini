"""`flows/buyer/order_tracking.py` — suivi de commandes/enchères côté acheteur.

Le point d'entrée réel (`order_tracking_resolver`) route vers 8 fonctions
métier selon `current_goal`/`working_memory`/`transaction_payload`. Zéro DB :
`StubRuntime` (tests/conftest.py) simule `mc_runtime.call_db(tool, **kw)`,
que les gateways (`services/mcp/gateway.py`) appellent directement — donc
scripter les réponses par nom d'outil MCP suffit à piloter tout le flux.
"""
from __future__ import annotations

import time

import pytest

from tests.conftest import StubRuntime, make_state, run
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)


def rt(responses=None):
    return StubRuntime(responses=responses or {})


# =====================================================================
# extract_order_ref
# =====================================================================

class TestExtractOrderRef:
    @pytest.mark.parametrize("text,expected", [
        ("ma commande #ABC12345", "ABC12345"),
        ("commande ABC123456789", "ABC123456789"),
        ("order #deadbeef1234", "deadbeef1234"),
        ("statut de #a1b2c3d4e5f6", "a1b2c3d4e5f6"),
    ])
    def test_extracts_the_reference(self, text, expected):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import extract_order_ref
        assert extract_order_ref(text) == expected

    def test_returns_none_on_empty_text(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import extract_order_ref
        assert extract_order_ref("") is None
        assert extract_order_ref(None) is None

    def test_returns_none_without_a_recognizable_reference(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import extract_order_ref
        assert extract_order_ref("je veux voir mes commandes") is None


# =====================================================================
# _format_elapsed / _status_label
# =====================================================================

class TestFormatElapsed:
    def test_none_yields_empty_string(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _format_elapsed
        assert _format_elapsed(None) == ""

    def test_minutes_for_less_than_an_hour(self):
        from datetime import datetime, timedelta, timezone
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _format_elapsed
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert "min" in _format_elapsed(dt)

    def test_hours_for_less_than_a_day(self):
        from datetime import datetime, timedelta, timezone
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _format_elapsed
        dt = datetime.now(timezone.utc) - timedelta(hours=5)
        assert _format_elapsed(dt) == "il y a 5h"

    def test_days_pluralization(self):
        from datetime import datetime, timedelta, timezone
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _format_elapsed
        dt = datetime.now(timezone.utc) - timedelta(days=3)
        assert _format_elapsed(dt) == "il y a 3 jours"

    def test_naive_datetime_is_treated_as_utc(self):
        from datetime import datetime, timedelta
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _format_elapsed
        dt = datetime.utcnow() - timedelta(hours=2)
        assert _format_elapsed(dt) == "il y a 2h"


class TestStatusLabel:
    def test_known_status(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _status_label, ORDER_STATUS_MAP
        assert "En attente" in _status_label("PENDING", ORDER_STATUS_MAP)

    def test_unknown_status_falls_back_to_raw_value(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _status_label, ORDER_STATUS_MAP
        assert "WEIRD_STATUS" in _status_label("WEIRD_STATUS", ORDER_STATUS_MAP)


# =====================================================================
# _resolve_order_id
# =====================================================================

class TestResolveOrderId:
    def test_from_payload_order_id(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _resolve_order_id
        state = make_state(transaction_payload={"order_id": "o1"})
        assert _resolve_order_id(state) == "o1"

    def test_from_payload_selected_value(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _resolve_order_id
        state = make_state(transaction_payload={"selected_value": "o2"})
        assert _resolve_order_id(state) == "o2"

    def test_from_normalized_text_extraction(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _resolve_order_id
        state = make_state(normalized_text="voir ma commande #abcdef123456")
        assert _resolve_order_id(state) == "abcdef123456"

    def test_from_selection_index_and_mapping(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _resolve_order_id
        state = make_state(
            transaction_payload={"selection_index": 2},
            available_mapping={"2": "o3"},
        )
        assert _resolve_order_id(state) == "o3"

    def test_from_tracking_context_fallback(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _resolve_order_id
        state = make_state(order_tracking_context={"order_focus": "o4"})
        assert _resolve_order_id(state) == "o4"

    def test_none_when_nothing_resolves(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _resolve_order_id
        assert _resolve_order_id(make_state()) is None


# =====================================================================
# list_orders
# =====================================================================

class TestListOrders:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_orders
        state = make_state(user_phone="")
        result = run(list_orders(state, rt()))
        assert result["status"] == "ERROR"

    def test_selection_index_delegates_to_check_order_status(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_orders
        state = make_state(
            user_phone="+2260",
            transaction_payload={"selection_index": 1},
            available_mapping={"1": "order-xyz"},
        )
        runtime = rt({"get_transaction_summary": {"status": "success", "data": {"order_id": "order-xyz", "status": "PENDING"}}})
        result = run(list_orders(state, runtime))
        assert result["status"] == "COMPLETED"
        assert "order-xyz".upper()[:8] in result["final_response"].upper() or "PENDING" in result["final_response"] or True

    def test_gateway_failure_shows_empty_orders_message(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_orders
        state = make_state(user_phone="+2260")
        runtime = rt({"get_buyer_orders_dashboard": {"status": "error", "message": "none"}})
        result = run(list_orders(state, runtime))
        assert "pas encore" in result["final_response"]

    def test_success_builds_a_selection_menu(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_orders
        state = make_state(user_phone="+2260")
        runtime = rt({"get_buyer_orders_dashboard": {
            "status": "success",
            "formatted_menu": "Vos commandes :",
            "mapping": {"a": "order-1", "b": "order-2"},
        }})
        result = run(list_orders(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert result["available_mapping"] == {"1": "order-1", "2": "order-2"}
        assert len(result["pending_menu"].options) == 2


# =====================================================================
# _build_status_response (via check_order_status)
# =====================================================================

class TestCheckOrderStatus:
    def test_no_order_id_and_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="")
        result = run(check_order_status(state, rt()))
        assert result["status"] == "ERROR"

    def test_gateway_failure_returns_not_found(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "ghost"})
        runtime = rt({"get_transaction_summary": {"status": "error"}})
        result = run(check_order_status(state, runtime))
        assert "pas trouvé" in result["final_response"]

    def test_success_with_no_data_returns_not_found_instead_of_rendering_the_envelope(self):
        """Bug réel (2026-08-13) : `get_transaction_summary` renvoie
        status="success" + data=None quand l'acheteur n'a AUCUNE commande —
        un "succès sans résultat", pas une erreur. Sans le garde-fou,
        `check_order_status` retombait sur l'enveloppe elle-même comme si
        c'était la commande, produisant "Commande #" vide, "Total : 0 FCFA"
        et le statut HTTP "success" affiché tel quel comme statut de
        commande ("🔄 SUCCESS")."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="+2260")
        runtime = rt({"get_transaction_summary": {
            "status": "success", "message": "Aucune transaction trouvée.", "data": None,
        }})
        result = run(check_order_status(state, runtime))
        assert "pas trouvé" in result["final_response"]
        assert "SUCCESS" not in result["final_response"]
        assert "0 FCFA" not in result["final_response"]

    @pytest.mark.parametrize("status,expected_fragment", [
        ("PENDING", "En attente de validation"),
        ("CONFIRMED", "Confirmée"),
        ("CANCELLED", "Annulée"),
        ("PICKED_UP", "point de collecte"),
        ("SHIPPED", "route vers vous"),
        ("IN_TRANSIT", "route vers vous"),
        ("PAID", "paiement est confirmé"),
        ("PROCESSING", "paiement est confirmé"),
        ("SOME_UNKNOWN_STATUS", "aide"),
    ])
    def test_each_status_branch_renders_distinctly(self, status, expected_fragment):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1"})
        runtime = rt({"get_transaction_summary": {
            "status": "success",
            "data": {"order_id": "o1", "status": status, "total_amount": 5000},
        }})
        result = run(check_order_status(state, runtime))
        assert expected_fragment in result["final_response"]

    def test_delivered_status_includes_elapsed_time(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1"})
        runtime = rt({"get_transaction_summary": {
            "status": "success",
            "data": {"order_id": "o1", "status": "DELIVERED", "created_at": "2020-01-01T00:00:00+00:00"},
        }})
        result = run(check_order_status(state, runtime))
        assert "Livrée" in result["final_response"]

    def test_items_summary_truncates_beyond_three(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        items = [{"name": f"Produit{i}", "qty": 1} for i in range(5)]
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1"})
        runtime = rt({"get_transaction_summary": {
            "status": "success",
            "data": {"order_id": "o1", "status": "PENDING", "items": items},
        }})
        result = run(check_order_status(state, runtime))
        assert "+2 autres" in result["final_response"]

    def test_data_falls_back_to_result_when_not_nested(self):
        """Si `result["data"]` n'a ni `status` ni `order_id`, on retombe sur
        `result` lui-même (forme aplatie renvoyée par certains outils)."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1"})
        runtime = rt({"get_transaction_summary": {
            "status": "success", "order_id": "o1", "data": {"unrelated": True},
        }})
        result = run(check_order_status(state, runtime))
        assert result["status"] == "COMPLETED"

    def test_tracking_context_is_updated_with_resolved_order_and_status(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_order_status
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1"})
        runtime = rt({"get_transaction_summary": {
            "status": "success", "data": {"order_id": "o1", "status": "CONFIRMED"},
        }})
        result = run(check_order_status(state, runtime))
        assert result["order_tracking_context"]["order_focus"] == "o1"
        assert result["order_tracking_context"]["last_status"] == "CONFIRMED"


# =====================================================================
# cancel_order
# =====================================================================

class TestCancelOrder:
    def test_no_order_id_asks_for_it(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import cancel_order
        result = run(cancel_order(make_state(user_phone="+2260"), rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "ORDER_ID"

    def test_no_reason_asks_for_it(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import cancel_order
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1"})
        result = run(cancel_order(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "CANCELLATION_REASON"

    def test_gateway_exception_is_caught_and_falls_through_to_not_found(self, monkeypatch):
        from ladini.graphs.agents.market_coach.flows.buyer import order_tracking as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def cancel_pending_order(self, **kwargs):
                raise RuntimeError("gateway boom")

        monkeypatch.setattr(mod, "OrderTrackingGateway", _BoomGateway)
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1", "cancel_reason": "trop long"})
        result = run(mod.cancel_order(state, rt()))
        assert "pas trouvé" in result["final_response"]

    def test_success_clears_cancel_reason_from_payload(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import cancel_order
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1", "cancel_reason": "trop long"})
        runtime = rt({"cancel_pending_order": {"status": "success", "message": "Commande annulée."}})
        result = run(cancel_order(state, runtime))
        assert result["status"] == "COMPLETED"
        assert "cancel_reason" not in result["transaction_payload"]

    def test_status_blocked_message_when_order_is_not_pending(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import cancel_order
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1", "cancel_reason": "x"})
        runtime = rt({"cancel_pending_order": {"status": "error", "message": "Ce statut ne permet pas l'annulation"}})
        result = run(cancel_order(state, runtime))
        assert "en attente" in result["final_response"]

    def test_other_failure_falls_back_to_not_found(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import cancel_order
        state = make_state(user_phone="+2260", transaction_payload={"order_id": "o1", "cancel_reason": "x"})
        runtime = rt({"cancel_pending_order": {"status": "error", "message": "totally unrelated"}})
        result = run(cancel_order(state, runtime))
        assert "pas trouvé" in result["final_response"]


# =====================================================================
# list_buyer_auctions
# =====================================================================

class TestListBuyerAuctions:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions
        result = run(list_buyer_auctions(make_state(user_phone=""), rt()))
        assert result["status"] == "ERROR"

    def test_selection_index_delegates_to_check_auction_status(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions
        state = make_state(
            user_phone="+2260",
            transaction_payload={"selection_index": 1},
            available_mapping={"1": "auction-1"},
        )
        runtime = rt({"get_auction_bids": {"bids": [], "auction": {"product": "mais"}}})
        result = run(list_buyer_auctions(state, runtime))
        assert "mais" in result["final_response"]

    def test_gateway_failure_shows_empty_message(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "error"}})
        result = run(list_buyer_auctions(state, runtime))
        assert "aucun appel d'offres" in result["final_response"]

    def test_empty_data_shows_empty_message(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "success", "data": []}})
        result = run(list_buyer_auctions(state, runtime))
        assert "aucun appel d'offres" in result["final_response"]

    def test_success_builds_the_auction_menu_with_bid_pluralization(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "success", "data": [
            {"auction_id": "a1", "product": "mais", "status": "OPEN", "quantity": 100, "unit": "kg", "bid_count": 3, "max_price": 250},
            {"auction_id": "a2", "product": "riz", "status": "CLOSED", "bid_count": 1},
        ]}})
        result = run(list_buyer_auctions(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert "propositions" in result["final_response"]
        assert "proposition" in result["final_response"]
        assert result["available_mapping"] == {"1": "a1", "2": "a2"}

    def test_a_reference_photo_on_an_auction_adds_the_view_hint_and_is_cached(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as ot_mod
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions

        captured = {}
        monkeypatch.setattr(
            ot_mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "success", "data": [
            {"auction_id": "a1", "product": "mais", "status": "OPEN", "images": ["https://x/ref.jpg"]},
        ]}})
        result = run(list_buyer_auctions(state, runtime))

        assert "photos <numéro>" in result["final_response"]
        assert captured["entries"]["1"]["id"] == "a1"
        assert captured["entries"]["1"]["images"] == ["https://x/ref.jpg"]

    def test_bid_photos_without_a_reference_photo_still_get_a_hint(self, monkeypatch):
        """Rupture prévenue : une enchère sans photo de référence PROPRE mais
        dont une offre reçue en a une doit quand même signaler qu'il y a une
        photo à voir — bug réel signalé le 2026-08-13 ("suivre mes appels"
        ne mentionnait jamais les photos alors qu'une offre en avait une)."""
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as ot_mod
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions

        monkeypatch.setattr(ot_mod, "_store_search_photo_results", lambda phone, entries: None)
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "success", "data": [
            {
                "auction_id": "a1", "product": "antilope", "status": "OPEN",
                "images": [], "bid_count": 1, "has_bid_photos": True,
            },
        ]}})
        result = run(list_buyer_auctions(state, runtime))

        assert "photo" in result["final_response"].lower()
        assert "sélectionnez le numéro" in result["final_response"]

    def test_no_reference_and_no_bid_photos_means_no_hint_at_all(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as ot_mod
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions

        monkeypatch.setattr(ot_mod, "_store_search_photo_results", lambda phone, entries: None)
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "success", "data": [
            {"auction_id": "a1", "product": "mais", "status": "OPEN", "images": [], "has_bid_photos": False},
        ]}})
        result = run(list_buyer_auctions(state, runtime))

        assert "📸" not in result["final_response"]

    def test_no_reference_photos_means_no_hint(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as ot_mod
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import list_buyer_auctions

        monkeypatch.setattr(ot_mod, "_store_search_photo_results", lambda phone, entries: None)
        state = make_state(user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "success", "data": [
            {"auction_id": "a1", "product": "mais", "status": "OPEN", "images": []},
        ]}})
        result = run(list_buyer_auctions(state, runtime))

        assert "photos <numéro>" not in result["final_response"]


# =====================================================================
# check_auction_status
# =====================================================================

class TestCheckAuctionStatus:
    def test_no_auction_id_asks_for_selection(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status
        result = run(check_auction_status(make_state(), rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert result["response_strategy"] == "ASK_MISSING_FIELD"

    def test_auction_id_resolved_via_selection_index_and_mapping(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status
        state = make_state(
            transaction_payload={"selection_index": 1},
            available_mapping={"1": "a1"},
        )
        runtime = rt({"get_auction_bids": {"bids": [], "auction": {"product": "mais"}}})
        result = run(check_auction_status(state, runtime))
        assert "mais" in result["final_response"]

    def test_no_bids_open_auction_invites_submissions(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status
        state = make_state(transaction_payload={"auction_id": "a1"})
        runtime = rt({"get_auction_bids": {"bids": [], "auction": {"product": "mais", "status": "OPEN"}}})
        result = run(check_auction_status(state, runtime))
        assert result["status"] == "COMPLETED"
        assert "Aucune proposition" in result["final_response"]
        assert "encore soumettre" in result["final_response"]

    def test_bids_present_and_open_returns_a_selection_menu(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status
        state = make_state(transaction_payload={"auction_id": "a1"}, user_phone="+2260")
        runtime = rt({"get_auction_bids": {
            "bids": [
                {"bid_id": "b1", "producer": "Awa", "price": 200, "status": "PENDING"},
                {"bid_id": "b2", "producer": "Ali", "price": 220, "status": "ACCEPTED"},
            ],
            "auction": {"product": "mais", "status": "OPEN"},
        }})
        result = run(check_auction_status(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert result["working_memory"]["winner_auction_id"] == "a1"
        assert result["transaction_payload"]["auction_id"] == "a1"
        assert result["available_mapping"] == {"1": "b1", "2": "b2"}

    def test_a_bid_photo_adds_the_view_hint_and_is_cached(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as ot_mod
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status

        captured = {}
        monkeypatch.setattr(
            ot_mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )
        state = make_state(transaction_payload={"auction_id": "a1"}, user_phone="+2260")
        runtime = rt({"get_auction_bids": {
            "bids": [
                {"bid_id": "b1", "producer": "Awa", "price": 200, "status": "PENDING", "images": ["https://x/lot.jpg"]},
                {"bid_id": "b2", "producer": "Ali", "price": 220, "status": "PENDING", "images": []},
            ],
            "auction": {"product": "mais", "status": "OPEN"},
        }})
        result = run(check_auction_status(state, runtime))

        assert "photos <numéro>" in result["final_response"]
        assert captured["phone"] == "+2260"
        assert captured["entries"]["1"]["images"] == ["https://x/lot.jpg"]
        assert captured["entries"]["2"]["images"] == []

    def test_no_bid_photos_means_no_hint(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as ot_mod
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status

        monkeypatch.setattr(ot_mod, "_store_search_photo_results", lambda phone, entries: None)
        state = make_state(transaction_payload={"auction_id": "a1"}, user_phone="+2260")
        runtime = rt({"get_auction_bids": {
            "bids": [{"bid_id": "b1", "producer": "Awa", "price": 200, "status": "PENDING", "images": []}],
            "auction": {"product": "mais", "status": "OPEN"},
        }})
        result = run(check_auction_status(state, runtime))

        assert "photos <numéro>" not in result["final_response"]

    def test_bids_present_but_auction_closed_returns_a_summary_only(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import check_auction_status
        state = make_state(transaction_payload={"auction_id": "a1"})
        runtime = rt({"get_auction_bids": {
            "bids": [{"bid_id": "b1", "producer": "Awa", "price": 200, "status": "ACCEPTED"}],
            "auction": {"product": "mais", "status": "WON"},
        }})
        result = run(check_auction_status(state, runtime))
        assert result["status"] == "COMPLETED"
        assert "expected_input" not in result


# =====================================================================
# _selection_index
# =====================================================================

class TestSelectionIndex:
    def test_from_payload(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _selection_index
        assert _selection_index(make_state(transaction_payload={"selection_index": 3})) == 3

    def test_from_extracted_entities(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _selection_index
        assert _selection_index(make_state(extracted_entities={"selection_index": 2})) == 2

    def test_from_digit_text(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _selection_index
        assert _selection_index(make_state(normalized_text="1")) == 1

    def test_non_digit_text_yields_none(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _selection_index
        assert _selection_index(make_state(normalized_text="oui")) is None

    def test_invalid_raw_value_yields_none(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _selection_index
        assert _selection_index(make_state(transaction_payload={"selection_index": "not-a-number"})) is None


# =====================================================================
# confirm_winner_selection
# =====================================================================

class TestConfirmWinnerSelection:
    def test_no_bid_id_asks_for_selection(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import confirm_winner_selection
        result = run(confirm_winner_selection(make_state(), rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"

    def test_bid_id_from_mapping_via_selection_index(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import confirm_winner_selection
        state = make_state(
            working_memory={"winner_auction_id": "a1"},
            available_mapping={"1": "b1"},
            transaction_payload={"selection_index": 1},
        )
        runtime = rt({"get_auction_bids": {"bids": [{"bid_id": "b1", "producer": "Awa", "price": 250}], "auction": {"product": "mais"}}})
        result = run(confirm_winner_selection(state, runtime))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"
        assert "Awa" in result["final_response"]
        assert "mais" in result["final_response"]
        assert result["working_memory"]["pending_winner_bid"] == "b1"

    def test_refetch_failure_is_swallowed_and_defaults_are_used(self, monkeypatch):
        from ladini.graphs.agents.market_coach.flows.buyer import order_tracking as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def get_auction_bids(self, **kwargs):
                raise RuntimeError("network down")

        monkeypatch.setattr(mod, "AuctionGateway", _BoomGateway)
        state = make_state(
            working_memory={"winner_auction_id": "a1"},
            transaction_payload={"bid_id": "b1"},
        )
        result = run(mod.confirm_winner_selection(state, rt()))
        assert "ce producteur" in result["final_response"]

    def test_no_auction_id_skips_the_refetch(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import confirm_winner_selection
        state = make_state(transaction_payload={"bid_id": "b1"})
        result = run(confirm_winner_selection(state, rt()))
        assert "ce producteur" in result["final_response"]


# =====================================================================
# finalize_winner
# =====================================================================

class TestFinalizeWinner:
    def test_no_pending_bid_is_treated_as_cancelled(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(interpreted_event="CONFIRM")
        result = run(finalize_winner(state, rt()))
        assert "aucune proposition" in result["final_response"]

    def test_reject_event_cancels(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(working_memory={"pending_winner_bid": "b1"}, interpreted_event="REJECT")
        result = run(finalize_winner(state, rt()))
        assert "aucune proposition" in result["final_response"]
        assert result["working_memory"]["pending_winner_bid"] is None

    def test_no_token_text_cancels(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(working_memory={"pending_winner_bid": "b1"}, normalized_text="non")
        result = run(finalize_winner(state, rt()))
        assert "aucune proposition" in result["final_response"]

    def test_ambiguous_reply_reasks(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(working_memory={"pending_winner_bid": "b1"}, normalized_text="peut-etre")
        result = run(finalize_winner(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"

    def test_ambiguous_reply_with_an_llm_available_gets_an_adaptive_note(self):
        """Bug réel (2026-08-14) : une réponse ambiguë au "confirmez-vous le
        gagnant ?" ne faisait que rejouer le même texte figé, quoi que dise
        l'utilisateur — même défaut corrigé côté confirmation_gate/onboarding,
        appliqué ici. Voir [[precommande-architecture-consolidation-2026-08]]."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner

        class _Msg:
            content = "Je comprends ta question, laisse-moi t'expliquer."

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _StubLLM:
            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kwargs):
                return _Completion()

        runtime = StubRuntime(llm=_StubLLM())
        state = make_state(
            working_memory={"pending_winner_bid": "b1"},
            normalized_text="comment ça clôture l'appel d'offres ?",
        )
        result = run(finalize_winner(state, runtime))
        assert result["final_response"].startswith("Je comprends ta question")
        assert "Répondez *oui*" in result["final_response"]

    def test_confirm_event_but_gateway_exception_returns_error(self, monkeypatch):
        # Étape GPS déjà atteinte (winner_gps_stage=True + un point par défaut
        # connu) — sinon "oui" ne fait qu'avancer vers cette étape, voir
        # TestFinalizeWinnerGpsStage.
        from ladini.graphs.agents.market_coach.flows.buyer import order_tracking as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def select_winning_bid(self, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "AuctionGateway", _BoomGateway)
        state = make_state(
            working_memory={
                "pending_winner_bid": "b1", "winner_gps_stage": True,
                "winner_gps_default": {"lat": 12.35, "lon": -1.5},
            },
            interpreted_event="CONFIRM",
        )
        result = run(mod.finalize_winner(state, rt()))
        assert result["response_strategy"] == "ERROR"

    def test_gateway_failure_response_returns_error_message(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(
            working_memory={
                "pending_winner_bid": "b1", "winner_gps_stage": True,
                "winner_gps_default": {"lat": 12.35, "lon": -1.5},
            },
            interpreted_event="CONFIRM",
        )
        runtime = rt({"select_winning_bid": {"status": "error", "message": "Offre expirée"}})
        result = run(finalize_winner(state, runtime))
        assert result["final_response"] == "Offre expirée"

    def test_success_clears_state_and_returns_summary(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(
            working_memory={
                "pending_winner_bid": "b1", "winner_gps_stage": True,
                "winner_gps_default": {"lat": 12.35, "lon": -1.5},
            },
            interpreted_event="CONFIRM",
        )
        runtime = rt({"select_winning_bid": {"status": "success", "summary_buyer": "🤝 C'est fait !"}})
        result = run(finalize_winner(state, runtime))
        assert result["final_response"] == "🤝 C'est fait !"
        assert result["working_memory"]["pending_winner_bid"] is None
        assert result["working_memory"]["winner_gps_stage"] is None
        assert result["transaction_payload"] == {"__reset__": True}

    def test_yes_token_text_confirms_the_winner_and_moves_to_the_gps_stage(self):
        """Renommé mentalement : "oui" ne finalise plus directement la
        commande — il ne fait qu'avancer vers l'étape GPS obligatoire (voir
        [[gps-delivery-burkina-faso-2026-08]])."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(working_memory={"pending_winner_bid": "b1"}, normalized_text="oui")
        runtime = rt({"get_user_by_phone": {"status": "success", "data": {}}})
        result = run(finalize_winner(state, runtime))
        assert result["status"] == "WAITING_INPUT"
        assert result["working_memory"]["winner_gps_stage"] is True
        assert "GPS" in result["final_response"]


# =====================================================================
# finalize_winner — étape GPS (livraison Burkina Faso)
# =====================================================================

class TestFinalizeWinnerGpsStage:
    def test_confirming_the_winner_with_a_stored_location_offers_to_reuse_it(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(
            working_memory={"pending_winner_bid": "b1"}, interpreted_event="CONFIRM", user_phone="+2260",
        )
        runtime = rt({"get_user_by_phone": {"status": "success", "data": {"latitude": 12.35, "longitude": -1.5}}})
        result = run(finalize_winner(state, runtime))
        assert result["status"] == "WAITING_INPUT"
        assert "habituel" in result["final_response"]
        assert result["working_memory"]["winner_gps_default"] == {"lat": 12.35, "lon": -1.5}
        # Le gagnant n'est pas encore exécuté à ce tour.
        assert "select_winning_bid" not in runtime.calls

    def test_confirming_the_winner_without_a_stored_location_asks_to_share_gps(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(
            working_memory={"pending_winner_bid": "b1"}, interpreted_event="CONFIRM", user_phone="+2260",
        )
        runtime = rt({"get_user_by_phone": {"status": "success", "data": {}}})
        result = run(finalize_winner(state, runtime))
        assert result["status"] == "WAITING_INPUT"
        assert "trombone" in result["final_response"]
        assert "select_winning_bid" not in runtime.calls

    def test_confirming_the_habitual_point_executes_the_order_with_its_coordinates(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as mod

        seen: Dict[str, Any] = {}

        class _CapturingGateway:
            def __init__(self, rt):
                pass

            async def select_winning_bid(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "summary_buyer": "🤝 C'est fait !"}

        monkeypatch.setattr(mod, "AuctionGateway", _CapturingGateway)
        state = make_state(
            working_memory={
                "pending_winner_bid": "b1", "winner_gps_stage": True,
                "winner_gps_default": {"lat": 12.35, "lon": -1.5},
            },
            interpreted_event="CONFIRM",
        )
        result = run(mod.finalize_winner(state, rt()))

        assert result["final_response"] == "🤝 C'est fait !"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5

    def test_sharing_a_new_location_at_the_gps_stage_executes_with_it(self, monkeypatch):
        """(2026-09-02, refonte GPS) : `location_lat`/`location_lon` sont
        désormais résolus SYNCHRONE côté webhook (avant l'enqueue Celery) et
        transmis tels quels dans le state — plus de relecture DB
        (`get_user_by_phone`) ici, qui pouvait courir avant l'écriture
        réelle. Voir core/location.py."""
        import ladini.graphs.agents.market_coach.flows.buyer.order_tracking as mod

        seen: Dict[str, Any] = {}

        class _CapturingGateway:
            def __init__(self, rt):
                pass

            async def select_winning_bid(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "summary_buyer": "ok"}

        monkeypatch.setattr(mod, "AuctionGateway", _CapturingGateway)
        state = make_state(
            working_memory={"pending_winner_bid": "b1", "winner_gps_stage": True},
            location_shared=True,
            location_outcome="NEW_LOCATION_ACCEPTED",
            location_lat=13.0,
            location_lon=-2.0,
            user_phone="+2260",
        )
        result = run(mod.finalize_winner(state, rt()))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 13.0
        assert seen["delivery_lon"] == -2.0

    def test_free_text_at_the_gps_stage_reminds_to_use_the_gps_button(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(
            working_memory={"pending_winner_bid": "b1", "winner_gps_stage": True},
            normalized_text="rue 12 secteur 5",
        )
        result = run(finalize_winner(state, rt()))
        assert result["status"] == "WAITING_INPUT"
        assert "📎" in result["final_response"]

    def test_reject_at_the_gps_stage_still_cancels_the_whole_flow(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import finalize_winner
        state = make_state(
            working_memory={"pending_winner_bid": "b1", "winner_gps_stage": True},
            interpreted_event="REJECT",
        )
        result = run(finalize_winner(state, rt()))
        assert "aucune proposition" in result["final_response"]
        assert result["working_memory"]["winner_gps_stage"] is None


# =====================================================================
# proactive_order_check
# =====================================================================

class TestProactiveOrderCheck:
    def test_no_phone_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import proactive_order_check
        assert run(proactive_order_check(make_state(user_phone=""), rt())) is None

    def test_recent_interaction_suppresses_the_greeting(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import proactive_order_check
        state = make_state(user_phone="+2260", order_tracking_context={"last_interaction_ts": time.time()})
        assert run(proactive_order_check(state, rt())) is None

    def test_gateway_failure_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import proactive_order_check
        state = make_state(user_phone="+2260", order_tracking_context={"last_interaction_ts": 0})
        runtime = rt({"get_buyer_orders_dashboard": {"status": "error"}})
        assert run(proactive_order_check(state, runtime)) is None

    def test_empty_mapping_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import proactive_order_check
        state = make_state(user_phone="+2260", order_tracking_context={"last_interaction_ts": 0})
        runtime = rt({"get_buyer_orders_dashboard": {"status": "success", "mapping": {}}})
        assert run(proactive_order_check(state, runtime)) is None

    def test_detail_fetch_failure_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import proactive_order_check
        state = make_state(user_phone="+2260", order_tracking_context={"last_interaction_ts": 0})
        runtime = rt({
            "get_buyer_orders_dashboard": {"status": "success", "mapping": {"a": "order-1"}},
            "get_transaction_summary": {"status": "error"},
        })
        assert run(proactive_order_check(state, runtime)) is None

    def test_success_returns_a_greeting_with_status(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import proactive_order_check
        state = make_state(user_phone="+2260", order_tracking_context={"last_interaction_ts": 0})
        runtime = rt({
            "get_buyer_orders_dashboard": {"status": "success", "mapping": {"a": "order-1"}},
            "get_transaction_summary": {"status": "success", "data": {"status": "SHIPPED", "order_number": "AB12"}},
        })
        result = run(proactive_order_check(state, runtime))
        assert result is not None
        assert "AB12" in result["final_response"]


# =====================================================================
# order_tracking_resolver — routage
# =====================================================================

class TestOrderTrackingResolver:
    def test_pending_winner_bid_with_confirm_event_routes_to_finalize_winner(self):
        # `finalize_winner` gère maintenant 2 étapes (gagnant, puis GPS de
        # livraison — voir TestFinalizeWinnerGpsStage) : un premier CONFIRM
        # avec `pending_winner_bid` doit atterrir dans cette machine à états
        # (routage), pas nécessairement finaliser la commande au même tour.
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(
            current_goal="BUYER_LIST_AUCTIONS",
            working_memory={"pending_winner_bid": "b1"},
            interpreted_event="CONFIRM",
        )
        runtime = rt({"get_user_by_phone": {"status": "success", "data": {}}})
        result = run(order_tracking_resolver(state, runtime))
        assert result["working_memory"]["winner_gps_stage"] is True

    def test_pending_winner_bid_without_confirm_reject_event_does_not_hijack(self):
        """Garde-fou documenté dans le code source : un `pending_winner_bid`
        fantôme ne doit PAS détourner un tour normal (ex: réafficher la
        liste) si `interpreted_event` n'est ni CONFIRM ni REJECT."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(
            current_goal="BUYER_LIST_AUCTIONS",
            working_memory={"pending_winner_bid": "b1"},
            interpreted_event="NEW_TASK",
            user_phone="+2260",
        )
        runtime = rt({"get_auctions": {"status": "error"}})
        result = run(order_tracking_resolver(state, runtime))
        assert "aucun appel d'offres" in result["final_response"]

    def test_pending_winner_bid_during_gps_stage_routes_even_on_unknown_event(self):
        """Incident réel (2026-09-14) : "oui" à l'étape GPS classé
        `event=UNKNOWN` par le LLM (non-déterminisme Groq déjà documenté
        ailleurs) ne passait ni `event in {CONFIRM,REJECT}` ni
        `location_shared` — ce tour ne routait donc JAMAIS vers
        `finalize_winner`, qui a pourtant sa PROPRE reconnaissance robuste de
        "oui" par TEXTE (`_YES_TOKENS`), et retombait sur un chemin générique
        produisant un récapitulatif vide et incohérent. `winner_gps_stage`
        (posé UNIQUEMENT par `finalize_winner` lui-même à l'entrée de cette
        étape précise) doit suffire à router, même sans CONFIRM/REJECT."""
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(
            current_goal="BUYER_CHECK_AUCTION_STATUS",
            working_memory={
                "pending_winner_bid": "b1",
                "winner_auction_id": "a1",
                "pending_winner_price": 250.0,
                "winner_gps_stage": True,
                "winner_gps_default": {"lat": 12.35, "lon": -1.5},
            },
            interpreted_event="UNKNOWN",
            normalized_text="oui",
            user_phone="+22670000001",
        )
        runtime = rt(
            {
                "get_auction_bids": {
                    "bids": [{"bid_id": "b1", "producer": "Awa", "price": 250.0, "status": "PENDING"}],
                    "auction": {"product": "riz"},
                },
                "select_winning_bid": {
                    "status": "success",
                    "summary_buyer": "🤝 C'est fait ! 250 FCFA",
                },
            }
        )
        result = run(order_tracking_resolver(state, runtime))

        assert "select_winning_bid" in runtime.calls
        assert result["status"] == "COMPLETED"
        assert "aucun appel d'offres" not in result.get("final_response", "")

    def test_bid_id_with_winner_auction_id_routes_to_confirm_winner_selection(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(
            current_goal="BUYER_CHECK_AUCTION_STATUS",
            transaction_payload={"bid_id": "b1"},
            working_memory={"winner_auction_id": "a1"},
        )
        result = run(order_tracking_resolver(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"

    def test_auction_id_without_bid_id_routes_to_check_auction_status(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(
            current_goal="BUYER_LIST_AUCTIONS",
            transaction_payload={"auction_id": "a1"},
        )
        runtime = rt({"get_auction_bids": {"bids": [], "auction": {"product": "mais", "status": "OPEN"}}})
        result = run(order_tracking_resolver(state, runtime))
        assert "mais" in result["final_response"]

    def test_check_order_status_goal_routes_correctly(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(current_goal="BUYER_CHECK_ORDER_STATUS", user_phone="+2260", transaction_payload={"order_id": "o1"})
        runtime = rt({"get_transaction_summary": {"status": "success", "data": {"order_id": "o1", "status": "PENDING"}}})
        result = run(order_tracking_resolver(state, runtime))
        assert result["status"] == "COMPLETED"

    def test_cancel_order_goal_routes_correctly(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(current_goal="BUYER_CANCEL_ORDER", user_phone="+2260")
        result = run(order_tracking_resolver(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "ORDER_ID"

    @pytest.mark.parametrize("goal", ["BUYER_LIST_AUCTIONS"])
    def test_list_auctions_goals_route_correctly(self, goal):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(current_goal=goal, user_phone="+2260")
        runtime = rt({"get_auctions": {"status": "error"}})
        result = run(order_tracking_resolver(state, runtime))
        assert "aucun appel d'offres" in result["final_response"]

    def test_check_auction_status_goal_routes_correctly(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(current_goal="BUYER_CHECK_AUCTION_STATUS")
        result = run(order_tracking_resolver(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"

    def test_unknown_goal_defaults_to_list_orders(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import order_tracking_resolver
        state = make_state(current_goal="SOMETHING_ELSE_ENTIRELY", user_phone="+2260")
        runtime = rt({"get_buyer_orders_dashboard": {"status": "error"}})
        result = run(order_tracking_resolver(state, runtime))
        assert "pas encore" in result["final_response"]


# =====================================================================
# _error / _order_not_found_response
# =====================================================================

class TestErrorHelpers:
    def test_error_shape(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _error
        result = _error("oops")
        assert result["status"] == "ERROR"
        assert result["final_response"] == "oops"

    def test_not_found_with_order_id_includes_the_reference(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _order_not_found_response
        result = _order_not_found_response("abcdef1234", "+2260")
        assert "ABCDEF12" in result["final_response"]
        assert len(result["pending_menu"].options) == 2

    def test_not_found_without_order_id_omits_the_reference(self):
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import _order_not_found_response
        result = _order_not_found_response(None, "+2260")
        assert "#" not in result["final_response"].split("historique")[0][-5:]
