"""Contrat d'architecture Phase B2c.4 : tout consommateur buyer affiche la sémantique commerciale
CERTIFIÉE (`PricingView.pricing_label`), jamais une reconstruction `f"{price} FCFA/{unit}"` qui
suppose silencieusement "par unité" — exactement le bug déjà fermé pour les écritures en B1/B2c.3,
fermé ici pour les LECTURES buyer.

1. `search_products` (services/database/buyer.py) sélectionne la colonne de snapshot ET utilise
   `product_pricing_view`/`market_offer_pricing_view` — jamais seulement `price`/`unit` bruts.
2. Le tri des résultats (`_sort_price`) compare un prix NORMALISÉ quand il existe, jamais un
   montant brut TOTAL_LOT contre un montant brut PER_BASE_UNIT.
3. Chaque renderer buyer identifié (menu de recherche, menu vendeur, panier, alerte proactive)
   préfère `pricing_label`/`v.get("pricing_label")` à une reconstruction brute.
4. `render_pricing_label`/`comparable_total` restent l'UNIQUE implémentation (relocalisées dans
   `commercial_pricing_snapshot.py`), `bid_pricing_flow.py` les ré-exporte sans les redéfinir.
"""
from __future__ import annotations

from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"

BUYER_DB = SRC / "services" / "database" / "buyer.py"
CATEGORY_DB = SRC / "services" / "database" / "category.py"
SUCCESS_RENDERING = SRC / "graphs" / "agents" / "market_coach" / "nodes" / "rendering" / "success.py"
CART_SERVICE = SRC / "graphs" / "agents" / "market_coach" / "services" / "domain" / "cart_service.py"
CART_FLOW = SRC / "graphs" / "agents" / "market_coach" / "flows" / "buyer" / "cart.py"
SELECTION_ACTIONS = SRC / "graphs" / "agents" / "market_coach" / "domain" / "selection_actions.py"
PROXIMITY_SERVICE = SRC / "workers" / "automation" / "proximity_matching_service.py"
OUTBOX_TEMPLATES = SRC / "workers" / "outbox" / "templates.py"
BID_PRICING_FLOW = SRC / "domain" / "bid_pricing_flow.py"
PRICING_SNAPSHOT = SRC / "domain" / "commercial_pricing_snapshot.py"


class TestSearchProductsUsesCertifiedPricingViews:
    def test_selects_the_snapshot_columns(self):
        src = BUYER_DB.read_text(encoding="utf-8")
        assert "Product.commercial_pricing" in src
        assert "MarketOffer.pricing_snapshot" in src

    def test_builds_a_pricing_view_for_both_catalog_and_future_rows(self):
        src = BUYER_DB.read_text(encoding="utf-8")
        assert "product_pricing_view(row)" in src
        assert "market_offer_pricing_view(row)" in src

    def test_sort_key_prefers_the_normalized_price_never_mixes_raw_bases(self):
        src = BUYER_DB.read_text(encoding="utf-8")
        assert "def _sort_price" in src
        assert "normalized_unit_price" in src


class TestBuyerRenderersPreferTheCertifiedLabel:
    def _asserts_pricing_label_preference(self, path: Path) -> None:
        src = path.read_text(encoding="utf-8")
        assert "pricing_label" in src, f"{path.name} n'utilise pas pricing_label"

    def test_search_results_menu(self):
        self._asserts_pricing_label_preference(SUCCESS_RENDERING)

    def test_vendor_selection_menu(self):
        self._asserts_pricing_label_preference(CART_SERVICE)

    def test_producer_option_relist(self):
        self._asserts_pricing_label_preference(SELECTION_ACTIONS)

    def test_cart_vendor_switch_and_prompts(self):
        self._asserts_pricing_label_preference(CART_FLOW)

    def test_proactive_new_offer_alert(self):
        self._asserts_pricing_label_preference(PROXIMITY_SERVICE)
        self._asserts_pricing_label_preference(OUTBOX_TEMPLATES)


class TestSingleCanonicalLabelAndComparisonImplementation:
    def test_render_pricing_label_lives_in_commercial_pricing_snapshot(self):
        src = PRICING_SNAPSHOT.read_text(encoding="utf-8")
        assert "def render_pricing_label(" in src
        assert "def comparable_total(" in src

    def test_bid_pricing_flow_re_exports_never_redefines(self):
        src = BID_PRICING_FLOW.read_text(encoding="utf-8")
        assert "def render_pricing_label(" not in src
        assert "def comparable_total(" not in src
        assert "render_pricing_label" in src and "comparable_total" in src

    def test_both_modules_expose_the_same_function_object(self):
        from ladini.domain import bid_pricing_flow
        from ladini.domain.commercial_pricing_snapshot import (
            comparable_total,
            render_pricing_label,
        )

        assert bid_pricing_flow.render_pricing_label is render_pricing_label
        assert bid_pricing_flow.comparable_total is comparable_total


class TestPricingViewNeverInventsCertaintyFromARawRow:
    def test_field_accessor_supports_both_orm_rows_and_row_mappings(self):
        src = PRICING_SNAPSHOT.read_text(encoding="utf-8")
        assert "def _field(row: Any, name: str) -> Any:" in src

    def test_legacy_row_is_never_comparable(self):
        from ladini.domain.commercial_pricing_snapshot import market_offer_pricing_view

        view = market_offer_pricing_view({"price_per_unit": 100.0, "pricing_snapshot": None})
        assert not view.is_comparable
        assert view.status == "LEGACY_PARTIAL"
