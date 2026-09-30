"""Fast-path safety + product entity preservation (2026-09-30).

Incident réel : `expected=QUANTITY`, product courant="lait", l'utilisateur
répond "J'ai 60 L de miel" — le fast-path numérique
(`_interpret_fast_path::fast_path_slot_numeric_answer`) n'extrayait QUE
`{quantity=60, unit=LITRE}`, ignorant silencieusement "miel" ; `cognitive_guard`
réinjectait ensuite le produit périmé "lait" (`_entity_carry_forward`), et le
système continuait comme si l'utilisateur avait proposé 60 L de LAIT.

Invariant introduit ici (voir `domain/quantity_unit.py::is_pure_numeric_answer`) :
un fast-path ne peut produire un résultat que s'il comprend COMPLÈTEMENT les
éléments métier significatifs du message pour son périmètre — sinon il
s'abstient (`return None`) et laisse la main à l'interprétation complète
(micro-prompt ACTIVE_SLOT), seule capable d'extraire `product` et d'atteindre
la logique de conflit déjà correcte de `nodes/memory.py::_apply_slot`.

Ce garde ne s'applique QUE quand un LLM existe réellement sur le runtime
(`llm_available=True`) : sans LLM, il n'y a nulle part d'autre où renvoyer le
message (voir le repli documenté de `skip_numeric_shortcut` dans
`_interpret_fast_path.__doc__`) — abstenir serait alors strictement pire que
le résultat partiel existant. Les tests `llm_available=False` (déjà couverts
par `tests/interpreter/test_extraction_and_llm_primacy.py`) restent donc
inchangés par construction et ne sont pas dupliqués ici.
"""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import is_pure_numeric_answer
from ladini.graphs.agents.market_coach.interpreter.routing import _interpret_fast_path
from tests.conftest import make_state


def fast(text: str, expected_input: str, *, product: str = "lait"):
    """Reproduit le câblage réel de production pour PRICE/QUANTITY quand un LLM
    est disponible : `skip_numeric_shortcut=True` ET `llm_available=True` (voir
    `interpreter/routing.py`, calcul de `_skip_numeric_shortcut` juste avant
    l'appel à `_interpret_fast_path`)."""
    state = make_state(
        expected_input=expected_input,
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload={"product": product},
        user_role="PRODUCER",
    )
    return _interpret_fast_path(
        state, text, skip_numeric_shortcut=True, llm_available=True
    )


# =====================================================================
# Primitive is_pure_numeric_answer — testée isolément
# =====================================================================


class TestIsPureNumericAnswer:
    @pytest.mark.parametrize(
        "text",
        [
            "60",
            "60 L",
            "60 litres",
            "500",
            "500 FCFA",
            "500 par litre",
            "60kg",
            "500fcfa",
            "1 000 kg",
        ],
    )
    def test_pure_shapes(self, text):
        assert is_pure_numeric_answer(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "60 L de miel",
            "j ai 60 L de miel",
            "finalement 60 L de miel",
            "finalement 60 L",
            "non, 60 L de miel",
            "non plutôt 60 L",
            "60 L mais de miel",
            "500 pour le miel",
            "le miel à 500",
            "500 mais pour le lait",
            "le miel coûte 500",
        ],
    )
    def test_impure_shapes(self, text):
        assert is_pure_numeric_answer(text) is False


# =====================================================================
# QUANTITY — cas §3 du mandat
# =====================================================================


class TestQuantityFastPathAbstention:
    def test_bare_quantity_still_uses_fast_path(self):
        """Cas A : « 60 L » -> fast-path autorisé, product inchangé (carry-forward
        normal, hors périmètre de ce fast-path)."""
        r = fast("60 L", "QUANTITY")
        assert r is not None
        assert r["interpreted_event"] == "ANSWER"
        assert r["extracted_entities"]["quantity"] == 60.0
        assert r["extracted_entities"]["unit"] == "LITRE"
        assert "product" not in r["extracted_entities"]

    def test_quantity_with_explicit_different_product_abstains(self):
        """Cas B : « 60 L de miel » (product courant=lait) -> ABSTAIN, jamais de
        résultat partiel {quantity, unit} qui laisserait courir "lait"."""
        assert fast("60 L de miel", "QUANTITY") is None

    def test_deviation_marker_with_product_abstains(self):
        """Cas C : « j ai 60 L de miel » -> ABSTAIN."""
        assert fast("j ai 60 L de miel", "QUANTITY") is None

    def test_correction_marker_with_product_abstains(self):
        """Cas D : « finalement 60 L de miel » -> ABSTAIN."""
        assert fast("finalement 60 L de miel", "QUANTITY") is None

    def test_negation_marker_with_product_abstains(self):
        """Cas E (numérotation mandat : « non, 60 L de miel ») -> ABSTAIN."""
        assert fast("non, 60 L de miel", "QUANTITY") is None

    def test_bare_quantity_alternate_unit_spelling_still_fast(self):
        """Cas F : « 60 litres » -> fast-path toujours autorisé."""
        r = fast("60 litres", "QUANTITY")
        assert r is not None
        assert r["extracted_entities"]["quantity"] == 60.0
        assert r["extracted_entities"]["unit"] == "LITRE"


# =====================================================================
# PRICE — cas §4 du mandat
# =====================================================================


class TestPriceFastPathAbstention:
    def test_bare_price_number_without_unit_defers_to_llm_when_available(self):
        """« 500 » seul (aucune unité/devise voisine) est déjà, avant ce
        correctif, un cas `_unambiguous_single is None` : `skip_numeric_shortcut`
        (LLM disponible) vide `candidates` et défère au LLM — comportement
        PRÉ-EXISTANT, non lié à `is_pure_numeric_answer` (voir
        `tests/interpreter/test_extraction_and_llm_primacy.py::
        test_ambiguous_bare_number_is_deferred_to_llm` pour le même cas via
        le mot "environ"). Ce test documente que ce chemin reste inchangé
        par ce correctif, pas qu'il en découle."""
        assert fast("500", "PRICE") is None

    def test_bare_price_with_currency_still_fast(self):
        r = fast("500 FCFA", "PRICE")
        assert r is not None
        assert r["extracted_entities"]["price"] == 500.0

    def test_price_with_explicit_product_abstains(self):
        assert fast("500 FCFA pour le miel", "PRICE") is None

    def test_price_stated_as_product_verb_abstains(self):
        assert fast("le miel à 500", "PRICE") is None


# =====================================================================
# §12 — test d'invariant explicite
# =====================================================================


def test_invariant_explicit_product_never_silently_kept_from_memory():
    """`explicit_product_in_current_message AND explicit_product != memory_product
    => le fast-path NE PEUT PAS produire un résultat qui laisse le produit mémoire
    (« lait ») intact sans jamais avoir vu « miel »` — le fast-path doit
    s'abstenir, quel que soit le slot attendu (QUANTITY ici, PRICE couvert par
    la classe ci-dessus)."""
    memory_product = "lait"
    message = "J ai 60 L de miel"
    result = fast(message, "QUANTITY", product=memory_product)
    assert result is None, (
        "le fast-path ne doit jamais produire de résultat partiel quand le "
        "message porte un produit explicite différent du produit mémoire — "
        "il doit s'abstenir et laisser l'interprétation complète (et "
        "`nodes/memory.py::_apply_slot`) trancher le conflit"
    )
