"""`services/domain/cart_service.py::_is_confident_product_match` — filet de
sécurité contre les faux positifs de la recherche floue trigram.

Incident réel (2026-08-14) : un acheteur a demandé "une douzaine d'œufs à
500 FCFA" et le système a réservé 10 TÊTES DE BŒUF à 486 000 FCFA/unité
(~4.86M FCFA), parce que la recherche floue (`services/database/search.py`,
seuil 0.22, volontairement bas pour tolérer les fautes de frappe comme
"tomte"→"tomate") a fait correspondre "oeufs" à "Bœuf" — deux mots courts
partageant assez de trigrammes pour franchir le seuil, sans être le même
produit. Voir [[buyer-search-fuzzy-match-safety-2026-08]]."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.services.domain.cart_service import (
    _is_confident_product_match,
)


class TestIsConfidentProductMatch:
    def test_the_real_incident_is_flagged_as_not_confident(self):
        assert _is_confident_product_match("oeufs", "Bœuf") is False
        assert _is_confident_product_match("laitue, oeufs", "Bœuf") is False

    def test_exact_match_is_confident(self):
        assert _is_confident_product_match("tomate", "Tomate") is True

    def test_the_search_term_being_a_substring_of_the_match_is_confident(self):
        assert _is_confident_product_match("oignon", "Oignons") is True

    def test_accents_and_the_oe_ligature_do_not_affect_the_comparison(self):
        assert _is_confident_product_match("mais", "Maïs") is True
        assert _is_confident_product_match("oeufs", "Œufs") is True

    def test_a_genuine_typo_correction_is_treated_as_uncertain_too(self):
        """Compromis assumé (choix produit 2026-08-14, "garder tolérant mais
        avertir") : même "tomte" -> "tomate" n'est pas un sous-texte l'un de
        l'autre, donc demande une confirmation au lieu d'être appliqué en
        silence. Le seuil de similarité reste inchangé (toujours tolérant
        aux fautes de frappe) — seule la confiance AFFICHÉE change."""
        assert _is_confident_product_match("tomte", "Tomate") is False

    def test_empty_inputs_are_never_confident(self):
        assert _is_confident_product_match("", "Tomate") is False
        assert _is_confident_product_match("tomate", "") is False
        assert _is_confident_product_match(None, None) is False
