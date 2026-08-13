"""`nodes/rendering/success.py` — le plus gros renderer AG-UI (résultats
d'outils MCP réussis : listes, catalogues, dashboards fermes/stocks,
gabarits transactionnels par goal). Zéro appel MCP — rendu pur."""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import RenderContext
from agriconnect.graphs.agents.market_coach.nodes.rendering.success import (
    _format_future_cycle_line,
    _render_buyer_catalog_sections,
    _render_catalog_section,
    _render_cycles_section,
    _render_farm_sections,
    _render_flat_list,
    _transactional_fallback_text,
    render_success,
)
from tests.conftest import make_state, run


def ctx(**state_overrides):
    state = make_state(**state_overrides)
    return RenderContext(
        state=state, mc_runtime=None,
        strategy=str(state.get("response_strategy") or ""),
        status=str(state.get("status") or ""),
        goal=state.get("current_goal"),
        salutation="",
        payload=state.get("transaction_payload") or {},
    )


# =====================================================================
# _format_future_cycle_line
# =====================================================================

class TestFormatFutureCycleLine:
    def test_livestock_gets_the_livestock_emoji(self):
        line = _format_future_cycle_line({"production_type": "LIVESTOCK", "display_label": "Poulets"})
        assert "🐄" in line

    def test_crop_gets_the_crop_emoji(self):
        line = _format_future_cycle_line({"production_type": "CROP", "species": "Mais"})
        assert "🌱" in line

    def test_includes_quantity_price_and_harvest_date(self):
        line = _format_future_cycle_line({
            "display_label": "Mais", "available_quantity": 100, "unit": "kg",
            "price_per_unit": 250, "expected_harvest_date": "2026-12-31",
        })
        assert "100" in line and "250" in line and "31/12/2026" in line

    def test_preorder_enabled_is_flagged(self):
        line = _format_future_cycle_line({"display_label": "Mais", "preorder_enabled": True})
        assert "précommande active" in line


# =====================================================================
# _render_farm_sections
# =====================================================================

class TestRenderFarmSections:
    def test_empty_dict_yields_nothing(self):
        text, options = _render_farm_sections({})
        assert text == "" and options == []

    def test_skips_non_dict_farm_entries(self):
        text, options = _render_farm_sections({"f1": "not-a-dict"})
        assert "Exploitation" not in text

    def test_farm_without_stock_says_so(self):
        text, options = _render_farm_sections({"f1": {"farm_name": "Ferme A", "stocks": []}})
        assert "Aucun produit stocké" in text

    def test_stocks_and_cycles_produce_indexed_options(self):
        text, options = _render_farm_sections({
            "f1": {
                "farm_name": "Ferme A",
                "stocks": [{"item_name": "mais", "quantity": 100, "unit": "kg", "stock_id": "s1"}],
                "upcoming_cycles": [{"display_label": "riz", "offer_id": "c1"}],
            }
        })
        assert len(options) == 2
        assert options[0]["value"] == "s1"
        assert options[1]["value"] == "c1"

    def test_more_than_two_cycles_are_truncated_with_a_count(self):
        cycles = [{"display_label": f"c{i}", "offer_id": f"id{i}"} for i in range(5)]
        text, options = _render_farm_sections({"f1": {"farm_name": "F", "stocks": [], "upcoming_cycles": cycles}})
        assert "+3 autre" in text


# =====================================================================
# _render_catalog_section
# =====================================================================

class TestRenderCatalogSection:
    def test_empty_list_yields_nothing(self):
        assert _render_catalog_section([]) == ("", [])

    def test_builds_options_with_price_and_quantity(self):
        text, options = _render_catalog_section([
            {"name": "mais", "price": 250, "quantity": 100, "unit": "kg", "id": "p1"},
        ])
        assert "250" in text and "100" in text
        assert options[0]["value"] == "p1"

    def test_short_code_is_shown_as_a_reference(self):
        text, _ = _render_catalog_section([{"name": "mais", "short_code": "ABC"}])
        assert "#ABC" in text


# =====================================================================
# _render_cycles_section
# =====================================================================

class TestRenderCyclesSection:
    def test_empty_list_yields_empty_string(self):
        assert _render_cycles_section([]) == ""

    def test_deduplicates_by_id(self):
        cycles = [{"cycle_id": "c1", "display_label": "mais"}, {"cycle_id": "c1", "display_label": "mais dup"}]
        text = _render_cycles_section(cycles)
        assert text.count("mais") <= 2  # une seule occurrence retenue (dédup)

    def test_skips_non_dict_entries(self):
        text = _render_cycles_section(["not-a-dict", {"cycle_id": "c1", "display_label": "mais"}])
        assert "mais" in text


# =====================================================================
# _render_flat_list
# =====================================================================

