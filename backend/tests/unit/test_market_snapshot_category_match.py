"""`services/database/category.py::_is_confident_category_match` — même
garde-fou que `cart_service.py::_is_confident_product_match` (voir
[[buyer-search-fuzzy-match-safety-2026-08]]), dupliqué côté DB pour
`get_market_snapshot` : un produit hors catalogue (ex: "riz") ne doit
jamais fuzzy-matcher une sous-catégorie sans rapport et faire croire à un
prix pour un tout autre produit. Voir
[[precommande-architecture-consolidation-2026-08]]."""
from __future__ import annotations

from ladini.services.database.category import _is_confident_category_match


class TestIsConfidentCategoryMatch:
    def test_exact_match_is_confident(self):
        assert _is_confident_category_match("tomate", "Tomate") is True

    def test_the_search_term_being_a_substring_of_the_match_is_confident(self):
        assert _is_confident_category_match("oignon", "Oignons") is True

    def test_accents_and_the_oe_ligature_do_not_affect_the_comparison(self):
        assert _is_confident_category_match("mais", "Maïs") is True
        assert _is_confident_category_match("oeufs", "Œufs") is True

    def test_an_unrelated_word_that_only_fuzzy_matched_is_not_confident(self):
        # "riz" n'existe dans aucune sous-catégorie du catalogue : un
        # candidat fuzzy-matché au hasard ne doit jamais passer.
        assert _is_confident_category_match("riz", "Maïs") is False

    def test_empty_inputs_are_never_confident(self):
        assert _is_confident_category_match("", "Tomate") is False
        assert _is_confident_category_match("tomate", "") is False
        assert _is_confident_category_match(None, None) is False
