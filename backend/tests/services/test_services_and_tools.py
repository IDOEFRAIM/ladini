"""Services métier + couche d'exécution d'outils (MCP).

Couvre : rendu du récapitulatif de confirmation, enrichissement de slots,
contrats Pydantic, déballage d'enveloppes MCP, résolution d'arguments.
"""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary,
)
from ladini.graphs.agents.market_coach.services.domain.slot_enrichment import (
    enrich_payload_from_text,
    extract_production_type_from_text,
    extract_future_datetime_from_text,
    extract_surface_from_text,
)
from ladini.graphs.agents.market_coach.interpreter.contracts import enforce_contract
from ladini.graphs.agents.market_coach.utils import (
    _normalize_quantity_to_kg,
    canonical_unit_label,
    normalize_slot_keys,
    is_success_response,
)
from tests.conftest import StubRuntime, run


# =====================================================================
# RÉCAPITULATIF DE CONFIRMATION — ce que l'utilisateur voit avant d'engager
# =====================================================================

class TestConfirmationSummary:
    def test_price_unit_shown_as_typed_by_user(self):
        """« 200 tonnes » + « 10000 FCFA/kg » : le prix s'affiche par KG."""
        payload = _normalize_quantity_to_kg(
            {"product": "tomates", "quantity": 200, "unit": "TONNE",
             "price": 10000, "price_unit": "KG"}
        )
        s = build_confirmation_summary("SALES_PUBLISH_PRODUCT", payload)
        assert "FCFA/KG" in s
        assert "⚠️" not in s, "aucun avertissement : la quantité est déjà convertie en KG"

    def test_genuine_unit_mismatch_warns_the_user(self):
        """Prix par SAC sur une quantité en KG : conflit réel -> avertir."""
        payload = _normalize_quantity_to_kg(
            {"product": "tomates", "quantity": 50, "unit": "KG",
             "price": 5000, "price_unit": "SAC"}
        )
        s = build_confirmation_summary("SALES_PUBLISH_PRODUCT", payload)
        assert "⚠️" in s and "SAC" in s

    def test_currency_contamination_is_rejected(self):
        """Le LLM recopie parfois « FCFA/UNITE » : ne jamais afficher FCFA/FCFA/..."""
        payload = {"product": "mais", "quantity": 10, "unit": "KG",
                   "price": 100, "price_unit": "FCFA/UNITE"}
        s = build_confirmation_summary("SALES_PUBLISH_PRODUCT", payload)
        assert "FCFA/FCFA" not in s

    def test_partial_update_shows_only_changed_fields(self):
        s = build_confirmation_summary("SALES_UPDATE_PRODUCT", {"product": "maïs"})
        assert "Nouveau nom" in s
        assert "quantité" not in s.lower()

    def test_unknown_goal_never_crashes(self):
        assert build_confirmation_summary("GOAL_INEXISTANT", {}).strip()

    def test_unknown_goal_never_leaks_the_internal_constant_name(self):
        """Bug réel confirmé (2026-08-18) : un mauvais aiguillage a fait
        atteindre ce fallback avec goal="BUYER_REQUEST" (normalement
        `handled_by_flow`, jamais censé passer par un récap générique) — le
        récap affichait littéralement "Validation de l'opération :
        BUYER_REQUEST", un nom de constante interne exposé tel quel à
        l'utilisateur. Quel que soit le goal en entrée, son nom brut ne doit
        JAMAIS apparaître dans le texte renvoyé."""
        s = build_confirmation_summary("BUYER_REQUEST", {"product": "oignon"})
        assert "BUYER_REQUEST" not in s
        assert "oignon" in s

    def test_unknown_goal_with_no_product_still_never_leaks_the_name(self):
        s = build_confirmation_summary("SOME_UNMAPPED_GOAL", {})
        assert "SOME_UNMAPPED_GOAL" not in s

    def test_procurement_create_request_shows_the_price_unit(self):
        """Régression production (2026-08) : le récap d'un appel d'offres
        affichait « 300 FCFA » sans unité — ambigu (par kg ? par tonne ?),
        alors que la quantité était en TONNE et le prix saisi par KG."""
        payload = _normalize_quantity_to_kg(
            {"product": "carottes", "quantity": 500, "unit": "TONNE",
             "price": 300, "price_unit": "KG"}
        )
        s = build_confirmation_summary("PROCUREMENT_CREATE_REQUEST", payload)
        assert "FCFA/KG" in s

    def test_procurement_create_request_warns_on_genuine_price_unit_mismatch(self):
        payload = _normalize_quantity_to_kg(
            {"product": "carottes", "quantity": 50, "unit": "KG",
             "price": 5000, "price_unit": "SAC"}
        )
        s = build_confirmation_summary("PROCUREMENT_CREATE_REQUEST", payload)
        assert "⚠️" in s and "SAC" in s


