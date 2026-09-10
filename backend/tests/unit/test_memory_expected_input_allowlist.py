"""`nodes/memory.py::_EXPECTED_INPUT_ALLOWED_FIELDS` — audit exhaustif Bloc 2,
Blocker D (2026-09-09).

Protège contre : un LLM en repli dégradé (voir
`services/domain/slot_enrichment.py`/`raw_analysis.degraded_model`) qui
hallucine des champs hors-sujet pour TOUT le schéma JSON même quand une
seule info a été donnée pendant la collecte d'un slot précis — le
garde-fou dans `memory_update` ne doit laisser passer QUE les champs
légitimement associés au slot `expected_input` en cours.

Deux bugs réels trouvés et corrigés par cet audit (la table était recopiée à
la main et avait dérivé de `core/slots.py`) : `production_type` (slot
PRODUCT) et `surface` (slot QUANTITY) étaient absents de l'allowlist alors
qu'ils sont le champ EXACTEMENT demandé pour `DECLARE_CROP_CYCLE`/
`CROP_START_CYCLE` — une réponse dégradée à sa propre question se faisait
donc jeter par son propre garde-fou. La table est désormais dérivée de
`core/slots.py::fields_for_expected_input` (source unique)."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from ladini.graphs.agents.market_coach.nodes.memory import (
    _EXPECTED_INPUT_ALLOWED_FIELDS,
    memory_update,
)
from tests.conftest import StubRuntime, make_state, run


def _enter_field_pending(field: str) -> Dict[str, Any]:
    return {
        "kind": "ENTER_FIELD",
        "goal": None,
        "field": field,
        "context_ref": None,
        "candidates": [],
        "created_at": 0.0,
        "status": "ACTIVE",
        "target": None,
    }


# expected_input category -> (field currently being asked, a legitimate
# companion value to inject, a parasite field the degraded model should
# never be allowed to plant here).
_CASES = [
    ("PRODUCT", "product", "product", "maïs", "quantity", 9999),
    ("PRODUCT", "production_type", "production_type", "CROP", "price", 123456),
    ("QUANTITY", "quantity", "quantity", 500, "product", "produit_parasite"),
    ("QUANTITY", "surface", "surface", 2.5, "price", 999999),
    ("PRICE", "price", "price", 600, "product", "maïs_hallucine"),
    ("UNIT", "unit", "unit", "SAC", "price", 42),
    ("LOCATION", "zone", "zone", "Bobo-Dioulasso", "quantity", 42),
    ("DATE", "estimated_available_at", "estimated_available_at", "2026-12-01", "price", 1),
    ("DATE", "deadline", "deadline", "2026-10-30", "quantity", 1),
    ("FARM_NAME", "farm_name", "farm_name", "Ferme du Soleil", "price", 1),
    ("MOVEMENT_TYPE", "movement_type", "movement_type", "IN", "price", 1),
]


class TestAllowlistNeverRegressesBelowTheOriginalHandWrittenTable:
    """(2026-09-09, régression réelle trouvée en production LE JOUR MÊME du
    déploiement du Blocker D) : `unit` avait été retiré par erreur de
    QUANTITY en passant à la dérivation automatique — `core/slots.py`
    classe `unit` dans sa PROPRE catégorie ("UNIT"), une question DIFFÉRENTE
    de "quels champs accompagnent légitimement une réponse QUANTITY" (une
    réponse quantité porte quasi-toujours son unité : "600 L", "50 kg").
    Incident réel : "j'ai 600 l..." → `unit='LITRE'` jeté comme "hors-sujet"
    en repli dégradé, sur un producteur en train de PUBLIER un produit.

    Ce test verrouille l'invariant qui aurait attrapé cette régression avant
    déploiement : la nouvelle table (dérivée) doit rester un SUR-ensemble de
    l'ancienne table (recopiée à la main), jamais un sous-ensemble sur
    aucune catégorie."""

    _ORIGINAL_HAND_WRITTEN_TABLE = {
        "PRODUCT": {"product", "product_id"},
        "QUANTITY": {
            "quantity", "unit", "pricing_tiers", "agent_action",
            "action_producer_id", "action_pricing_tier_id",
            "action_package_count", "action_quantity", "action_unit",
        },
        "PRICE": {"price", "price_unit", "pricing_tiers"},
        "UNIT": {"unit"},
        "LOCATION": {"zone"},
        "DATE": {"estimated_available_at", "expected_harvest_date"},
        "FARM_NAME": {"farm_name"},
        "MOVEMENT_TYPE": {"movement_type"},
    }

    @pytest.mark.parametrize(
        "category,original_fields", list(_ORIGINAL_HAND_WRITTEN_TABLE.items())
    )
    def test_current_table_is_a_superset_of_the_original(self, category, original_fields):
        current = _EXPECTED_INPUT_ALLOWED_FIELDS.get(category, frozenset())
        missing = original_fields - current
        assert not missing, (
            f"{category} a perdu {missing} par rapport à la table d'origine "
            "— régression de la dérivation automatique"
        )

    def test_quantity_answer_with_its_unit_is_never_dropped_in_degraded_mode(self):
        """Reproduction directe de l'incident réel."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            normalized_text="j ai 600 l",
            pending_interaction=_enter_field_pending("quantity"),
            transaction_payload={"product": "lait"},
            extracted_entities={"quantity": 600, "unit": "LITRE"},
            raw_analysis={"degraded_model": True},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("quantity") == 600
        assert payload.get("unit") is not None, (
            "l'unité accompagnant une réponse quantité ne doit jamais être "
            "jetée comme hors-sujet"
        )


