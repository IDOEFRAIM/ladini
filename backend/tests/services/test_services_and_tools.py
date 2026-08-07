"""Services métier + couche d'exécution d'outils (MCP).

Couvre : rendu du récapitulatif de confirmation, enrichissement de slots,
contrats Pydantic, déballage d'enveloppes MCP, résolution d'arguments.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary,
)
from agriconnect.graphs.agents.market_coach.services.domain.slot_enrichment import (
    enrich_payload_from_text,
    extract_production_type_from_text,
    extract_future_datetime_from_text,
    extract_surface_from_text,
)
from agriconnect.graphs.agents.market_coach.interpreter.contracts import enforce_contract
from agriconnect.graphs.agents.market_coach.utils import (
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
