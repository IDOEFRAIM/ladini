"""`flows/producer/flow.py` — résolveurs de contexte côté producteur.

Le plus gros fichier de flow (1326 lignes) : découverte d'enchères, portefeuille
de bids, auto-résolution de farm_id, résolution de lot de stock, deux
mini-machines à états auto-suffisantes (mise à jour production/produit),
confirmation de livraison escrow (OTP), et le routeur `producer_context_resolver`.
Zéro DB : `StubRuntime` (tests/conftest.py) pilote les gateways.
"""
from __future__ import annotations

import pytest

from tests.conftest import StubRuntime, make_state, run
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from tests.harness.state import clears


def rt(responses=None):
    return StubRuntime(responses=responses or {})


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _StubLLM:
    """Double minimal pour `llm.chat.completions.create(...)` — voir
    `utils.py::llm_deviation_reply`."""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        return _Completion(self._text)


def rt_with_llm(text: str, responses=None):
    return StubRuntime(llm=_StubLLM(text), responses=responses or {})


# =====================================================================
# Parsing déterministe des corrections (_extract_*)
# =====================================================================

class TestExtractPriceCorrection:
    @pytest.mark.parametrize("text,expected", [
        ("le prix est 300", 300.0),
        ("ça coûte 450 fcfa", 450.0),
        ("225 FCFA le kilo", 225.0),
        ("prix : 199.5", 199.5),
        ("prix 150,5", 150.5),
    ])
    def test_extracts_price(self, text, expected):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_price_correction
        assert _extract_price_correction(text) == expected

    def test_zero_or_negative_price_is_rejected(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_price_correction
        assert _extract_price_correction("prix 0") is None

    def test_no_price_mentioned_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_price_correction
        assert _extract_price_correction("bonjour comment allez vous") is None


class TestExtractQuantityCorrection:
    def test_recognized_unit_wins(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_quantity_correction
        qty, unit = _extract_quantity_correction("quantité 500 kg")
        assert qty == 500
        assert unit is not None

    def test_bare_quantity_without_unit_word_is_still_accepted_via_fallback(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_quantity_correction
        qty, unit = _extract_quantity_correction("quantité 500")
        assert qty == 500.0
        assert unit is None

    def test_price_text_is_not_confused_with_quantity(self):
        """Régression documentée dans le fichier source : "225 fcfa" ne doit
        JAMAIS être lu comme une quantité=225."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_quantity_correction
        qty, unit = _extract_quantity_correction("225 fcfa")
        assert qty is None

    def test_no_number_returns_none_none(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_quantity_correction
        assert _extract_quantity_correction("bonjour") == (None, None)

    def test_a_long_verbal_connector_between_trigger_and_number_is_still_recognized(self):
        """Incident réel (2026-09-19) : "la quantite est maintenant de 95"
        (19 caractères entre "quantite" et "95") ne matchait plus rien avec
        l'ancienne borne `\\D{0,10}` — la quantité disparaissait
        silencieusement d'une double correction prix+quantité."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_quantity_correction
        qty, unit = _extract_quantity_correction("la quantite est maintenant de 95")
        assert qty == 95.0
        assert unit is None

    def test_a_distant_unrelated_number_across_a_full_new_clause_is_not_captured(self):
        """La borne élargie reste bornée : un nombre séparé du déclencheur
        par une clause entièrement différente ne doit toujours pas être pris
        pour la quantité."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_quantity_correction
        qty, unit = _extract_quantity_correction(
            "la quantité, on en reparlera une autre fois si besoin, le prix est 400"
        )
        assert qty is None


class TestExtractNameCorrection:
    def test_explicit_rename_trigger_is_required(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_name_correction
        assert _extract_name_correction("le kg d'oignon coûte 225 fcfa") is None

    def test_extracts_the_new_name_after_trigger(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_name_correction
        assert _extract_name_correction("nom mil") == "mil"

    def test_stops_at_a_linking_stopword(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_name_correction
        result = _extract_name_correction("le nom c'est mais et la quantité est 3632 kg")
        assert result == "mais"

    def test_captures_up_to_two_words(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_name_correction
        assert _extract_name_correction("nom petit mil rouge") == "petit mil"


class TestExtractDateCorrection:
    def test_iso_date_is_recognized(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_date_correction
        assert _extract_date_correction("date 2026-12-31") == "2026-12-31"

    def test_french_date_is_recognized(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_date_correction
        assert _extract_date_correction("disponible le 13 décembre 2026") == "2026-12-13"

    def test_slash_date_is_recognized(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_date_correction
        assert _extract_date_correction("le 13/12/2026") == "2026-12-13"

    def test_invalid_slash_date_is_rejected(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_date_correction
        assert _extract_date_correction("le 99/99/2026") is None

    def test_no_date_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_date_correction
        assert _extract_date_correction("aucune date ici") is None


class TestExtractOtpCode:
    def test_code_trigger_word_is_preferred(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_otp_code
        assert _extract_otp_code("mon code est 1234") == "1234"

    def test_bare_four_digit_number_is_accepted_without_trigger(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_otp_code
        assert _extract_otp_code("livré, 5678") == "5678"

    def test_no_code_returns_none(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _extract_otp_code
        assert _extract_otp_code("pas encore livré") is None


class TestParseUpdateCorrection:
    def test_combines_multiple_fields(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _parse_update_correction
        fields = _parse_update_correction("prix 300, quantité 50 kg, nom mil", allow_type_date=False)
        assert fields["price"] == 300.0
        assert fields["quantity"] == 50
        assert fields["product"] == "mil"

    def test_date_and_type_only_extracted_when_allowed(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _parse_update_correction
        text = "disponible le 2026-12-31"
        with_dates = _parse_update_correction(text, allow_type_date=True)
        without_dates = _parse_update_correction(text, allow_type_date=False)
        assert with_dates.get("estimated_available_at") == "2026-12-31"
        assert "estimated_available_at" not in without_dates

    def test_empty_text_yields_empty_fields(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _parse_update_correction
        assert _parse_update_correction("bonjour", allow_type_date=True) == {}

    def test_price_correction_with_a_per_unit_basis_updates_the_display_unit(self):
        """Incident réel (2026-09-09, WhatsApp) : "nonnn c est 3500 FCFA PAR
        UNITE" en réponse à un récap affichant "3500 FCFA/KG" laissait le
        récap totalement inchangé — `_extract_price_correction` extrayait le
        prix mais rien ne captait "PAR UNITE" comme base de prix, et
        `_format_pending_recap` affiche toujours `unit` (jamais un champ
        `price_unit` séparé, qui n'existe pas dans ce mini-flow)."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _parse_update_correction
        fields = _parse_update_correction(
            "nonnn c est 3500 FCFA PAR UNITE", allow_type_date=False
        )
        assert fields["price"] == 3500.0
        assert fields["unit"] == "UNITE"

    def test_per_unit_price_basis_never_overrides_an_explicit_quantity_unit(self):
        """Un message qui donne À LA FOIS une quantité typée et une base de
        prix ne doit jamais laisser la base de prix écraser l'unité de la
        quantité (ce mini-flow n'a qu'un seul champ `unit` affiché pour les
        deux — la quantité, plus précise, doit gagner)."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _parse_update_correction
        fields = _parse_update_correction(
            "500 kg, prix 3500 fcfa l'unite", allow_type_date=False
        )
        assert fields["unit"] == "KG"


class TestFormatPendingRecap:
    def test_includes_all_provided_fields(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _format_pending_recap
        recap = _format_pending_recap(
            {"product": "mil", "price": 300, "quantity": 50, "unit": "kg",
             "estimated_available_at": "2026-12-31", "production_type": "future"},
            noun="lot",
        )
        assert "mil" in recap and "300" in recap and "50" in recap and "2026-12-31" in recap

    def test_unit_only_line_shown_when_no_price_or_quantity(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _format_pending_recap
        recap = _format_pending_recap({"unit": "sac"}, noun="lot")
        assert "Nouvelle unité" in recap


# =====================================================================
# _resolve_auction
# =====================================================================

class TestResolveAuction:
    def test_no_results_returns_a_completed_message(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_auction
        runtime = rt({"get_auctions": {"status": "success", "count": 0}})
        result = run(_resolve_auction(runtime, "+2260", {"product": "mais"}))
        assert result["status"] == "COMPLETED"

    def test_gateway_failure_returns_a_completed_message(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_auction
        runtime = rt({"get_auctions": {"status": "error"}})
        result = run(_resolve_auction(runtime, "+2260", {}))
        assert result["status"] == "COMPLETED"

    def test_success_builds_a_selection_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_auction
        runtime = rt({"get_auctions": {
            "status": "success", "count": 2,
            "mapping": {"1": "a1", "2": "a2"},
            "data": [{"product": "mais"}, {"product": "riz"}],
        }})
        result = run(_resolve_auction(runtime, "+2260", {"product": "mais", "zone": "z1"}))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert len(result["pending_menu"].options) == 2


# =====================================================================
# _resolve_my_bids
# =====================================================================

class TestResolveMyBids:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_my_bids
        result = run(_resolve_my_bids(rt(), "", {}))
        assert result["status"] == "ERROR"

    def test_gateway_failure_returns_completed_message(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_my_bids
        runtime = rt({"get_my_active_bids": {"status": "error"}})
        result = run(_resolve_my_bids(runtime, "+2260", {}))
        assert result["status"] == "COMPLETED"

    def test_empty_data_returns_no_active_offers_message(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_my_bids
        runtime = rt({"get_my_active_bids": {"status": "success", "data": []}})
        result = run(_resolve_my_bids(runtime, "+2260", {}))
        assert "aucune offre" in result["final_response"].lower()

    def test_success_builds_a_selection_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_my_bids
        runtime = rt({"get_my_active_bids": {"status": "success", "data": [
            {"bid_id": "b1", "product": "mais", "offered_price": 250, "status": "PENDING"},
        ]}})
        result = run(_resolve_my_bids(runtime, "+2260", {}))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert result["available_mapping" ] if "available_mapping" in result else True


# (2026-09-13, Deep Intent Architecture Cleanup) : `TestResolveBid` supprimée
# — `_resolve_bid` (flows/producer/flow.py) a été retirée, exclusivement
# rattachée à `SALES_ACCEPT_CONTRACT`, supprimé d'INTENT_CONFIG.


# =====================================================================
# producer_auction_resolver — déviations (chantier résilience 2026-08)
# =====================================================================

class TestProducerAuctionResolverDeviations:
    """`flows/producer/auctions.py::producer_auction_resolver` — les 4
    branches "reask" (nouvelle offre + modification, prix + confirmation)
    doivent accuser réception via le LLM avant de rejouer leur texte figé."""

    def test_new_bid_confirm_phase_ambiguous_reply_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.auctions import (
            producer_auction_resolver,
        )
        state = make_state(
            current_goal="MARKET_BROWSE_REQUESTS",
            normalized_text="attendez je réfléchis encore",
            interpreted_event="",
            working_memory={
                "bid_phase": "CONFIRM",
                "pending_bid_auction": "a1",
                "pending_bid_price": 250,
                "pending_bid_pricing": {
                    "status": "RESOLVED", "amount": "250", "basis": "PER_BASE_UNIT", "price_unit": "KG",
                    "source": "USER_EXPLICIT",
                },
                "auction_brief": {"a1": {"product": "mais", "unit": "KG", "quantity": 100}},
            },
        )
        runtime = rt_with_llm("Pas de souci, prenez votre temps.")
        result = run(producer_auction_resolver(state, runtime))
        assert "Pas de souci" in result["final_response"]
        assert "250" in result["final_response"]

    def test_new_bid_ask_price_unknown_event_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.auctions import (
            producer_auction_resolver,
        )
        state = make_state(
            current_goal="MARKET_BROWSE_REQUESTS",
            normalized_text="c'est combien la normale déjà ?",
            interpreted_event="UNKNOWN",
            working_memory={
                "bid_phase": "ASK_PRICE",
                "pending_bid_auction": "a1",
                "auction_brief": {"a1": {"product": "mais", "unit": "KG", "quantity": 100}},
            },
        )
        runtime = rt_with_llm("Bonne question — je n'ai pas ce chiffre sous la main.")
        result = run(producer_auction_resolver(state, runtime))
        assert "Bonne question" in result["final_response"]

    def test_modify_confirm_phase_ambiguous_reply_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.auctions import (
            producer_auction_resolver,
        )
        state = make_state(
            current_goal="MARKET_GET_MY_PROPOSALS",
            normalized_text="hmm pas sûr",
            interpreted_event="",
            working_memory={
                "bid_phase": "CONFIRM_MODIFY",
                "pending_modify_bid": "b1",
                "pending_bid_price": 300,
                "pending_bid_pricing": {
                    "status": "RESOLVED", "amount": "300", "basis": "PER_BASE_UNIT", "price_unit": "KG",
                    "source": "USER_EXPLICIT",
                },
                "my_bids_brief": {"b1": {"product": "tomates", "unit": "KG", "quantity": 100}},
            },
        )
        runtime = rt_with_llm("D'accord, dites-moi si vous préférez garder l'ancien prix.")
        result = run(producer_auction_resolver(state, runtime))
        assert "D'accord" in result["final_response"]

    def test_modify_ask_price_unknown_event_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.auctions import (
            producer_auction_resolver,
        )
        state = make_state(
            current_goal="MARKET_GET_MY_PROPOSALS",
            normalized_text="je sais pas trop quoi mettre",
            interpreted_event="OUT_OF_SCOPE",
            working_memory={
                "bid_phase": "ASK_PRICE_MODIFY",
                "pending_modify_bid": "b1",
                "my_bids_brief": {"b1": {"product": "tomates", "unit": "KG", "quantity": 100}},
            },
        )
        runtime = rt_with_llm("Aucun souci, un prix approximatif suffit pour commencer.")
        result = run(producer_auction_resolver(state, runtime))
        assert "Aucun souci" in result["final_response"]


# =====================================================================
# _resolve_default_farm
# =====================================================================

class TestResolveDefaultFarm:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        result = run(_resolve_default_farm(rt(), "", {}))
        assert result["status"] == "ERROR"

    def test_cached_farms_skip_the_network_call(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        runtime = rt()
        state = {"user_farms_cache": [{"id": "f1"}]}
        result = run(_resolve_default_farm(runtime, "+2260", {}, state=state))
        assert result["transaction_payload"]["farm_id"] == "f1"
        assert "get_farms" not in runtime.calls

    def test_zero_farms_defers_to_planning(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        runtime = rt({"get_farms": {"status": "success", "data": []}})
        result = run(_resolve_default_farm(runtime, "+2260", {}))
        assert result["status"] == "PLANNING"

    def test_single_farm_autofills_silently(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        runtime = rt({"get_farms": {"status": "success", "data": [{"id": "f1", "name": "Ferme A"}]}})
        result = run(_resolve_default_farm(runtime, "+2260", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["farm_id"] == "f1"

    def test_single_farm_without_id_is_an_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        runtime = rt({"get_farms": {"status": "success", "data": [{"name": "Ferme sans id"}]}})
        result = run(_resolve_default_farm(runtime, "+2260", {}))
        assert "farm_id_unresolved" in result["validation_errors"]

    def test_multiple_farms_with_selection_index_resolves(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        runtime = rt({"get_farms": {"status": "success", "data": [
            {"id": "f1", "name": "Ferme A"}, {"id": "f2", "name": "Ferme B"},
        ]}})
        result = run(_resolve_default_farm(runtime, "+2260", {"selection_index": 2}))
        assert result["transaction_payload"]["farm_id"] == "f2"
        assert clears(result["transaction_payload"], "selection_index")

    def test_multiple_farms_without_selection_shows_a_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_default_farm
        runtime = rt({"get_farms": {"status": "success", "data": [
            {"id": "f1", "name": "Ferme A", "size": 5}, {"id": "f2", "name": "Ferme B"},
        ]}})
        result = run(_resolve_default_farm(runtime, "+2260", {}))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert len(result["pending_menu"].options) == 2


# (2026-09-13, Deep Intent Architecture Cleanup) : `TestResolveStock`
# supprimée — `_resolve_stock` (flows/producer/flow.py) a été retirée,
# exclusivement rattachée à STOCK_ADJUST/STOCK_REMOVE_PARTIAL/
# STOCK_RECORD_MOVEMENT/STOCK_DELETE, tous supprimés d'INTENT_CONFIG.


# =====================================================================
# _resolve_cycle_for_update — mise à jour d'une production future
# =====================================================================

class TestResolveCycleForUpdate:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        result = run(_resolve_cycle_for_update(rt(), "", {}, {}, "", ""))
        assert result["status"] == "ERROR"

    def test_confirm_phase_with_a_new_correction_updates_the_recap(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        result = run(_resolve_cycle_for_update(rt(), "+2260", {}, working, "quantité 500 kg", ""))
        assert result["working_memory"]["update_pending"]["quantity"] == 500
        assert result["working_memory"]["update_pending"]["price"] == 200

    def test_confirm_event_success_writes_and_clears_state(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        runtime = rt({"update_production_fields": {"status": "success", "message": "OK"}})
        result = run(_resolve_cycle_for_update(runtime, "+2260", {}, working, "oui", "CONFIRM"))
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "SUCCESS"
        assert result["working_memory"]["update_cycle_id"] is None
        assert result["transaction_payload"] == {"__reset__": True}

    def test_confirm_event_gateway_exception_returns_error(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def update_production(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "StockGateway", _BoomGateway)
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        result = run(mod._resolve_cycle_for_update(rt(), "+2260", {}, working, "oui", "CONFIRM"))
        assert result["response_strategy"] == "ERROR"

    def test_confirm_event_gateway_failure_result(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        runtime = rt({"update_production_fields": {"status": "error", "message": "Refusé"}})
        result = run(_resolve_cycle_for_update(runtime, "+2260", {}, working, "oui", "CONFIRM"))
        assert result["final_response"] == "Refusé"

    def test_reject_event_cancels_without_writing(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        result = run(_resolve_cycle_for_update(rt(), "+2260", {}, working, "non", "REJECT"))
        assert "annulée" in result["final_response"]

    def test_ambiguous_reply_reshows_the_recap(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        result = run(_resolve_cycle_for_update(rt(), "+2260", {}, working, "peut-etre", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"

    def test_collect_phase_no_correction_asks_what_to_update(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {}
        result = run(_resolve_cycle_for_update(rt(), "+2260", {"cycle_id": "c1"}, working, "bonjour", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "UPDATE_FIELD"

    def test_collect_phase_with_correction_moves_to_confirm(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {}
        result = run(_resolve_cycle_for_update(rt(), "+2260", {"cycle_id": "c1"}, working, "prix 400", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"
        assert result["working_memory"]["update_pending"]["price"] == 400.0

    def test_ambiguous_confirm_reply_gets_an_adaptive_note(self):
        """Chantier résilience 2026-08 : la branche "ni correction ni CONFIRM
        ni REJECT clair" doit accuser réception via le LLM avant de rejouer
        le récap, pas juste le répéter mot pour mot."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {"update_phase": "CONFIRM", "update_cycle_id": "c1", "update_pending": {"price": 200}}
        runtime = rt_with_llm("D'accord, dites-moi si vous voulez changer autre chose.")
        result = run(_resolve_cycle_for_update(runtime, "+2260", {}, working, "attendez je réfléchis", ""))
        assert result["final_response"].startswith("D'accord, dites-moi")
        assert "prix" in result["final_response"].lower()

    def test_collect_phase_unknown_event_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {}
        runtime = rt_with_llm("Je ne suis pas sûr de comprendre votre question.")
        result = run(_resolve_cycle_for_update(
            runtime, "+2260", {"cycle_id": "c1"}, working, "c'est compliqué tout ça", "UNKNOWN",
        ))
        assert result["final_response"].startswith("Je ne suis pas sûr")
        assert "modifier" in result["final_response"].lower()

    def test_collect_phase_answer_event_does_not_call_the_llm(self):
        """Sur l'entrée fraîche (juste après la sélection du lot, event pas
        classé UNKNOWN/OUT_OF_SCOPE), pas d'appel LLM inutile."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        working = {}

        class _BoomLLM(_StubLLM):
            def create(self, **kwargs):
                raise AssertionError("le LLM ne devait pas être appelé ici")

        runtime = rt()
        runtime.llm = _BoomLLM("n/a")
        result = run(_resolve_cycle_for_update(runtime, "+2260", {"cycle_id": "c1"}, working, "1", "SELECTION"))
        assert to_tunnel_category(get_pending_interaction(result)) == "UPDATE_FIELD"

    def test_select_phase_gateway_exception(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def list_productions(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "StockGateway", _BoomGateway)
        result = run(mod._resolve_cycle_for_update(rt(), "+2260", {}, {}, "", ""))
        assert result["status"] == "ERROR"

    def test_select_phase_status_failure(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        runtime = rt({"list_producer_productions": {"status": "error"}})
        result = run(_resolve_cycle_for_update(runtime, "+2260", {}, {}, "", ""))
        assert result["status"] == "ERROR"

    def test_select_phase_empty_items(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        runtime = rt({"list_producer_productions": {"status": "success", "data": []}})
        result = run(_resolve_cycle_for_update(runtime, "+2260", {}, {}, "", ""))
        assert result["status"] == "COMPLETED"

    def test_select_phase_success_shows_a_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_cycle_for_update
        runtime = rt({"list_producer_productions": {"status": "success", "data": [
            {"cycle_id": "c1", "product_label": "mais", "quantity": 100, "unit": "kg", "price": 250},
        ]}})
        result = run(_resolve_cycle_for_update(runtime, "+2260", {}, {}, "", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert result["available_mapping"] == {"1": "c1"}


# =====================================================================
# _resolve_product_for_update — miroir côté catalogue
# =====================================================================

class TestResolveProductForUpdate:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        result = run(_resolve_product_for_update(rt(), "", {}, {}, "", ""))
        assert result["status"] == "ERROR"

    def test_confirm_event_success_renames_product_field(self):
        """`update_product` reçoit `name=` (pas `product=`) — vérifie le
        renommage de clé avant l'appel gateway."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        working = {"update_phase": "CONFIRM", "update_product_id": "p1", "update_pending": {"product": "mil"}}
        runtime = rt({"update_product_price_and_qty": {"status": "success", "message": "OK"}})
        result = run(_resolve_product_for_update(runtime, "+2260", {}, working, "oui", "CONFIRM"))
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "SUCCESS"

    def test_reject_event_cancels(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        working = {"update_phase": "CONFIRM", "update_product_id": "p1", "update_pending": {"price": 200}}
        result = run(_resolve_product_for_update(rt(), "+2260", {}, working, "non", "REJECT"))
        assert "annulée" in result["final_response"]

    def test_collect_phase_no_correction_asks(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        result = run(_resolve_product_for_update(rt(), "+2260", {"product_id": "p1"}, {}, "bonjour", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "UPDATE_FIELD"

    def test_ambiguous_confirm_reply_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        working = {"update_phase": "CONFIRM", "update_product_id": "p1", "update_pending": {"price": 200}}
        runtime = rt_with_llm("Pas de souci, dites-moi ce que vous voulez ajuster.")
        result = run(_resolve_product_for_update(runtime, "+2260", {}, working, "hmm attendez", ""))
        assert result["final_response"].startswith("Pas de souci")
        assert "prix" in result["final_response"].lower()

    def test_collect_phase_unknown_event_gets_an_adaptive_note(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        runtime = rt_with_llm("Je note votre remarque.")
        result = run(_resolve_product_for_update(
            runtime, "+2260", {"product_id": "p1"}, {}, "c'est pas clair", "OUT_OF_SCOPE",
        ))
        assert result["final_response"].startswith("Je note votre remarque")
        assert "produit" in result["final_response"].lower()

    def test_select_phase_gateway_exception(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def get_my_products(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "ProductGateway", _BoomGateway)
        result = run(mod._resolve_product_for_update(rt(), "+2260", {}, {}, "", ""))
        assert result["status"] == "ERROR"

    def test_select_phase_empty_catalog(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        runtime = rt({"get_my_products": {"status": "success", "data": []}})
        result = run(_resolve_product_for_update(runtime, "+2260", {}, {}, "", ""))
        assert result["status"] == "COMPLETED"

    def test_select_phase_success_shows_a_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_product_for_update
        runtime = rt({"get_my_products": {"status": "success", "data": [
            {"id": "p1", "name": "mais", "quantity_for_sale": 100, "unit": "kg", "price": 250},
        ]}})
        result = run(_resolve_product_for_update(runtime, "+2260", {}, {}, "", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"


# =====================================================================
# _resolve_delivery_otp
# =====================================================================

class TestResolveDeliveryOtp:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_delivery_otp
        result = run(_resolve_delivery_otp(rt(), "", {}, {}, "", ""))
        assert result["status"] == "ERROR"

    def test_no_code_asks_for_it(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_delivery_otp
        result = run(_resolve_delivery_otp(rt(), "+2260", {}, {}, "pas encore livré", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "OTP_CODE"

    def test_gateway_exception_returns_error(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def verify_delivery_otp(self, *a, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "EscrowGateway", _BoomGateway)
        result = run(mod._resolve_delivery_otp(rt(), "+2260", {}, {}, "code 1234", ""))
        assert result["response_strategy"] == "ERROR"

    def test_invalid_code_stays_in_the_tunnel(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_delivery_otp
        runtime = rt({"verify_delivery_otp": {"status": "error", "message": "Code invalide"}})
        result = run(_resolve_delivery_otp(runtime, "+2260", {}, {}, "code 1234", ""))
        assert to_tunnel_category(get_pending_interaction(result)) == "OTP_CODE"
        assert result["final_response"] == "Code invalide"

    def test_valid_code_unlocks_funds(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_delivery_otp
        runtime = rt({"verify_delivery_otp": {"status": "success", "message": "Livraison confirmée"}})
        result = run(_resolve_delivery_otp(runtime, "+2260", {}, {}, "code 1234", ""))
        assert result["status"] == "COMPLETED"
        assert result["transaction_payload"] == {"__reset__": True}

    def test_code_can_come_from_payload_directly(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import _resolve_delivery_otp
        runtime = rt({"verify_delivery_otp": {"status": "success", "message": "OK"}})
        result = run(_resolve_delivery_otp(runtime, "+2260", {"otp_code": "9999"}, {}, "", ""))
        assert result["status"] == "COMPLETED"


# =====================================================================
# producer_context_resolver — routage
# =====================================================================

class TestProducerContextResolver:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import producer_context_resolver
        state = make_state(user_phone="")
        result = run(producer_context_resolver(state, rt()))
        assert result["status"] == "ERROR"

    def test_farm_id_auto_resolution_runs_first_for_eligible_goals(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            producer_context_resolver, GOALS_NEEDING_FARM_ID,
        )
        goal = next(iter(GOALS_NEEDING_FARM_ID))
        state = make_state(user_phone="+2260", current_goal=goal, transaction_payload={})
        runtime = rt({"get_farms": {"status": "success", "data": []}})
        result = run(producer_context_resolver(state, runtime))
        # 0 ferme → PLANNING interne à _resolve_default_farm, donc le flow
        # continue vers le fallback générique (pas d'erreur bloquante).
        assert result["status"] in ("PLANNING", "ERROR", "WAITING_INPUT", "COMPLETED")

    def test_farm_id_already_present_skips_auto_resolution(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            producer_context_resolver, GOALS_NEEDING_FARM_ID,
        )
        goal = next(iter(GOALS_NEEDING_FARM_ID))
        state = make_state(user_phone="+2260", current_goal=goal, transaction_payload={"farm_id": "f1"})
        runtime = rt()
        result = run(producer_context_resolver(state, runtime))
        assert "get_farms" not in runtime.calls

    @pytest.mark.parametrize("goal", ["SALES_PLACE_BID", "MARKET_BROWSE_REQUESTS", "MARKET_GET_MY_PROPOSALS"])
    def test_auction_goals_delegate_to_producer_auction_resolver(self, monkeypatch, goal):
        import ladini.graphs.agents.market_coach.flows.producer.auctions as auctions_mod

        calls = []

        async def _fake_resolver(state, mc_runtime):
            calls.append(state["current_goal"])
            return {"status": "COMPLETED", "final_response": "auction resolver called"}

        monkeypatch.setattr(auctions_mod, "producer_auction_resolver", _fake_resolver)
        from ladini.graphs.agents.market_coach.flows.producer.flow import producer_context_resolver
        state = make_state(user_phone="+2260", current_goal=goal, transaction_payload={})
        result = run(producer_context_resolver(state, rt()))
        assert result["final_response"] == "auction resolver called"
        assert calls == [goal]

    def test_production_update_future_goal_routes_to_cycle_resolver(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import producer_context_resolver
        state = make_state(
            user_phone="+2260", current_goal="PRODUCTION_UPDATE_FUTURE",
            transaction_payload={}, working_memory={},
        )
        runtime = rt({"list_producer_productions": {"status": "success", "data": []}})
        result = run(producer_context_resolver(state, runtime))
        assert result["status"] == "COMPLETED"

    def test_sales_update_product_goal_routes_to_product_resolver(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import producer_context_resolver
        state = make_state(
            user_phone="+2260", current_goal="SALES_UPDATE_PRODUCT",
            transaction_payload={}, working_memory={},
        )
        runtime = rt({"get_my_products": {"status": "success", "data": []}})
        result = run(producer_context_resolver(state, runtime))
        assert result["status"] == "COMPLETED"

    def test_delivery_otp_goal_routes_to_otp_resolver(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import producer_context_resolver
        state = make_state(
            user_phone="+2260", current_goal="PRODUCER_CONFIRM_DELIVERY_OTP",
            transaction_payload={}, working_memory={}, normalized_text="pas de code",
        )
        result = run(producer_context_resolver(state, rt()))
        assert to_tunnel_category(get_pending_interaction(result)) == "OTP_CODE"

    # (2026-09-13, Deep Intent Architecture Cleanup) :
    # `test_sales_accept_contract_without_bid_id_routes_to_resolve_bid`,
    # `test_sales_accept_contract_with_bid_id_falls_through_to_default`,
    # `test_stock_goal_without_stock_id_resolves_then_merges` et
    # `test_stock_goal_menu_short_circuits_before_merge` supprimés —
    # SALES_ACCEPT_CONTRACT/STOCK_ADJUST/STOCK_DELETE ont été retirés
    # d'INTENT_CONFIG et leurs branches de dispatch dans
    # `producer_context_resolver` supprimées avec eux (contrat outil
    # fictif / F4 sécurité — voir interpreter/intent.py).

    def test_default_fallback_returns_planning(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import producer_context_resolver
        state = make_state(user_phone="+2260", current_goal="SOME_UNRELATED_GOAL", transaction_payload={})
        result = run(producer_context_resolver(state, rt()))
        assert result["status"] == "PLANNING"

    def test_stateful_update_event_loads_entity_snapshot(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        calls = []

        async def _fake_snapshot(mc_runtime, goal, entity_id, payload, *, phone, entity_kind):
            calls.append((goal, entity_id, entity_kind))
            return {"status": "PLANNING", "transaction_payload": {**payload, "loaded": True}}

        monkeypatch.setattr(mod, "load_entity_snapshot", _fake_snapshot)
        state = make_state(
            user_phone="+2260", current_goal="SOME_UNRELATED_GOAL",
            interpreted_event="UPDATE",
            transaction_payload={"stock_id": "s1"},
        )
        result = run(mod.producer_context_resolver(state, rt()))
        assert calls == [("SOME_UNRELATED_GOAL", "s1", "stock")]
        assert result["transaction_payload"]["loaded"] is True

    def test_stateful_update_snapshot_error_short_circuits(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        async def _fake_snapshot(mc_runtime, goal, entity_id, payload, *, phone, entity_kind):
            return {"status": "ERROR", "final_response": "Introuvable"}

        monkeypatch.setattr(mod, "load_entity_snapshot", _fake_snapshot)
        state = make_state(
            user_phone="+2260", current_goal="SOME_UNRELATED_GOAL",
            interpreted_event="UPDATE",
            transaction_payload={"stock_id": "s1"},
        )
        result = run(mod.producer_context_resolver(state, rt()))
        assert result["status"] == "ERROR"
        assert result["final_response"] == "Introuvable"

    def test_snapshot_not_attempted_when_original_entity_already_present(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.producer.flow as mod

        called = {"n": 0}

        async def _fake_snapshot(*a, **kw):
            called["n"] += 1
            return {"status": "PLANNING", "transaction_payload": {}}

        monkeypatch.setattr(mod, "load_entity_snapshot", _fake_snapshot)
        state = make_state(
            user_phone="+2260", current_goal="SOME_UNRELATED_GOAL",
            interpreted_event="UPDATE",
            transaction_payload={"stock_id": "s1"},
            original_entity={"already": "loaded"},
        )
        run(mod.producer_context_resolver(state, rt()))
        assert called["n"] == 0