class TestAllowlistCoversTheExactSlotBeingAsked:
    @pytest.mark.parametrize(
        "category,field_asked,legit_field,legit_value,parasite_field,parasite_value",
        _CASES,
    )
    def test_legit_field_survives_parasite_field_is_dropped(
        self, category, field_asked, legit_field, legit_value, parasite_field, parasite_value
    ):
        assert legit_field in _EXPECTED_INPUT_ALLOWED_FIELDS[category], (
            f"{legit_field} doit être autorisé pour expected_input={category} "
            "(c'est le champ exactement demandé ou un compagnon légitime)"
        )
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            normalized_text="reponse degradee",
            pending_interaction=_enter_field_pending(field_asked),
            extracted_entities={legit_field: legit_value, parasite_field: parasite_value},
            raw_analysis={"degraded_model": True},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get(legit_field) == legit_value
        assert parasite_field not in payload or payload.get(parasite_field) != parasite_value


class TestPriceAnswerWithHallucinatedProductProtection:
    """L'exemple explicite du mandat : pending=PRICE, LLM dégradé hallucine
    aussi `product` — le produit déjà établi ne doit JAMAIS être remplacé."""

    def test_hallucinated_product_never_overrides_the_established_one(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            normalized_text="600 fcfa",
            pending_interaction=_enter_field_pending("price"),
            transaction_payload={"product": "tomate"},
            extracted_entities={"price": 600, "product": "maïs"},
            raw_analysis={"degraded_model": True},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("price") == 600
        assert payload.get("product") == "tomate", (
            "un `product` halluciné par le modèle dégradé pendant une "
            "réponse PRICE ne doit jamais écraser le produit déjà établi"
        )


class TestAllowlistOnlyAppliesToTheDegradedModelFallback:
    """Non-régression : le garde-fou est un filet de sécurité pour le SEUL
    modèle dégradé (voir `raw_analysis.degraded_model`) — le modèle
    principal, lui, a le droit d'extraire plusieurs champs à la fois
    (comportement voulu, documenté dans slot_enrichment.py)."""

    def test_primary_model_extraction_is_never_narrowed(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            normalized_text="600 fcfa et au fait c'est 500 kg",
            pending_interaction=_enter_field_pending("price"),
            extracted_entities={"price": 600, "quantity": 500},
            raw_analysis={"degraded_model": False},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("price") == 600
        assert payload.get("quantity") == 500, (
            "le modèle PRINCIPAL a le droit de remplir plusieurs champs à la "
            "fois même hors de l'allowlist du slot en cours — seul le repli "
            "dégradé est restreint"
        )


class TestDegradedModelCorrectionBoundaryIsADocumentedTradeoff:
    """(Mandat §27) Une VRAIE correction ("non c'est du maïs finalement")
    arrivant pendant le repli dégradé et ciblant un champ hors de
    l'allowlist du slot en cours est ELLE AUSSI filtrée — `memory_update` ne
    doit PAS deviner qu'il s'agit d'une correction pour contourner la
    policy (ce serait recréer exactement le risque de duplication de
    décision que l'audit Bloc 1/2 élimine ailleurs). Classifier
    correctement ce tour (NEW_TASK/correction plutôt que ANSWER simple)
    est la responsabilité d'`input_interpreter`/`cognitive_guard` (Bloc 1,
    gelé) — documenté ici comme limite connue, pas contourné localement."""

    def test_an_update_event_targeting_an_out_of_scope_field_is_still_dropped_while_degraded(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UPDATE",
            normalized_text="non finalement c'est du maïs",
            pending_interaction=_enter_field_pending("price"),
            transaction_payload={"product": "tomate"},
            extracted_entities={"product": "maïs"},
            raw_analysis={"degraded_model": True},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("product") == "tomate", (
            "comportement ACTUEL documenté, pas un bug de cet audit : tant "
            "que Bloc 1 classe ce tour comme UPDATE sur le slot PRICE "
            "plutôt que comme un changement de produit, `memory_update` ne "
            "doit pas essayer de deviner la correction lui-même"
        )