# =====================================================================
# CONVERSION D'UNITÉS — stockage canonique
# =====================================================================

class TestQuantityNormalisation:
    def test_tonnes_converted_to_kg_while_display_keeps_user_words(self):
        p = _normalize_quantity_to_kg({"quantity": 200, "unit": "TONNE"})
        assert p["quantity"] == 200_000.0 and p["unit"] == "KG"
        assert p["unit_display"] == "TONNE" and p["original_quantity"] == 200

    def test_kg_is_left_unchanged(self):
        p = _normalize_quantity_to_kg({"quantity": 50, "unit": "KG"})
        assert p["quantity"] == 50 and p["unit"] == "KG"

    def test_non_mass_units_are_not_converted(self):
        p = _normalize_quantity_to_kg({"quantity": 12, "unit": "TETE"})
        assert p["quantity"] == 12 and p["unit"] == "TETE"

    def test_display_fields_stay_frozen_across_turns_that_dont_touch_quantity(self):
        """Comportement voulu (setdefault, `refresh_display` par défaut à
        False) : un tour qui répond à un AUTRE champ (deadline...) ne doit
        pas remplacer "2 TONNE" affiché par la valeur interne convertie."""
        established = _normalize_quantity_to_kg({"quantity": 2, "unit": "TONNE"})
        assert established["unit_display"] == "TONNE"
        # Le payload MERGE_DICT (transaction_payload) porte déjà ces clés au
        # tour suivant — la fonction ne les touche pas, comme aujourd'hui.
        untouched = _normalize_quantity_to_kg(established)
        assert untouched["unit_display"] == "TONNE"
        assert untouched["original_quantity"] == 2

    def test_refresh_display_updates_the_recap_on_a_genuine_correction(self):
        """Bug réel (2026-09-03, incident PROCUREMENT_CREATE_REQUEST) :
        `quantity_display`/`unit_display` restaient figés sur "2 TONNE" à
        travers PLUSIEURS corrections successives ("non j'ai dit 2 tonnes et
        250 kg" puis "non, plutôt 1 tonne et 125 kg") — le contrat
        d'exécution (`quantity`/`unit`) était bien mis à jour, mais le
        récapitulatif affiché à l'utilisateur ne changeait jamais.
        `refresh_display=True` (passé par `nodes/memory.py` quand
        quantity/unit ont RÉELLEMENT changé ce tour) corrige ça sans changer
        le comportement du cas commun ci-dessus."""
        established = _normalize_quantity_to_kg({"quantity": 2, "unit": "TONNE"})
        assert established["unit_display"] == "TONNE"

        corrected = dict(established)
        corrected["quantity"] = 1
        corrected["unit"] = "TONNE"
        refreshed = _normalize_quantity_to_kg(corrected, refresh_display=True)
        assert refreshed["unit_display"] == "TONNE"
        assert refreshed["original_quantity"] == 1
        assert refreshed["quantity"] == 1000.0  # converti en KG

        # Sans le flag (comportement historique) : reste figé sur 2.
        stale = _normalize_quantity_to_kg(corrected)
        assert stale["original_quantity"] == 2

    @pytest.mark.parametrize("raw,expected", [("kg", "KG"), ("tonnes", "TONNE"), ("", "KG")])
    def test_canonical_unit_label(self, raw, expected):
        assert canonical_unit_label(raw) == expected


