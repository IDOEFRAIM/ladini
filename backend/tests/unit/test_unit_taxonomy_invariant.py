"""Invariant produit ↔ famille de mesure ↔ unité (incident 2026-09-28 : `boeufs` en `UNITE`).

Générique par taxonomie : aucun test ne dépend du nom « boeufs » seul (chèvres, poulets,
moutons…) ni d'un producteur précis. Le service (`create_product`) applique le même verdict
que le domaine, AVANT toute écriture."""
from __future__ import annotations

import types

import pytest

from ladini.domain.unit_taxonomy import UnitAction, validate_product_unit
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run

LIVESTOCK = ["boeufs", "bœuf", "chèvres", "moutons", "poulets", "porcs"]


class TestDomainVerdict:
    @pytest.mark.parametrize("product", LIVESTOCK)
    def test_livestock_with_mass_or_volume_unit_is_rejected(self, product):
        for unit in ("KG", "kg", "TONNE", "LITRE"):
            verdict = validate_product_unit(product, unit)
            assert verdict.action == UnitAction.REJECT, (product, unit)
            assert verdict.reason == "livestock_requires_count_unit"
            assert "TETE" in verdict.suggested

    @pytest.mark.parametrize("product", LIVESTOCK)
    def test_generic_unite_is_canonicalized_to_tete_for_livestock(self, product):
        verdict = validate_product_unit(product, "UNITE")
        assert verdict.action == UnitAction.CANONICALIZE
        assert verdict.unit == "TETE"

    @pytest.mark.parametrize("product", LIVESTOCK)
    def test_tete_and_packaging_are_accepted_for_livestock(self, product):
        assert validate_product_unit(product, "TETE").ok
        assert validate_product_unit(product, "tête").unit == "TETE"
        assert validate_product_unit(product, "SAC").action == UnitAction.ACCEPT

    @pytest.mark.parametrize("product,unit", [("maïs", "KG"), ("lait", "LITRE"), ("riz", "SAC")])
    def test_unknown_taxonomy_never_causes_a_false_rejection(self, product, unit):
        assert validate_product_unit(product, unit).action == UnitAction.ACCEPT

    def test_admin_category_config_is_the_source_of_truth(self):
        cfg = {"allowed_units": ["LITRE"], "priority_unit": "LITRE"}
        assert validate_product_unit("lait", "LITRE", category_config=cfg).ok
        rejected = validate_product_unit("lait", "KG", category_config=cfg)
        assert rejected.action == UnitAction.REJECT
        assert rejected.reason == "unit_not_allowed_for_category"
        assert rejected.suggested == ("LITRE",)

    def test_admin_config_can_explicitly_allow_unite_for_livestock(self):
        cfg = {"allowed_units": ["TETE", "UNITE"], "priority_unit": "TETE"}
        verdict = validate_product_unit("boeufs", "UNITE", category_config=cfg)
        assert verdict.action == UnitAction.ACCEPT and verdict.unit == "UNITE"


class _Session:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _service(session, *, config=None):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

        async def _resolve_producer_phone(self, *, phone=None, producer_id=None):
            return phone or "+22670000001"

        async def get_producer_profile(self, phone):
            return types.SimpleNamespace(), types.SimpleNamespace(id="00000000-0000-0000-0000-000000000001")

        async def get_product_category_unit_config(self, product_name):
            return config or {"status": "error", "message": "pas de config"}

        async def guess_category(self, product_name=None):
            return "Élevage"

    return _Svc()


class TestCreateProductEnforcesTheInvariantBeforeAnyWrite:
    def test_boeufs_in_kg_is_rejected_and_nothing_is_written(self):
        session = _Session()
        svc = _service(session)
        with pytest.raises(BusinessRuleException) as exc:
            run(svc.create_product("boeufs", 461000, 10, "KG", sub_category_id="00000000-0000-0000-0000-0000000000aa", phone="+22670000001"))
        assert exc.value.reason == "livestock_requires_count_unit"
        assert session.added == []

    def test_the_historical_incident_unite_never_reaches_the_row(self, monkeypatch):
        import ladini.services.database.producer as mod

        async def _no_event(self, product):
            return None

        monkeypatch.setattr(mod.BusinessEventEmitter, "emit_product_published_for_sale", _no_event)
        session = _Session()
        svc = _service(session)

        async def _refresh(*_a, **_k):
            return None

        run(svc.create_product("boeufs", 461000, 461000, "UNITE", sub_category_id="00000000-0000-0000-0000-0000000000aa", phone="+22670000001"))
        assert len(session.added) == 1
        assert session.added[0].unit == "TETE"

    def test_admin_config_rejection_wins_over_the_fallback(self):
        session = _Session()
        svc = _service(session, config={"status": "success", "data": {"allowed_units": ["LITRE"], "priority_unit": "LITRE"}})
        with pytest.raises(BusinessRuleException) as exc:
            run(svc.create_product("lait", 500, 10, "KG", sub_category_id="00000000-0000-0000-0000-0000000000bb", phone="+22670000001"))
        assert exc.value.reason == "unit_not_allowed_for_category"
        assert session.added == []


# ── Phase 20 : qualité de l'offre avant proposition à l'achat ─────────────