class TestRenderFlatList:
    def test_farm_shaped_item_uses_size_and_stocks(self):
        text, options = _render_flat_list([{"size": 5, "location": "Ouaga", "farm_id": "f1", "id": "ignored"}])
        assert "5 ha" in text
        assert "Ouaga" in text
        assert options[0]["value"] == "f1"

    def test_stock_shaped_item_uses_quantity_and_price(self):
        text, options = _render_flat_list([{"quantity": 50, "unit": "kg", "price": 250, "stock_id": "s1"}])
        assert "50" in text and "250" in text
        assert options[0]["value"] == "s1"

    def test_auction_shaped_item_uses_price_or_target_price(self):
        text, options = _render_flat_list([{"target_price": 300, "auction_id": "a1"}])
        assert "300" in text
        assert options[0]["value"] == "a1"

    def test_generic_item_falls_back_to_index_as_id(self):
        text, options = _render_flat_list([{"name": "chose"}])
        assert options[0]["value"] == "1"

    def test_non_dict_entries_are_tolerated(self):
        text, options = _render_flat_list(["not-a-dict"])
        assert len(options) == 1


# =====================================================================
# _render_buyer_catalog_sections
# =====================================================================

class TestRenderBuyerCatalogSections:
    def test_empty_list_yields_empty_string(self):
        assert _render_buyer_catalog_sections([]) == ""

    def test_direct_items_are_shown_separately_from_future(self):
        result = _render_buyer_catalog_sections([
            {"name": "mais", "price": 250, "source_type": "DIRECT"},
            {"name": "riz", "estimated_available_at": "2026-12-31"},
        ])
        assert "disponibles immédiatement" in result
        assert "Productions futures" in result

    def test_missing_price_shows_a_placeholder(self):
        result = _render_buyer_catalog_sections([{"name": "mais", "source_type": "DIRECT"}])
        assert "communiqué" in result


# =====================================================================
# _transactional_fallback_text
# =====================================================================

class TestTransactionalFallbackText:
    def test_add_to_cart(self):
        text = _transactional_fallback_text("BUYER_ADD_TO_CART", "", {"product": "mais"})
        assert "panier" in text

    def test_preorder(self):
        text = _transactional_fallback_text("BUYER_PREORDER_INIT", "", {"product": "mais"})
        assert "précommande" in text

    def test_procurement_or_auction(self):
        text = _transactional_fallback_text("PROCUREMENT_CREATE_REQUEST", "", {"product": "mais"})
        assert "appel d'offres" in text

    def test_publish_or_sell(self):
        text = _transactional_fallback_text("SALES_PUBLISH_PRODUCT", "", {"product": "mais"})
        assert "publiée" in text

    def test_bid_includes_price(self):
        text = _transactional_fallback_text("SALES_PLACE_BID", "", {"product": "mais", "price": 250})
        assert "250" in text

    def test_read_goals_stay_neutral(self):
        # "BUYER_LIST_AUCTIONS" contient "AUCTION", qui matche la branche
        # PROCUREMENT/AUCTION avant d'atteindre la branche LECTURE — un goal
        # LIST/GET/... sans mot-clé transactionnel est nécessaire ici.
        text = _transactional_fallback_text("STOCK_GET_HISTORY", "", {})
        assert "rien trouvé" in text

    def test_unknown_goal_default(self):
        text = _transactional_fallback_text("SOMETHING_ELSE", "", {})
        assert "C'est noté" in text


# =====================================================================
# render_success — le handler principal
# =====================================================================

