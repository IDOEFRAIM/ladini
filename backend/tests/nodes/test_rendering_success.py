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
    _render_market_snapshot,
    _render_price_check,
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

    def test_a_large_stock_is_paginated_instead_of_producing_one_giant_block(self):
        """Incident 2026-08-27 : une exploitation avec beaucoup de produits
        produisait un texte non borné, tronqué à l'aveugle (et perdu au-delà
        de 4 messages) par `api/tasks.py::_chunk_whatsapp_body`. Le rendu
        doit désormais poser des marqueurs PAGE_BREAK entre pages d'au plus
        5 éléments, jamais au milieu d'un élément."""
        from agriconnect.graphs.agents.market_coach.nodes.rendering.success import (
            PAGE_BREAK,
            _STOCK_LIST_MAX_ITEMS_PER_PAGE,
        )

        stocks = [
            {"item_name": f"produit{i}", "quantity": 10, "unit": "kg", "stock_id": f"s{i}"}
            for i in range(12)
        ]
        text, options = _render_farm_sections({"f1": {"farm_name": "Ferme A", "stocks": stocks}})

        assert len(options) == 12
        pages = text.split(PAGE_BREAK)
        assert len(pages) > 1
        # Chaque page (hors la dernière, qui porte aussi le footer) contient
        # au plus le nombre max d'éléments configuré.
        for page in pages:
            assert page.count("️⃣") <= _STOCK_LIST_MAX_ITEMS_PER_PAGE

    def test_pagination_never_cuts_an_item_in_half(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.success import PAGE_BREAK

        stocks = [
            {"item_name": f"produit{i}", "quantity": 10, "unit": "kg", "stock_id": f"s{i}"}
            for i in range(8)
        ]
        text, _options = _render_farm_sections({"f1": {"farm_name": "Ferme A", "stocks": stocks}})

        for i in range(8):
            # Chaque libellé d'item apparaît une seule fois, entier, jamais
            # scindé par un PAGE_BREAK au milieu.
            assert f"produit{i} : 10 KG" in text.replace(PAGE_BREAK, "\n")

    def test_a_small_stock_has_no_page_markers(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.success import PAGE_BREAK

        text, _options = _render_farm_sections(
            {"f1": {"farm_name": "Ferme A", "stocks": [{"item_name": "mais", "quantity": 10, "unit": "kg", "stock_id": "s1"}]}}
        )
        assert PAGE_BREAK not in text


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
    def test_empty_list_yields_empty_string_and_no_options(self):
        assert _render_cycles_section([]) == ("", [])

    def test_deduplicates_by_id(self):
        cycles = [{"cycle_id": "c1", "display_label": "mais"}, {"cycle_id": "c1", "display_label": "mais dup"}]
        text, options = _render_cycles_section(cycles)
        assert text.count("mais") <= 2  # une seule occurrence retenue (dédup)
        assert len(options) == 1

    def test_skips_non_dict_entries(self):
        text, _options = _render_cycles_section(["not-a-dict", {"cycle_id": "c1", "display_label": "mais"}])
        assert "mais" in text

    def test_builds_one_selectable_option_per_cycle(self):
        """Audit UX interactive 2026-08-27 : c'était le seul rendu structuré
        de ce fichier à ne jamais produire d'options — condamnait les
        précommandes/productions futures à du texte brut sur WhatsApp."""
        cycles = [
            {"cycle_id": "c1", "display_label": "Poussins", "farm_name": "Ferme de Jojo"},
            {"cycle_id": "c2", "display_label": "Maïs", "farm_name": "Ferme de Jojo"},
        ]
        text, options = _render_cycles_section(cycles)
        assert len(options) == 2
        assert options[0] == {"index": "1", "label": "Poussins (Ferme de Jojo)", "value": "c1"}
        assert options[1]["value"] == "c2"

    def test_text_lines_use_numbered_emoji_index_not_a_bare_bullet(self):
        """Corrigé sur demande explicite (2026-08-27) : cette section
        utilisait une puce "•" simple, seule exception aux index numérotés
        déjà utilisés partout ailleurs dans ce fichier (farms, catalog,
        flat_list) — incohérent avec les options sélectionnables juste en
        dessous, qui elles étaient déjà numérotées 1, 2, 3..."""
        cycles = [
            {"cycle_id": "c1", "display_label": "Poussins", "farm_name": "Ferme de Jojo"},
            {"cycle_id": "c2", "display_label": "Maïs", "farm_name": "Ferme de Jojo"},
        ]
        text, _options = _render_cycles_section(cycles)
        assert "•" not in text
        assert "1️⃣" in text
        assert "2️⃣" in text


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
    def test_empty_list_yields_empty_string_and_no_options(self):
        assert _render_buyer_catalog_sections([]) == ("", [])

    def test_direct_items_are_shown_separately_from_future(self):
        text, _options = _render_buyer_catalog_sections([
            {"name": "mais", "price": 250, "source_type": "DIRECT"},
            {"name": "riz", "estimated_available_at": "2026-12-31"},
        ])
        assert "disponibles immédiatement" in text
        assert "Productions futures" in text

    def test_missing_price_shows_a_placeholder(self):
        text, _options = _render_buyer_catalog_sections([{"name": "mais", "source_type": "DIRECT"}])
        assert "communiqué" in text

    def test_only_direct_items_become_selectable_options(self):
        """Audit UX interactive 2026-08-27 : les items DIRECT (déjà numérotés
        dans le texte) deviennent sélectionnables ; les productions futures
        restent du texte pur — pas de régression sur leur affichage, qui
        n'était de toute façon jamais numéroté."""
        text, options = _render_buyer_catalog_sections([
            {"name": "mais", "price": 250, "source_type": "DIRECT", "id": "p1"},
            {"name": "riz", "estimated_available_at": "2026-12-31"},
        ])
        assert len(options) == 1
        assert options[0]["value"] == "p1"


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

    def test_a_stale_goal_from_an_unrelated_tool_is_not_honored(self):
        """Bug réel (2026-08-15) : un prix vérifié via VALIDATE_PRICE
        (`check_price_anomaly`) a été annoncé "Votre appel d'offres... a été
        enregistré avec succès" — `goal` (résolu par `resolve_goal_for_ui`,
        tolérant, "jamais UNKNOWN") portait un goal PÉRIMÉ d'une tentative
        d'appel d'offres précédente abandonnée dans la même conversation.
        `selected_tool` (toujours frais, posé par `mcp_tool_executor` CE
        tour) doit invalider le gabarit "appel d'offres" quand l'outil qui a
        réellement tourné n'est PAS `create_auction`. Voir
        [[precommande-architecture-consolidation-2026-08]]."""
        text = _transactional_fallback_text(
            "PROCUREMENT_CREATE_REQUEST", "", {"product": "riz"},
            selected_tool="check_price_anomaly",
        )
        assert "enregistré avec succès" not in text
        assert "C'est noté" in text

    def test_a_matching_tool_still_honors_the_procurement_template(self):
        text = _transactional_fallback_text(
            "PROCUREMENT_CREATE_REQUEST", "", {"product": "riz"},
            selected_tool="create_auction",
        )
        assert "appel d'offres" in text

    def test_a_stale_read_goal_does_not_shadow_a_successful_write(self):
        """Incident réel (2026-08-27) : `add_stock` a RÉUSSI (575 poulets
        enregistrés) mais le message final annonçait "Je n'ai rien trouvé à
        afficher" — la branche LECTURE (LIST/GET/CHECK/SEARCH/VIEW/
        DASHBOARD/SNAPSHOT) était la SEULE de cette fonction sans garde-fou
        `selected_tool`, contrairement à toutes les autres. Un `goal` PÉRIMÉ
        contenant "GET" (ex: reliquat de `get_farms` appelé plus tôt par
        `context_resolver` dans la même conversation) suffisait à déclencher
        le texte "rien à afficher" même quand l'outil réellement exécuté ce
        tour était une écriture."""
        text = _transactional_fallback_text(
            "STOCK_GET_HISTORY", "", {}, selected_tool="add_stock",
        )
        assert "rien trouvé" not in text
        assert "C'est noté" in text

    def test_a_genuinely_matching_read_tool_still_stays_neutral(self):
        text = _transactional_fallback_text(
            "STOCK_GET_HISTORY", "", {}, selected_tool="get_stock",
        )
        assert "rien trouvé" in text

    def test_stock_register_harvest_confirms_instead_of_the_generic_fallback(self):
        """Point 1 de la demande 2026-08-27 : une action confirmée ne doit
        plus retomber sur "C'est noté. Dites-moi ce que vous souhaitez
        faire..." — un gabarit dédié doit confirmer CE qui vient de se
        passer."""
        text = _transactional_fallback_text(
            "STOCK_REGISTER_HARVEST", "", {"product": "poulet", "quantity": 575, "unit": "UNITE"},
            selected_tool="add_stock",
        )
        assert "C'est noté" not in text
        assert "poulet" in text
        assert "575" in text

    def test_stock_register_harvest_is_not_honored_by_a_mismatched_tool(self):
        text = _transactional_fallback_text(
            "STOCK_REGISTER_HARVEST", "", {"product": "poulet"},
            selected_tool="get_farms",
        )
        assert "Récolte enregistrée" not in text

    @pytest.mark.parametrize(
        ("goal", "tool"),
        [
            ("STOCK_RECORD_MOVEMENT", "add_stock_movement_by_id"),
            ("STOCK_ADJUST", "adjust_stock_by_id"),
            ("STOCK_REMOVE_PARTIAL", "remove_stock_by_id"),
            ("STOCK_DELETE", "delete_stock_by_id"),
        ],
    )
    def test_other_stock_write_goals_also_confirm(self, goal, tool):
        text = _transactional_fallback_text(
            goal, "", {"product": "mais"}, selected_tool=tool,
        )
        assert "C'est noté" not in text
        assert "rien trouvé" not in text

    def test_stock_read_goals_are_unaffected_by_the_write_templates(self):
        # "STOCK_" est un préfixe partagé — les goals de LECTURE
        # (STOCK_GET_*) ne doivent matcher AUCUN des nouveaux gabarits
        # d'écriture (match exact, pas substring).
        text = _transactional_fallback_text("STOCK_GET_SUMMARY", "", {}, selected_tool="get_stocks")
        assert "rien trouvé" in text


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

    def test_the_transactional_fallback_uses_form_data_when_payload_is_empty(self):
        """Bug réel (2026-08-14) : "Votre appel d'offres pour 0 de *votre
        demande* a été enregistré avec succès" — pour les goals à formulaire
        (ex: appel d'offres), produit/quantité vivent dans `form_data`
        (voir `build_procurement_escalation`), pas dans `transaction_payload`
        qui est quasi vide au moment de la confirmation finale. Voir
        [[precommande-architecture-consolidation-2026-08]]."""
        c = ctx(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            execution_result={"status": "success"},
            transaction_payload={"resolved_id": "AUCTION_CONFIRM"},
            form_data={"product": "riz", "quantity": 10, "unit": "SAC"},
        )
        result = run(render_success(c))
        assert "riz" in result["final_response"]
        assert "10" in result["final_response"]

    def test_the_transactional_fallback_uses_confirmation_summary_payload_when_payload_is_empty(self):
        """Récidive réelle (2026-09-07) : "✅ Mamadou, Récolte enregistrée
        pour 0  pour votre demande." pour STOCK_REGISTER_HARVEST — même
        classe de bug que le 2026-08-14 ci-dessus, mais ce goal n'utilise
        pas `form_data`. Root cause précise : l'écriture DB (`add_stock`)
        avait bien reçu quantity=6000 — `state_cleaner_node` efface
        `transaction_payload` (`{"__reset__": True}`) dès que
        `status=="COMPLETED"`, et tourne AVANT `final_response` (edge réel :
        response_strategy -> state_cleaner -> final_response). Le gabarit
        transactionnel doit donc se rabattre sur
        `confirmation_summary_payload` (posé par `confirmation_gate` au tour
        du récap, jamais effacé par ce reset terminal) quand
        `transaction_payload` est vide."""
        c = ctx(
            current_goal="STOCK_REGISTER_HARVEST",
            execution_result={"status": "success"},
            transaction_payload={},
            confirmation_summary_payload={
                "product": "poulets", "quantity": 6000, "unit": "UNITE",
            },
        )
        result = run(render_success(c))
        assert "poulets" in result["final_response"]
        assert "6000" in result["final_response"]
        assert "pour 0" not in result["final_response"]
        assert "votre demande" not in result["final_response"]
        assert "pour 0" not in result["final_response"]

    def test_non_empty_payload_values_still_win_over_form_data(self):
        """`transaction_payload` reste prioritaire quand il a une valeur
        (le tour le plus récent) — `form_data` ne comble QUE les trous."""
        c = ctx(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            execution_result={"status": "success"},
            transaction_payload={"product": "maïs"},
            form_data={"product": "riz", "quantity": 10, "unit": "SAC"},
        )
        result = run(render_success(c))
        assert "maïs" in result["final_response"]
        assert "riz" not in result["final_response"]

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
# _render_market_snapshot / get_market_snapshot rendering
# =====================================================================

class TestRenderMarketSnapshot:
    """Voir [[precommande-architecture-consolidation-2026-08]] — demande
    utilisateur : un produit hors catalogue doit être signalé clairement
    avec la liste des vraies catégories, et un produit du catalogue doit
    afficher un prix de référence (admin en priorité, sinon moyenne)."""

    def test_product_not_in_catalog_lists_available_categories(self):
        text = _render_market_snapshot({
            "status": "success",
            "product_in_catalog": False,
            "message": "« riz » n'est pas encore disponible sur notre plateforme.",
            "available_categories": [
                {"id": "c1", "name": "Céréales", "icon": "🌾"},
                {"id": "c2", "name": "Légumes", "icon": "🥕"},
            ],
        })
        assert "riz" in text
        assert "Céréales" in text
        assert "🌾" in text
        assert "Légumes" in text

    def test_product_not_in_catalog_prefers_citing_concrete_products_with_prices(self):
        """Demande explicite utilisateur (2026-08-15) : "l'agent doit pouvoir
        citer les produits qui sont permis dans le catalogue et présenter les
        prix standard" — un nom de produit + son prix est plus actionnable
        qu'un simple nom de catégorie ("Céréales")."""
        text = _render_market_snapshot({
            "status": "success",
            "product_in_catalog": False,
            "message": "« coumba » n'est pas encore disponible sur notre plateforme.",
            "available_products": [
                {"name": "Tomate", "price": 300, "unit": "KG", "price_source": "admin"},
                {"name": "Maïs", "price": 250, "unit": "KG", "price_source": "market_average"},
                {"name": "Igname"},
            ],
            "available_categories": [{"id": "c1", "name": "Légumes", "icon": "🥕"}],
        })
        assert "Tomate" in text
        assert "300" in text
        assert "prix standard" in text
        assert "Maïs" in text
        assert "prix moyen" in text
        assert "Igname" in text
        # Les catégories ne doivent pas doublonner quand des produits concrets existent.
        assert "Catégories disponibles" not in text

    def test_product_not_in_catalog_falls_back_to_categories_when_no_products_available(self):
        text = _render_market_snapshot({
            "status": "success",
            "product_in_catalog": False,
            "message": "« riz » n'est pas encore disponible.",
            "available_products": [],
            "available_categories": [{"id": "c1", "name": "Céréales", "icon": "🌾"}],
        })
        assert "Céréales" in text

    def test_admin_standard_price_is_preferred_over_market_average(self):
        text = _render_market_snapshot({
            "status": "success",
            "product_in_catalog": True,
            "data": [{
                "product": "Tomate",
                "total_stock": 0,
                "standard_price": 300,
                "standard_unit": "KG",
                "standard_price_zone": "Ouagadougou",
                "price_source": "admin",
            }],
        })
        assert "Tomate" in text
        assert "Prix standard" in text
        assert "300" in text
        assert "Ouagadougou" in text

    def test_falls_back_to_market_average_when_no_standard_price(self):
        text = _render_market_snapshot({
            "status": "success",
            "product_in_catalog": True,
            "data": [{
                "product": "Tomate",
                "total_stock": 120,
                "min_price": 200,
                "avg_price": 250,
                "price_source": "market_average",
            }],
        })
        assert "moyen constaté" in text
        assert "250" in text
        assert "200" in text

    def test_matched_product_with_no_stock_and_no_standard_price_shows_the_message(self):
        text = _render_market_snapshot({
            "status": "success",
            "product_in_catalog": True,
            "data": [],
            "message": "« Tomate » est un produit reconnu, mais aucun producteur n'a de stock.",
        })
        assert "produit reconnu" in text

    def test_render_success_dispatches_to_market_snapshot_and_does_not_build_a_selection_menu(self):
        """Régression : `_render_flat_list` construit `options_ui` pour
        CHAQUE ligne (append inconditionnel), et CAS 2 (liste plate)
        écrase `ag_component` avec un menu « Faites votre choix » dès que
        `tool_data` est une liste — sans regarder `structured_sections_handled`.
        Sans l'exclusion dédiée à get_market_snapshot, un prix de référence se
        retrouvait transformé en faux menu de sélection."""
        c = ctx(
            selected_tool="get_market_snapshot",
            execution_result={
                "status": "success",
                "product_in_catalog": True,
                "data": [{
                    "product": "Tomate",
                    "total_stock": 120,
                    "min_price": 200,
                    "avg_price": 250,
                    "price_source": "market_average",
                }],
            },
        )
        result = run(render_success(c))
        assert "Tomate" in result["final_response"]
        assert "moyen constaté" in result["final_response"]
        ag_component = result.get("ag_ui_component")
        if ag_component:
            metadata = ag_component.get("kwargs", {}).get("metadata", {})
            assert metadata.get("mode") != "flat_list"

    def test_render_success_not_in_catalog_lists_categories_and_no_menu(self):
        c = ctx(
            selected_tool="get_market_snapshot",
            execution_result={
                "status": "success",
                "product_in_catalog": False,
                "message": "« riz » n'est pas encore disponible sur notre plateforme.",
                "available_categories": [{"id": "c1", "name": "Céréales", "icon": "🌾"}],
            },
        )
        result = run(render_success(c))
        assert "riz" in result["final_response"]
        assert "Céréales" in result["final_response"]
        ag_component = result.get("ag_ui_component")
        if ag_component:
            metadata = ag_component.get("kwargs", {}).get("metadata", {})
            assert metadata.get("mode") != "flat_list"


# =====================================================================
# _render_price_check / check_price_anomaly rendering
# =====================================================================

class TestRenderPriceCheck:
    """Voir [[precommande-architecture-consolidation-2026-08]] Round 5 —
    `check_price_anomaly` (VALIDATE_PRICE) ne renvoyait ni `message` ni
    `data`, tombant dans le gabarit transactionnel générique et affichant un
    texte sans rapport avec la vérification de prix demandée."""

    def test_high_anomaly_shows_the_warning_reason(self):
        text = _render_price_check({
            "is_anomaly": True, "level": "HIGH",
            "reason": "Prix proposé (2000 FCFA) est 4.0x le prix référence (500 FCFA).",
            "reference_price": 500,
        })
        assert "⚠️" in text
        assert "4.0x" in text

    def test_no_anomaly_with_a_reference_price_confirms_it(self):
        text = _render_price_check({"is_anomaly": False, "reference_price": 480})
        assert "cohérent" in text
        assert "480" in text

    def test_no_reference_price_at_all_still_gives_a_clean_message(self):
        text = _render_price_check({"is_anomaly": False, "reason": "Pas de prix de référence disponible."})
        assert "Pas de prix de référence" in text

    def test_render_success_dispatches_to_price_check_not_the_generic_template(self):
        """Régression bout-en-bout du bug réel : même si `goal` résolu pour
        l'UI porte encore un vestige "PROCUREMENT_CREATE_REQUEST" d'une
        tentative précédente, le dispatch par `selected_tool` doit rendre le
        texte prix, jamais "Votre appel d'offres..."."""
        c = ctx(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            selected_tool="check_price_anomaly",
            execution_result={"is_anomaly": False, "reference_price": 480},
        )
        result = run(render_success(c))
        assert "appel d'offres" not in result["final_response"]
        assert "cohérent" in result["final_response"]


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
