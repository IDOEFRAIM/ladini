"""DETTE EXPLICITE — shortlist désactivée pour 6 fichiers historiques seulement.

Production : `BUYER_SHORTLIST_SIZE=5` (5 offres montrées, le reste via « montre les autres »). Ces fichiers (écrits avant la shortlist) bâtissent
leurs scénarios sur un menu producteur COMPLET de 6-7 offres numérotées et sélectionnent l'offre n°6/7. Les migrer = réécrire leurs scénarios en
« shortlist + show-more » (travail dédié, sans changement de comportement produit). Tous les AUTRES tests d'intégration tournent avec le défaut de
production. Ne PAS ajouter de fichier à cette liste : écrire le test avec la shortlist.
"""
from __future__ import annotations

import pytest

from ladini.core.settings import settings

_FULL_MENU_LEGACY_MODULES = frozenset({
    "test_buyer_cart_ready_confirmation_priority_e2e",
    "test_buyer_direct_purchase_flow",
    "test_buyer_numeric_command_collision_e2e",
    "test_buyer_pricing_tier_protocol_e2e",
    "test_buyer_product_switch_during_vendor_selection_e2e",
    "test_buyer_same_product_continuation_e2e",
})


@pytest.fixture(autouse=True)
def _full_producer_menu_for_legacy_modules(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in _FULL_MENU_LEGACY_MODULES:
        monkeypatch.setattr(settings, "BUYER_SHORTLIST_SIZE", 0, raising=False)
