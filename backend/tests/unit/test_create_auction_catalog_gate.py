"""`AuctionMixin.create_auction` — résolution produit.

**REVERTED (2026-08-15, incident réel en production)** : une version
antérieure de ce test verrouillait un REJET pur quand aucune sous-catégorie
existante ne matchait ("« riz » n'est pas encore disponible..."). En
production, ça a bloqué l'appel d'offres pour "riz" — un produit agricole
parfaitement légitime — parce que `get_public_categories()` (utilisée pour
lister les "vraies catégories" dans le message de rejet) ne renvoie que les
catégories ayant du stock ACTIF : sans producteur ayant du stock à cet
instant, la liste était VIDE et le rejet devenait absolu, même légitime. Un
appel d'offres exprime un BESOIN acheteur, pas un article d'un catalogue
déjà stabilisé — l'auto-provisioning (`_get_or_create_sub_category_for_rfq`)
est restaurée comme dernier recours. Voir
[[precommande-architecture-consolidation-2026-08]] Round 6.

Ce fichier verrouille maintenant :
  1. Aucun candidat du tout -> auto-provisionné (PAS rejeté).
  2. Un candidat existe mais n'est qu'un faux positif trigram (même classe
     que l'incident bœuf/œufs, [[buyer-search-fuzzy-match-safety-2026-08]])
     -> le garde-fou de confiance (toujours en place, lui) l'écarte et
     retombe sur l'auto-provisioning, PAS sur le faux match.
  3. Un candidat confiant -> utilisé tel quel, rien n'est auto-provisionné.
  4. Un terme interdit -> toujours rejeté (défense en profondeur, dans
     `_get_or_create_sub_category_for_rfq`)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agriconnect.services.database.errors import BusinessRuleException
from tests.conftest import run


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return SimpleNamespace(all=lambda: (self._value or []))


class _FakeSession:
    """`scalar_values` est consommée dans l'ORDRE des appels réels de
    `create_auction` : (1) le top-1 SQL du produit, puis, si
    auto-provisioning atteint, (2) la recherche de `Category` existante.
    `subcategories` alimente `_fuzzy_match_sub_category` (second essai, via
    `.execute()`)."""

    def __init__(self, *, scalar_values=None, subcategories=None):
        self._scalar_values = list(scalar_values or [])
        self._subcategories = subcategories or []
        self.added = []

    async def scalar(self, stmt):
        if self._scalar_values:
            return self._scalar_values.pop(0)
        return None

    async def execute(self, stmt):
        return _FakeScalarResult(self._subcategories)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = f"generated-{len(self.added)}"

    async def rollback(self):
        pass


def _service(*, scalar_values=None, subcategories=None, prohibited_terms=None):
    from agriconnect.services.database.auction import AuctionMixin

    class _Svc(AuctionMixin):
        def __init__(self):
            self._session = _FakeSession(scalar_values=scalar_values, subcategories=subcategories)

        @property
        def session(self):
            return self._session

        async def get_buyer_profile(self, phone):
            return SimpleNamespace(zone_id="z1"), SimpleNamespace(id="b1")

        async def get_prohibited_terms(self):
            return {"terms": prohibited_terms or []}

        async def guess_category(self, product_name):
            return "CEREALES"

    return _Svc()


def _sub_category(name):
    return SimpleNamespace(id="sc1", name=name)


async def _create(svc, product_query="riz"):
    return await svc.create_auction(
        phone="+22670000001", product_query=product_query, qty=10, unit="KG",
        max_price=200, deadline="2026-09-01",
        delivery_location="Ouagadougou", delivery_deadline="2026-09-05",
    )


class TestCreateAuctionCatalogResolution:
    def test_no_candidate_at_all_gets_auto_provisioned_not_rejected(self):
        svc = _service(scalar_values=[None, None], subcategories=[])
        result = run(_create(svc))
        assert result["status"] == "success"
        created_names = [obj.name for obj in svc.session.added if hasattr(obj, "name")]
        assert "Riz" in created_names

    def test_a_fuzzy_matched_but_unconfident_candidate_falls_back_to_auto_provisioning(self):
        """Le même garde-fou que l'incident bœuf/œufs : un match trigram
        top-1 (seuil 0.22) sans rapport réel avec le terme cherché ne doit
        pas être accepté silencieusement — mais, contrairement au
        comportement révoqué, ça ne bloque plus l'appel d'offres : il retombe
        sur l'auto-provisioning d'une VRAIE sous-catégorie "Riz"."""
        svc = _service(scalar_values=[_sub_category("Maïs"), None], subcategories=[])
        result = run(_create(svc))
        assert result["status"] == "success"
        assert "Maïs" not in result["message"]
        created_names = [obj.name for obj in svc.session.added if hasattr(obj, "name")]
        assert "Riz" in created_names
        assert "Maïs" not in created_names

    def test_a_confidently_matched_candidate_is_used_as_is(self):
        svc = _service(scalar_values=[_sub_category("Riz")])
        result = run(_create(svc))
        assert result["status"] == "success"
        # Rien n'a dû être auto-provisionné (ni Category ni SubCategory) : le
        # candidat confiant a suffi — seule l'Auction elle-même est ajoutée.
        created_names = [obj.name for obj in svc.session.added if hasattr(obj, "name")]
        assert created_names == []

    def test_the_second_stage_fuzzy_matcher_is_also_confidence_gated(self):
        """Incident réel confirmé (2026-08-16) : un appel d'offres pour
        "champignons" a été enregistré pour *oignons* — `_fuzzy_match_sub_category`
        (rapidfuzz `WRatio`) a scoré 83.1 pour cette paire (bien au-dessus du
        seuil 78), car son `partial_ratio` sous-jacent trouve "ignons" commun
        aux deux mots. Un score `WRatio` élevé ne garantit PAS qu'il s'agit du
        même produit — le garde-fou de confiance (substring) doit s'appliquer
        à CE matcher aussi, pas seulement au premier candidat SQL. Voir
        [[precommande-architecture-consolidation-2026-08]]."""
        from rapidfuzz import fuzz
        assert fuzz.WRatio("champignons", "oignons") >= 78  # précondition du bug

        svc = _service(scalar_values=[None, None], subcategories=[_sub_category("Oignons")])
        result = run(_create(svc, product_query="champignons"))
        assert result["status"] == "success"
        assert "oignons" not in result["message"].lower()
        created_names = [obj.name for obj in svc.session.added if hasattr(obj, "name")]
        assert "Champignons" in created_names
        assert "Oignons" not in created_names

    def test_a_prohibited_term_is_still_rejected(self):
        svc = _service(scalar_values=[None, None], subcategories=[], prohibited_terms=["arme"])
        with pytest.raises(BusinessRuleException) as exc_info:
            run(_create(svc, product_query="arme"))
        assert exc_info.value.reason == "prohibited_product"
