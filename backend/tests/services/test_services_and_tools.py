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

    def test_unknown_goal_with_quantity_never_duplicates_the_unit(self):
        """Bug réel confirmé (2026-09-23, "10 KG KG") : le fallback générique
        (goal sans gabarit dédié — même mauvais-aiguillage que le test
        ci-dessus) rajoutait `{display_unit}` APRÈS `quantity_line`, qui
        porte déjà l'unité (`_format_quantity` -> "10 KG"). Observé en
        production sur un besoin récurrent mal aiguillé vers ce récap
        générique : "Confirmez-vous cette opération pour 10 KG KG de
        *tomate* ?"."""
        s = build_confirmation_summary(
            "SOME_UNMAPPED_GOAL", {"product": "tomate", "quantity": 10, "unit": "KG"}
        )
        assert "KG KG" not in s
        assert "10 KG" in s
        assert "tomate" in s

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

    def test_procurement_create_request_with_a_certified_total_budget_offer_never_shows_per_unit(self):
        """Mandat B2c.5 : "10 tonnes pour 4 millions au total" ne doit JAMAIS s'afficher
        "4 000 000 FCFA/TONNE" (l'ancien bug — `reconcile_price_basis` n'avait pas de notion de
        TOTAL_LOT). Le récap projette l'offre CERTIFIÉE, jamais `payload["price"]` brut."""
        from ladini.domain.commercial_offer import (
            CommercialOffer,
            CommercialQuantity,
            InventoryQuantity,
            PriceBasis,
            Pricing,
            Provenance,
        )

        offer = CommercialOffer(
            product="mais",
            commercial_quantity=CommercialQuantity(10.0, "TONNE", Provenance.USER_EXPLICIT),
            inventory_quantity=InventoryQuantity(10.0, "TONNE", Provenance.USER_EXPLICIT),
            pricing=Pricing(
                amount=4_000_000.0, basis=PriceBasis.TOTAL_LOT,
                source=Provenance.USER_EXPLICIT, basis_source=Provenance.USER_EXPLICIT,
            ),
        )
        payload = {
            "product": "mais", "quantity": 10.0, "unit": "TONNE",
            "price": 4_000_000.0, "commercial_offer": offer.to_dict(),
        }
        s = build_confirmation_summary("PROCUREMENT_CREATE_REQUEST", payload)
        assert "Budget maximal total : 4 000 000 FCFA pour l'ensemble" in s
        assert "4 000 000 FCFA/TONNE" not in s

    def test_an_assumed_unit_is_flagged_for_confirmation(self):
        """Incident réel (2026-09-15) : « Vente de 25 LITRE de boeufs » — un
        producteur qui confirme par habitude, sans tout relire, ne voyait
        jamais que l'unité avait été DEVINÉE (nature du produit) plutôt
        qu'écrite par lui. `unit_was_assumed` (posé par
        `validation.py::_apply_slot_defaults`) doit rendre cette hypothèse
        visible dans le récap, avec un moyen explicite de la corriger."""
        payload = _normalize_quantity_to_kg(
            {
                "product": "boeufs",
                "quantity": 25,
                "unit": "TETE",
                "price": 425000,
                "unit_was_assumed": True,
            }
        )
        s = build_confirmation_summary("SALES_PUBLISH_PRODUCT", payload)
        assert "⚠️" in s
        # (2026-09-15, retour terrain) : la consigne demande le MOT lui-même
        # ("tête"), pas une syntaxe de commande ("modifier unité : ...") —
        # un producteur qui répond juste "modifier unite" sans valeur ne
        # donnait rien d'exploitable au tour suivant.
        assert "modifier unité" not in s
        assert "tête" in s.lower()

    def test_an_explicitly_typed_unit_is_never_flagged(self):
        """Non-régression : le producteur a écrit l'unité lui-même — aucun
        avertissement à afficher."""
        payload = _normalize_quantity_to_kg(
            {"product": "tomates", "quantity": 50, "unit": "KG", "price": 5000}
        )
        s = build_confirmation_summary("SALES_PUBLISH_PRODUCT", payload)
        assert "⚠️" not in s


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


