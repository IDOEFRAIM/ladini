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

(2026-09-30, durcissement post-revue) — CETTE abstention est désormais
INDÉPENDANTE de `llm_available` : la sûreté métier (ne jamais attacher
silencieusement une quantité/un prix au mauvais produit) ne peut pas dépendre
d'un provider LLM disponible, d'un timeout ou d'un mode dégradé — sinon la
même panne réseau qui rend le LLM indisponible réintroduirait exactement le
bug corrigé plus haut. `llm_available` ne change QUE ce qui se passe APRÈS
l'abstention :
  - LLM disponible   -> interprétation complète (micro-prompt ACTIVE_SLOT) ;
  - LLM indisponible -> repli SANS MUTATION déjà existant dans
    `_input_interpreter_impl` (`interpreted_event="UNKNOWN"`, aucune entité
    extraite — voir le warning "No LLM on runtime"), jamais un second
    mécanisme de clarification créé pour l'occasion.
Voir `TestQuantityFastPathAbstentionWithoutLLM`/`TestPriceFastPathAbstentionWithoutLLM`
ci-dessous pour les mêmes scénarios rejoués avec `llm_available=False`.
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


def fast_no_llm(text: str, expected_input: str, *, product: str = "lait"):
    """Même scénario que `fast()`, mais LLM RÉELLEMENT indisponible sur ce
    runtime (`llm_available=False`) — reproduit le câblage réel de production
    dans ce cas (`_skip_numeric_shortcut = expected_input in ("PRICE",
    "QUANTITY") and llm is not None` -> `False` quand `llm is None`)."""
    state = make_state(
        expected_input=expected_input,
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload={"product": product},
        user_role="PRODUCER",
    )
    return _interpret_fast_path(
        state, text, skip_numeric_shortcut=False, llm_available=False
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
            # (2026-09-30, revue) : un simple marqueur de correction/hedging
            # SEUL (sans nom de produit ni autre mot de fond) ne bloque pas le
            # fast-path — voir "non, 500 kg" et "je veux 60 litre",
            # tests/architecture/test_fastpath_pipeline_safety.py et
            # tests/integration/test_tier_selection_full_node_chain.py,
            # cassés par une première version trop stricte de ce garde.
            "finalement 60 L",
            "non plutôt 60 L",
            "non, 500 kg",
            "je veux 60 litre",
            "environ 300",
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
            "non, 60 L de miel",
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


# =====================================================================
# Mandat 2026-09-30 (revue) — la sûreté ne dépend PAS de `llm_available`
# =====================================================================
# Cas A-D (QUANTITY) et E-G (PRICE) du mandat, rejoués avec `llm_available=
# False` : le trou signalé après la première revue ("le garde ne s'applique
# que si llm_available=True") est fermé — voir `fast_no_llm()` et le retrait
# de `llm_available and`/`not llm_available or` dans `interpreter/routing.py`.


class TestQuantityFastPathAbstentionWithoutLLM:
    def test_a_bare_quantity_still_fast_without_llm(self):
        """Cas A : « 60 L », LLM indisponible -> fast-path OK (comportement
        existant conservé, voir aussi `test_extraction_and_llm_primacy.py::
        test_ambiguous_bare_number_resolved_when_llm_unavailable` pour le
        principe général du repli sans LLM)."""
        r = fast_no_llm("60 L", "QUANTITY")
        assert r is not None
        assert r["interpreted_event"] == "ANSWER"
        assert r["extracted_entities"]["quantity"] == 60.0
        assert r["extracted_entities"]["unit"] == "LITRE"

    def test_b_explicit_product_abstains_without_llm(self):
        """Cas B : « 60 L de miel », LLM indisponible -> ABSTAIN. Jamais
        quantity=60 attachée à "lait", jamais de draft, jamais de progression
        silencieuse vers PRICE comme si la quantité était résolue."""
        assert fast_no_llm("60 L de miel", "QUANTITY") is None

    def test_c_deviation_marker_with_product_abstains_without_llm(self):
        """Cas C : « j ai 60 L de miel », LLM indisponible -> ABSTAIN."""
        assert fast_no_llm("j ai 60 L de miel", "QUANTITY") is None

    def test_d_correction_marker_with_product_abstains_without_llm(self):
        """Cas D : « finalement 60 L de miel », LLM indisponible -> ABSTAIN."""
        assert fast_no_llm("finalement 60 L de miel", "QUANTITY") is None


class TestPriceFastPathAbstentionWithoutLLM:
    def test_e_bare_price_still_fast_without_llm(self):
        """Cas E : « 500 », LLM indisponible -> fast-path OK (comportement
        existant conservé — nombre nu, aucun mot hors périmètre)."""
        r = fast_no_llm("500", "PRICE")
        assert r is not None
        assert r["extracted_entities"]["price"] == 500.0

    def test_f_explicit_product_abstains_without_llm(self):
        """Cas F : « 500 FCFA pour le miel », LLM indisponible -> ABSTAIN. Pas
        d'application du prix à "lait", pas de progression silencieuse."""
        assert fast_no_llm("500 FCFA pour le miel", "PRICE") is None

    def test_g_product_stated_as_verb_abstains_without_llm(self):
        """Cas G : « le miel à 500 », LLM indisponible -> ABSTAIN."""
        assert fast_no_llm("le miel à 500", "PRICE") is None


# =====================================================================
# §7 du mandat — tests d'invariant explicites (sûreté indépendante du LLM)
# =====================================================================


def test_invariant_non_pure_message_never_uses_lossy_fast_path_without_llm():
    """`non_pure_business_message AND llm_available=False =>
    must_not_use_lossy_numeric_fast_path` — un message impur ne doit jamais
    produire de résultat partiel, que le LLM soit disponible ou non."""
    assert fast_no_llm("60 L de miel", "QUANTITY") is None
    assert fast_no_llm("500 FCFA pour le miel", "PRICE") is None


def test_invariant_explicit_product_never_silently_wins_without_llm():
    """`explicit_product_in_message AND explicit_product != memory_product AND
    llm_unavailable => memory_product must not silently win` — le pendant,
    sans LLM, de `test_invariant_explicit_product_never_silently_kept_from_memory`
    ci-dessus."""
    memory_product = "lait"
    result = fast_no_llm("J ai 60 L de miel", "QUANTITY", product=memory_product)
    assert result is None, (
        "sans LLM non plus, le fast-path ne doit jamais laisser 'lait' "
        "gagner silencieusement face à un 'miel' explicite dans le message"
    )