# =====================================================================
# ENRICHISSEMENT — le LLM d'abord, la regex comble les trous
# =====================================================================

class TestSlotEnrichment:
    def test_regex_never_overwrites_a_value_from_the_llm(self):
        """Régression : la regex écrasait une quantité correctement extraite."""
        p = run(enrich_payload_from_text(
            {"product": "tomates", "quantity": 50, "unit": "KG"},
            "je veux 8 sacs", "BUYER_ADD_TO_CART", StubRuntime(llm=None),
        ))
        assert p["quantity"] == 50 and p["unit"] == "KG"

    def test_regex_fills_a_genuine_gap(self):
        p = run(enrich_payload_from_text(
            {"product": "tomates"}, "je veux 8 sacs",
            "BUYER_ADD_TO_CART", StubRuntime(llm=None),
        ))
        assert p["quantity"] == 8.0 and p["unit"] == "SAC"

    @pytest.mark.parametrize("text,expected", [
        ("c est une culture", "CROP"),
        ("plutot de l elevage", "LIVESTOCK"),
        ("je ne sais pas", None),
    ])
    def test_production_type_extraction(self, text, expected):
        assert extract_production_type_from_text(text) == expected

    def test_future_date_is_iso_formatted(self):
        d = extract_future_datetime_from_text("dans 3 jours")
        assert d is None or (len(d) >= 10 and d[4] == "-")

    def test_surface_extraction(self):
        v = extract_surface_from_text("2 hectares")
        assert v is None or v > 0


# =====================================================================
# CONTRATS PYDANTIC — défense en profondeur post-LLM
# =====================================================================

class TestContracts:
    def test_valid_publication_passes(self):
        ok, _, _ = enforce_contract("SALES_PUBLISH_PRODUCT", {
            "product": "maïs", "quantity": 50.0, "price": 250.0, "phone": "+22670000000",
        })
        assert ok

    @pytest.mark.parametrize("field,value", [("price", -1), ("quantity", 0)])
    def test_out_of_range_values_are_rejected(self, field, value):
        payload = {"product": "maïs", "quantity": 50.0, "price": 250.0, "phone": "+22670000000"}
        payload[field] = value
        ok, _, bad = enforce_contract("SALES_PUBLISH_PRODUCT", payload)
        assert not ok and bad == field

    def test_absent_field_is_not_a_contract_error(self):
        """La PRÉSENCE est le travail d'INTENT_CONFIG.required, pas des contrats."""
        ok, _, _ = enforce_contract("BUYER_CHECK_ORDER_STATUS", {"phone": "+22670000000"})
        assert ok

    def test_intent_without_contract_always_passes(self):
        ok, _, _ = enforce_contract("GOAL_SANS_CONTRAT", {})
        assert ok


# =====================================================================
# ENVELOPPES MCP — un échec ne doit jamais passer pour un succès
# =====================================================================

class TestMcpEnvelopes:
    @pytest.mark.parametrize("resp", [
        {"status": "success"},
        {"ok": True, "data": {"id": 1}},
    ])
    def test_success_shapes_detected(self, resp):
        assert is_success_response(resp) is True

    @pytest.mark.parametrize("resp", [
        {"status": "error", "message": "boom"},
        {"ok": False, "error": "stock insuffisant"},
    ])
    def test_failure_shapes_detected(self, resp):
        assert is_success_response(resp) is False

    def test_slot_key_normalisation_is_applied(self):
        out = normalize_slot_keys({"produit": "mais", "quantite": 5, "prix": 100})
        assert out["product"] == "mais" and out["quantity"] == 5 and out["price"] == 100