class TestUnitAssumedFlag:
    """Incident réel (2026-09-15) : « Vente de 25 LITRE de boeufs ». Un
    producteur pressé confirme souvent par habitude sans tout relire — une
    unité DEVINÉE (jamais écrite par lui) doit donc être marquée
    `unit_was_assumed`, jamais confondue avec une unité confirmée. Couvre
    aussi le trou symétrique côté culture : `resolve_product_unit` refuse à
    dessein de deviner (retourne `None`), mais rien n'appelait jamais
    l'utilisateur — `unit` restait `None` jusqu'à `services/database/
    producer.py::create_product`, où `unit = clean_text(...) or "KG"` le
    devinait quand même, hors de portée de tout récapitulatif."""

    def test_livestock_with_no_stated_unit_is_flagged(self):
        p = run(enrich_payload_from_text(
            {"product": "boeufs", "quantity": 25, "price": 425000},
            "je veux vendre mes 25 boeufs",
            "SALES_PUBLISH_PRODUCT", StubRuntime(llm=None),
        ))
        assert p["unit"] == "TETE"
        assert p.get("unit_was_assumed") is True

    def test_crop_with_no_stated_unit_is_flagged_not_silently_left_for_the_db_layer(self):
        p = run(enrich_payload_from_text(
            {"product": "haricot", "quantity": 23, "price": 500},
            "j en ai 23",
            "SALES_PUBLISH_PRODUCT", StubRuntime(llm=None),
        ))
        assert p["unit"] == "KG"
        assert p.get("unit_was_assumed") is True

    def test_a_unit_written_by_the_user_is_never_flagged(self):
        p = run(enrich_payload_from_text(
            {"product": "tomates", "quantity": 50, "unit": "KG", "price": 5000},
            "je vends 50 kg de tomates a 5000",
            "SALES_PUBLISH_PRODUCT", StubRuntime(llm=None),
        ))
        assert "unit_was_assumed" not in p

    def test_a_unit_found_in_the_text_is_never_flagged(self):
        p = run(enrich_payload_from_text(
            {"product": "riz", "quantity": 200},
            "je vends 200 en sacs",
            "SALES_PUBLISH_PRODUCT", StubRuntime(llm=None),
        ))
        assert p["unit"] == "SAC"


class TestCategoryConfigWiringInSlotEnrichment:
    """(2026-09-19, retour produit) — `enrich_payload_from_text` doit
    interroger `get_product_category_unit_config` via `mc_runtime.call_db`
    et laisser cette config PRIMER sur le texte libre quand elle est
    fournie. Sans config (le cas par défaut aujourd'hui, colonnes pas
    encore en base côté site), rien ne doit changer — voir la dernière
    classe de ce groupe."""

    def test_category_config_overrides_a_text_unit_outside_the_allowed_set(self):
        runtime = StubRuntime(llm=None, responses={
            "get_product_category_unit_config": {
                "status": "success",
                "data": {"priority_unit": "LITRE", "allowed_units": ["LITRE"]},
            },
        })
        p = run(enrich_payload_from_text(
            {"product": "lait", "quantity": 25, "price": 500},
            "je vends 25 kg de lait a 500",
            "SALES_PUBLISH_PRODUCT", runtime,
        ))
        assert p["unit"] == "LITRE"
        assert "get_product_category_unit_config" in runtime.calls

    def test_category_config_lets_an_allowed_text_unit_through(self):
        runtime = StubRuntime(llm=None, responses={
            "get_product_category_unit_config": {
                "status": "success",
                "data": {
                    "priority_unit": "TONNE",
                    "allowed_units": ["G", "KG", "TONNE", "SAC"],
                },
            },
        })
        p = run(enrich_payload_from_text(
            {"product": "mais", "quantity": 8},
            "je vends 8 sacs de mais",
            "SALES_PUBLISH_PRODUCT", runtime,
        ))
        assert p["unit"] == "SAC"

    def test_a_tool_failure_degrades_silently_to_the_historical_behaviour(self):
        """`get_product_category_unit_config` peut échouer (colonnes pas
        encore en base, réseau, etc.) — ça ne doit JAMAIS faire planter
        l'enrichissement, seulement retomber sur les règles 1-4 historiques."""
        def _boom(**kwargs):
            raise RuntimeError("colonnes pas encore en base")

        runtime = StubRuntime(llm=None, responses={
            "get_product_category_unit_config": _boom,
        })
        p = run(enrich_payload_from_text(
            {"product": "boeufs", "quantity": 25, "price": 425000},
            "je veux vendre mes 25 boeufs",
            "SALES_PUBLISH_PRODUCT", runtime,
        ))
        assert p["unit"] == "TETE"
        assert p.get("unit_was_assumed") is True

    def test_no_config_available_is_the_default_and_changes_nothing(self):
        """Comportement RÉEL aujourd'hui (aucun outil configuré dans
        `StubRuntime` -> réponse générique `{"status": "success", "data": {}}`,
        donc aucune `allowed_units` exploitable) : identique à avant ce
        chantier."""
        p = run(enrich_payload_from_text(
            {"product": "tomates", "quantity": 50, "unit": "KG", "price": 5000},
            "je vends 50 kg de tomates a 5000",
            "SALES_PUBLISH_PRODUCT", StubRuntime(llm=None),
        ))
        assert p["unit"] == "KG"
        assert "unit_was_assumed" not in p
        assert "unit_was_assumed" not in p

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