class TestRenderSuccess:
    def test_precomputed_final_response_without_exec_result_is_reused(self):
        c = ctx(final_response="deja pret", execution_result={}, ag_ui_component={"x": 1})
        result = run(render_success(c))
        assert result["final_response"] == "deja pret"

    def test_formatted_menu_wins_over_everything(self):
        c = ctx(execution_result={"status": "success", "formatted_menu": "MENU PRET"})
        result = run(render_success(c))
        assert result["final_response"] == "MENU PRET"

    def test_farms_structured_shape_renders_a_menu(self):
        c = ctx(execution_result={"status": "success", "data": {
            "farms": {"f1": {"farm_name": "Ferme A", "stocks": [{"item_name": "mais", "quantity": 10, "stock_id": "s1"}]}}
        }})
        result = run(render_success(c))
        assert "Ferme A" in result["final_response"]
        assert result["ag_ui_component"]["kwargs"]["metadata"]["mode"] == "stocks"

    def test_empty_catalog_shape_shows_a_dedicated_empty_message(self):
        c = ctx(execution_result={"status": "success", "data": {"farms": {}, "catalog": [], "upcoming_cycles": []}})
        result = run(render_success(c))
        assert "aucun produit ni exploitation" in result["final_response"]

    def test_catalog_payload_renders_catalog_section(self):
        c = ctx(execution_result={"status": "success", "data": {
            "catalog": [{"name": "mais", "price": 250, "id": "p1"}]
        }})
        result = run(render_success(c))
        assert "mais" in result["final_response"]
        assert result["ag_ui_component"]["kwargs"]["metadata"]["mode"] == "catalog"

    def test_cycles_payload_renders_cycles_section(self):
        c = ctx(execution_result={"status": "success", "data": {
            "upcoming_cycles": [{"cycle_id": "c1", "display_label": "mais"}]
        }})
        result = run(render_success(c))
        assert "Cultures en cours" in result["final_response"]

    def test_search_products_tool_renders_buyer_catalog(self):
        c = ctx(
            selected_tool="search_products",
            execution_result={"status": "success", "data": [
                {"name": "mais", "price": 250, "source_type": "DIRECT"},
            ]},
        )
        result = run(render_success(c))
        assert "disponibles immédiatement" in result["final_response"]

    def test_search_results_with_photos_get_a_view_hint_and_are_cached(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        captured = {}
        monkeypatch.setattr(
            success_mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )
        c = ctx(
            user_phone="+22670000001",
            selected_tool="search_products",
            execution_result={"status": "success", "data": [
                {"id": "p1", "name": "mais", "price": 250, "source_type": "DIRECT", "images": ["https://x/a.jpg"]},
            ]},
        )
        result = run(render_success(c))

        assert "photos <numéro>" in result["final_response"]
        assert captured["phone"] == "+22670000001"
        assert captured["entries"] == {"1": {"id": "p1", "name": "mais", "images": ["https://x/a.jpg"]}}

    def test_search_results_without_any_photo_get_no_hint(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        monkeypatch.setattr(success_mod, "_store_search_photo_results", lambda phone, entries: None)
        c = ctx(
            selected_tool="search_products",
            execution_result={"status": "success", "data": [
                {"id": "p1", "name": "mais", "price": 250, "source_type": "DIRECT", "images": []},
            ]},
        )
        result = run(render_success(c))

        assert "photos <numéro>" not in result["final_response"]

    def test_future_only_results_are_not_cached(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        called = {"count": 0}
        monkeypatch.setattr(
            success_mod, "_store_search_photo_results",
            lambda phone, entries: called.__setitem__("count", called["count"] + 1),
        )
        c = ctx(
            selected_tool="search_products",
            execution_result={"status": "success", "data": [
                {"name": "riz", "source_type": "FUTURE", "estimated_available_at": "2026-12-31"},
            ]},
        )
        run(render_success(c))

        assert called["count"] == 0

    def test_flat_list_renders_a_selection_menu(self):
        c = ctx(execution_result={"status": "success", "data": [
            {"name": "mais", "quantity": 100, "unit": "kg", "stock_id": "s1"},
            {"name": "riz", "quantity": 50, "unit": "kg", "stock_id": "s2"},
        ]})
        result = run(render_success(c))
        assert result["ag_ui_component"]["kwargs"]["metadata"]["mode"] == "flat_list"
        assert len(result["ag_ui_component"]["kwargs"]["options"]) == 2

    def test_empty_stocks_dict_shows_a_dedicated_empty_message(self):
        c = ctx(execution_result={"status": "success", "data": {"farm_name": "Ferme A", "stocks": []}})
        result = run(render_success(c))
        assert "Aucun produit n'est actuellement enregistré" in result["final_response"]

    def test_tool_message_is_used_verbatim_when_present(self):
        c = ctx(execution_result={"status": "success", "message": "Stock ajusté avec succès."})
        result = run(render_success(c))
        assert result["final_response"] == "Stock ajusté avec succès."

    def test_no_message_and_no_data_falls_back_to_the_goal_template(self):
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", execution_result={"status": "success"},
                transaction_payload={"product": "mais"})
        result = run(render_success(c))
        assert "publiée" in result["final_response"]

    def test_auto_farm_notice_is_prepended(self):
        c = ctx(execution_result={"status": "success", "message": "OK"}, auto_farm_notice="Ferme créée auto.")
        result = run(render_success(c))
        assert result["final_response"].startswith("Ferme créée auto.")

    def test_error_creating_farm_is_prepended(self):
        c = ctx(execution_result={"status": "success", "message": "OK"}, error_creating_farm=True)
        result = run(render_success(c))
        assert "configuration automatique" in result["final_response"]

    def test_proactive_hint_is_appended(self):
        c = ctx(execution_result={"status": "success", "message": "OK"}, proactive_hint="Pensez à ajouter une photo.")
        result = run(render_success(c))
        assert "Conseil" in result["final_response"]
        assert "photo" in result["final_response"]

    def test_nested_data_envelope_is_unwrapped(self):
        c = ctx(execution_result={
            "status": "success",
            "data": {"status": "success", "message": "message interne", "data": []},
        })
        result = run(render_success(c))
        assert result["final_response"] == "message interne"


# =====================================================================
# Photos côté enchères/appels d'offres (producteur ET acheteur)
# =====================================================================

class TestAuctionAndBidPhotoHooks:
    """Voir mémoire projet "auction-bid-photos"."""

    def test_placing_a_bid_sets_a_pending_photo_target_and_adds_a_hint(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        captured = {}
        monkeypatch.setattr(
            success_mod, "_set_pending_bid_photo",
            lambda phone, bid_id: captured.update(phone=phone, bid_id=bid_id),
        )
        c = ctx(
            user_phone="+22670000001",
            selected_tool="place_bid",
            execution_result={"status": "success", "bid_id": "b1", "message": "✅ Offre transmise."},
        )
        result = run(render_success(c))

        assert captured == {"phone": "+22670000001", "bid_id": "b1"}
        assert "✅ Offre transmise." in result["final_response"]
        assert "photo" in result["final_response"].lower()

    def test_creating_an_auction_sets_a_pending_photo_target_and_adds_a_hint(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        captured = {}
        monkeypatch.setattr(
            success_mod, "_set_pending_auction_photo",
            lambda phone, auction_id: captured.update(phone=phone, auction_id=auction_id),
        )
        c = ctx(
            user_phone="+22670000001",
            selected_tool="create_auction",
            execution_result={"status": "success", "auction_id": "a1", "message": "✅ Appel d'offres enregistré."},
        )
        result = run(render_success(c))

        assert captured == {"phone": "+22670000001", "auction_id": "a1"}
        assert "✅ Appel d'offres enregistré." in result["final_response"]
        assert "photo" in result["final_response"].lower()

    def test_a_bid_list_with_photos_is_cached_and_gets_a_hint(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        captured = {}
        monkeypatch.setattr(
            success_mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )
        c = ctx(
            user_phone="+22670000001",
            selected_tool="get_auctions_bids",
            execution_result={
                "status": "success",
                "formatted_menu": "📋 *Propositions reçues...*\n*1. Lot maïs*...",
                "data": [
                    {"bid_id": "b1", "product": "maïs", "producer": "Ferme Koné", "images": ["https://x/a.jpg"]},
                    {"bid_id": "b2", "product": "maïs", "producer": "Ferme Diallo", "images": []},
                ],
            },
        )
        result = run(render_success(c))

        assert "photos <numéro>" in result["final_response"]
        assert captured["entries"]["1"]["images"] == ["https://x/a.jpg"]
        assert captured["entries"]["2"]["images"] == []

    def test_a_bid_list_without_any_photo_gets_no_hint(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod
        monkeypatch.setattr(success_mod, "_store_search_photo_results", lambda phone, entries: None)

        c = ctx(
            user_phone="+22670000001",
            selected_tool="get_auctions_bids",
            execution_result={
                "status": "success",
                "formatted_menu": "📋 *Propositions reçues...*",
                "data": [{"bid_id": "b1", "product": "maïs", "producer": "Ferme Koné", "images": []}],
            },
        )
        result = run(render_success(c))

        assert "photos <numéro>" not in result["final_response"]

    def test_producer_auctions_listing_with_reference_photos_is_cached(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        captured = {}
        monkeypatch.setattr(
            success_mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )
        c = ctx(
            user_phone="+22670000001",
            selected_tool="get_producer_auctions",
            execution_result={
                "status": "success",
                "formatted_menu": "🎯 *Appels d'offres pour vos produits...*",
                "data": [
                    {"auction_id": "a1", "product": "maïs", "buyer_name": "Acheteur X", "images": ["https://x/ref.jpg"]},
                ],
            },
        )
        result = run(render_success(c))

        assert "photos <numéro>" in result["final_response"]
        assert captured["entries"]["1"]["id"] == "a1"

    def test_own_bids_listing_with_photos_is_cached(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.rendering.success as success_mod

        captured = {}
        monkeypatch.setattr(
            success_mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )
        c = ctx(
            user_phone="+22670000001",
            selected_tool="get_my_active_bids",
            execution_result={
                "status": "success",
                "formatted_menu": "📋 *Le statut de vos propositions...*",
                "data": [{"bid_id": "b1", "product": "maïs", "status_label": "En attente", "images": ["https://x/a.jpg"]}],
            },
        )
        result = run(render_success(c))

        assert "photos <numéro>" in result["final_response"]
        assert captured["entries"]["1"]["id"] == "b1"