class TestSearchOfferDataQuality:
    def test_flags_domain_helper(self):
        from ladini.domain.unit_taxonomy import offer_data_quality_flags

        assert offer_data_quality_flags("maïs", "KG", 300) == ([], True)
        flags, ok = offer_data_quality_flags("maïs", "KG", 0)
        assert flags == ["invalid_price"] and ok is False
        flags, ok = offer_data_quality_flags("boeufs", "KG", 450000)
        assert flags == ["unit_incompatible_with_product"] and ok is True
        flags, ok = offer_data_quality_flags("boeufs", "UNITE", 461000)
        assert flags == ["unit_generic_for_livestock"] and ok is True
        assert offer_data_quality_flags("boeufs", "TETE", 461000) == ([], True)
        assert offer_data_quality_flags("maïs", "KG", "n/a")[1] is False

    def test_search_excludes_unpriced_offers_flags_the_rest_and_never_hides_silently(
        self, monkeypatch, caplog
    ):
        import logging

        from ladini.services.database import buyer as buyer_mod

        def _row(pid, price, unit="TETE"):
            return {
                "id": pid, "name": "boeufs", "price": price, "quantity_for_sale": 5, "unit": unit,
                "images": [], "pricing_tiers": None, "producer_id": "P", "producer_name": "Gilbert-prod",
                "zone_name": "Ouaga", "priority": 3, "minimum_order_quantity": None,
                "minimum_order_unit": None,
            }

        catalog = [_row("ok", 450000), _row("free", 0), _row("legacy-unite", 461000, "UNITE")]

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def mappings(self):
                return self

            def all(self):
                return self._rows

        class _Session:
            calls = 0

            async def scalar(self, _stmt):
                return None

            async def execute(self, _stmt):
                _Session.calls += 1
                return _Result(catalog if _Session.calls == 1 else [])

        async def _no_emit(self, **_kw):
            return None

        monkeypatch.setattr(buyer_mod.BusinessEventEmitter, "emit", _no_emit)

        class _Svc(buyer_mod.BuyerMixin):
            @property
            def session(self):
                return _Session()

            async def get_buyer_profile(self, phone=None):
                return None, None

        with caplog.at_level(logging.WARNING):
            out = run(_Svc().search_products("boeufs", "+22670000001"))
        ids = [r["id"] for r in out["results"]]
        assert ids == ["ok", "legacy-unite"], "l'offre à prix nul doit être écartée"
        flagged = {r["id"]: r["data_quality_flags"] for r in out["results"]}
        assert flagged["ok"] == [] and flagged["legacy-unite"] == ["unit_generic_for_livestock"]
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "SEARCH_OFFER_DATA_QUALITY" in logged and "excluded=True" in logged


# ── Produits DÉRIVÉ d'un animal : jamais comptés à la tête (capture WhatsApp 2026-09-28) ──


class TestAnimalDerivedProductsAreNotLivestock:
    @pytest.mark.parametrize(
        "name",
        ["lait de vache", "Lait de chèvre", "lait de vache caillé", "oeufs de poule", "œufs de pintade",
         "viande de boeuf", "fromage de brebis", "peau de mouton", "fumier de poulet", "beurre de vache"],
    )
    def test_derived_products_are_not_livestock(self, name):
        from ladini.domain.quantity_unit import is_livestock_product

        assert is_livestock_product(name) is False, name

    @pytest.mark.parametrize("name", ["vache", "boeufs", "poulets", "chèvres", "mouton", "cobayes", "poule pondeuse"])
    def test_animals_are_still_livestock(self, name):
        from ladini.domain.quantity_unit import is_livestock_product

        assert is_livestock_product(name) is True, name

    def test_milk_in_litres_is_accepted_by_the_unit_invariant(self):
        assert validate_product_unit("lait de vache", "LITRE").action == UnitAction.ACCEPT
        assert validate_product_unit("oeufs de poule", "PLATEAU").ok
        assert validate_product_unit("viande de boeuf", "KG").ok

    def test_resolve_product_unit_keeps_litre_for_milk(self):
        from ladini.domain.quantity_unit import resolve_product_unit

        assert resolve_product_unit("lait de vache", current_unit="LITRE") == "LITRE"
        assert resolve_product_unit("boeufs", current_unit="KG") == "TETE"

    def test_buyer_display_unit_no_longer_calls_milk_a_head(self):
        from ladini.services.database.buyer import _guess_display_unit

        assert _guess_display_unit("lait de vache", "KG") == "KG"
        assert _guess_display_unit("fromage de chèvre", None) == "KG"
        assert _guess_display_unit("boeufs", "KG") == "TETE"

    def test_create_product_accepts_milk_in_litres(self, monkeypatch):
        import ladini.services.database.producer as mod

        async def _no_event(self, product):
            return None

        monkeypatch.setattr(mod.BusinessEventEmitter, "emit_product_published_for_sale", _no_event)
        session = _Session()
        svc = _service(session)
        run(svc.create_product("lait de vache", 400, 50, "LITRE", sub_category_id="00000000-0000-0000-0000-0000000000cc", phone="+22670000001"))
        assert session.added[0].unit == "LITRE"
