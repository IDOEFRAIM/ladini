"""`services/domain/cart_service.py::CartDomainService.build_product_selection_menu`
— extension "voir les photos d'un producteur" du menu de sélection acheteur
(distinct du catalogue de recherche brut, voir test_rendering_success.py)."""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.services.domain.cart_service import CartDomainService


def _svc():
    return CartDomainService(mc_runtime=None)


def _vendor(vendor_name="Ferme Koné", images=None, product_id="p1"):
    return {
        "product_id": product_id,
        "name": "maïs",
        "price": 250.0,
        "unit": "KG",
        "vendor_name": vendor_name,
        "producer_id": "prod-1",
        "zone": "Zone X",
        "source_type": "DIRECT",
        "is_auction": False,
        "available_qty": 100,
        "images": images or [],
    }


class TestBuildProductSelectionMenuPhotos:
    def test_a_hint_is_added_and_results_are_cached_when_a_vendor_has_photos(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as mod

        captured = {}
        monkeypatch.setattr(
            mod, "_store_search_photo_results",
            lambda phone, entries: captured.update(phone=phone, entries=entries),
        )

        vendors = [_vendor("Ferme Koné", images=["https://x/a.jpg"]), _vendor("Ferme Diallo", images=[])]
        state_patch, _menu = _svc().build_product_selection_menu(
            "maïs", vendors, phone="+22670000001",
        )

        assert "photos <numéro>" in state_patch["final_response"]
        assert captured["phone"] == "+22670000001"
        assert captured["entries"]["1"]["images"] == ["https://x/a.jpg"]
        assert captured["entries"]["2"]["images"] == []

    def test_no_hint_and_no_cache_write_when_no_vendor_has_photos(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as mod

        called = {"count": 0}
        monkeypatch.setattr(
            mod, "_store_search_photo_results",
            lambda phone, entries: called.__setitem__("count", called["count"] + 1),
        )

        vendors = [_vendor("Ferme Koné", images=[])]
        state_patch, _menu = _svc().build_product_selection_menu(
            "maïs", vendors, phone="+22670000001",
        )

        assert "photos <numéro>" not in state_patch["final_response"]
        assert called["count"] == 0

    def test_no_phone_means_no_cache_write_but_menu_still_renders(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as mod

        called = {"count": 0}
        monkeypatch.setattr(
            mod, "_store_search_photo_results",
            lambda phone, entries: called.__setitem__("count", called["count"] + 1),
        )

        vendors = [_vendor("Ferme Koné", images=["https://x/a.jpg"])]
        state_patch, _menu = _svc().build_product_selection_menu("maïs", vendors)

        assert called["count"] == 0
        assert "Ferme Koné" in state_patch["final_response"]

    def test_existing_post_hint_still_appears_after_the_photo_hint(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as mod
        monkeypatch.setattr(mod, "_store_search_photo_results", lambda phone, entries: None)

        vendors = [_vendor("Ferme Koné", images=["https://x/a.jpg"])]
        state_patch, _menu = _svc().build_product_selection_menu(
            "maïs", vendors, phone="+22670000001", post_hint="💡 Répondez *appel* sinon.",
        )

        text = state_patch["final_response"]
        assert text.index("photos <numéro>") < text.index("Répondez *appel*")


class TestResolveProductVendorsImages:
    def test_images_pass_through_from_search_results(self, monkeypatch):
        from unittest.mock import AsyncMock
        import agriconnect.graphs.agents.market_coach.services.mcp.gateway as gw

        monkeypatch.setattr(
            gw.ProductGateway, "search_products",
            AsyncMock(return_value={
                "status": "success",
                "results": [{
                    "id": "p1", "producer_id": "prod-1", "name": "maïs",
                    "price": 250, "unit": "KG", "vendor_name": "Ferme Koné",
                    "images": ["https://x/a.jpg"],
                }],
            }),
        )

        from tests.conftest import run
        vendors, _ = run(_svc().resolve_product_vendors("+22670000001", "maïs"))

        assert vendors[0]["images"] == ["https://x/a.jpg"]

    def test_a_raw_uuid_producer_id_is_cast_to_str(self, monkeypatch):
        """Incident 21656 (2026-08-27) : `producer_id` provient tel quel du
        driver DB (souvent un `uuid.UUID`, pas une string) et finit dans
        `MenuOption.value` — typé `Optional[str]` mais non vérifié à
        l'exécution — puis dans `content_variables` envoyées à Twilio."""
        import uuid
        from unittest.mock import AsyncMock
        import agriconnect.graphs.agents.market_coach.services.mcp.gateway as gw

        raw_uuid = uuid.uuid4()
        monkeypatch.setattr(
            gw.ProductGateway, "search_products",
            AsyncMock(return_value={
                "status": "success",
                "results": [{
                    "id": "p1", "producer_id": raw_uuid, "name": "maïs",
                    "price": 250, "unit": "KG", "vendor_name": "Ferme Koné",
                }],
            }),
        )

        from tests.conftest import run
        vendors, _ = run(_svc().resolve_product_vendors("+22670000001", "maïs"))

        assert vendors[0]["producer_id"] == str(raw_uuid)
        assert isinstance(vendors[0]["producer_id"], str)
