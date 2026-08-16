"""`flows/buyer/helpers.py::additional_products_hint` — quand un message
mentionne plusieurs produits ("œufs et laitue"), l'interprète ne met que le
PREMIER dans `product` (jamais fusionné en une chaîne — voir
`interpreter/routing.py` et [[buyer-search-fuzzy-match-safety-2026-08]]) et
liste le reste dans `additional_products`. Ce module vérifie que
l'utilisateur est informé de ce qui a été mis de côté, plutôt que de le voir
disparaître silencieusement."""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.flows.buyer.helpers import additional_products_hint


class TestAdditionalProductsHint:
    def test_no_hint_when_nothing_additional_was_mentioned(self):
        assert additional_products_hint({}, {}) == ""

    def test_no_hint_for_an_empty_list(self):
        assert additional_products_hint({"additional_products": []}, {}) == ""

    def test_hint_names_every_additional_product_from_the_payload(self):
        hint = additional_products_hint({"additional_products": ["laitue"]}, {})
        assert "laitue" in hint

    def test_hint_falls_back_to_extracted_entities_when_absent_from_payload(self):
        state = {"extracted_entities": {"additional_products": ["riz", "maïs"]}}
        hint = additional_products_hint({}, state)
        assert "riz" in hint
        assert "maïs" in hint

    def test_a_non_list_value_is_ignored_defensively(self):
        assert additional_products_hint({"additional_products": "not-a-list"}, {}) == ""
