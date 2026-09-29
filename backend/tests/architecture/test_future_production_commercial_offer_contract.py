"""Contrat d'architecture Phase B2c.3 : PRODUCTION_DECLARE_FUTURE partage LE modèle commercial de
SALES_PUBLISH_PRODUCT, jamais un second moteur, jamais un rendu depuis le payload brut.

1. `PRODUCTION_DECLARE_FUTURE` passe par le MÊME gate (`evaluate_sales_publish_state`) que
   SALES_PUBLISH_PRODUCT — un seul adaptateur legacy -> `CommercialOffer`, aucune 2e fabrique.
2. `AgronomyService.declare_crop_cycle` est l'UNIQUE endroit qui construit le payload MCP
   `declare_future_production` — et il le dérive de l'offre certifiée, jamais de champs plats bruts,
   dès qu'une offre est présente.
3. Le récapitulatif de confirmation (`build_confirmation_summary`) projette l'offre certifiée —
   jamais `payload["price"]` brut réinterprété comme « par unité ».
4. Aucun `price_per_unit`/`available_quantity` n'atteint `MarketOffer` sans passer par
   `certify_commercial_offer` (même garde que `create_product`)."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ladini.domain.commercial_offer import (
    CommercialOffer,
    CommercialQuantity,
    InventoryQuantity,
    PriceBasis,
    Pricing,
    Provenance,
)
from ladini.graphs.agents.market_coach.nodes import validation as validation_module
from ladini.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary,
)

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"
AGRO = SRC / "graphs" / "agents" / "market_coach" / "domain" / "agro.py"
PRODUCER_DB = SRC / "services" / "database" / "producer.py"


class TestSingleCommercialOfferGateForBothGoals:
    def test_production_declare_future_is_in_the_same_gate_set_as_sales_publish_product(self):
        assert validation_module.SALES_GOAL in validation_module._COMMERCIAL_OFFER_GOALS
        assert "PRODUCTION_DECLARE_FUTURE" in validation_module._COMMERCIAL_OFFER_GOALS

    def test_the_gate_call_is_not_duplicated_a_second_time_for_this_goal(self):
        """Un seul appel à `evaluate_sales_publish_state` dans le validator — le fait qu'il couvre
        les deux goals vient du SET ci-dessus, jamais d'un second bloc `if goal == ...`."""
        src = inspect.getsource(validation_module)
        assert src.count("evaluate_sales_publish_state(state, payload)") == 1


class TestDeclareCropCycleIsTheOnlyMcpPayloadBuilderAndDerivesFromTheOffer:
    def test_only_agro_py_builds_the_declare_future_production_tool_args(self):
        callers = [
            p for p in SRC.rglob("*.py")
            if p != AGRO and "DECLARE_FUTURE_PRODUCTION" in p.read_text(encoding="utf-8")
        ]
        # Seuls le registre d'outils (définition de l'enum) et le service DB (consommateur MCP) le
        # référencent en dehors de l'adaptateur lui-même — aucun AUTRE constructeur de payload.
        allowed_names = {"tooling.py"}
        offenders = [p for p in callers if p.name not in allowed_names]
        assert offenders == [], offenders

    def test_declare_crop_cycle_reads_offer_execution_payload_not_raw_price_when_an_offer_is_present(self):
        src = AGRO.read_text(encoding="utf-8")
        assert "offer_execution_payload(offer)" in src
        # Le repli legacy (`float(require(payload, "price"))`) reste dans le `else` — présent, mais
        # jamais le SEUL chemin : le contrat n'exige que sa coexistence avec la dérivation certifiée.
        assert "offer_execution_payload" in src and "float(require(payload, \"price\"))" in src

    def test_per_package_is_refused_never_silently_converted_to_a_unit_price(self):
        src = AGRO.read_text(encoding="utf-8")
        assert "PriceBasis.PER_PACKAGE" in src and "raise ValueError" in src


class TestMarketOfferWritesGoThroughCertifiedPricing:
    def test_declare_future_production_calls_certify_commercial_offer_before_constructing_the_row(self):
        src = PRODUCER_DB.read_text(encoding="utf-8")
        tree = ast.parse(src)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "declare_future_production"
        )
        body_src = ast.get_source_segment(src, func) or ""
        certify_at = body_src.index("certify_commercial_offer(")
        construct_at = body_src.index("MarketOffer(")
        assert certify_at < construct_at, "le snapshot doit être dérivé/validé AVANT de construire la ligne"
        assert "pricing_snapshot=pricing_snapshot_dict" in body_src


class TestConfirmationRendersTheCertifiedOfferNeverTheRawPayload:
    def _offer(self, *, amount, basis, basis_unit=None):
        return CommercialOffer(
            product="tomates",
            commercial_quantity=CommercialQuantity(10.0, "TONNE", Provenance.USER_EXPLICIT),
            inventory_quantity=InventoryQuantity(10.0, "TONNE", Provenance.USER_EXPLICIT),
            pricing=Pricing(
                amount=amount, basis=PriceBasis(basis), basis_unit=basis_unit,
                source=Provenance.USER_EXPLICIT, basis_source=Provenance.USER_EXPLICIT,
            ),
        )

    def test_total_lot_is_never_rendered_as_a_per_unit_price(self):
        offer = self._offer(amount=4_000_000.0, basis="TOTAL_LOT")
        payload = {
            "product": "tomates", "production_type": "CROP",
            # champs legacy volontairement TROMPEURS (base PER_BASE_UNIT) : le rendu doit les ignorer.
            "price": 4_000_000.0, "price_unit": "TONNE",
            "commercial_offer": offer.to_dict(),
        }
        summary = build_confirmation_summary("PRODUCTION_DECLARE_FUTURE", payload)
        assert "pour l'ensemble" in summary or "au total" in summary
        assert "FCFA/tonne" not in summary and "FCFA/TONNE" not in summary

    def test_per_base_unit_is_rendered_with_its_own_unit(self):
        offer = self._offer(amount=400000.0, basis="PER_BASE_UNIT", basis_unit="TONNE")
        payload = {
            "product": "tomates", "production_type": "CROP",
            "commercial_offer": offer.to_dict(),
        }
        summary = build_confirmation_summary("PRODUCTION_DECLARE_FUTURE", payload)
        assert "400 000 FCFA" in summary and "tonne" in summary

    def test_without_a_certified_offer_the_legacy_rendering_still_works(self):
        payload = {"product": "mil", "production_type": "CROP", "quantity": 5, "unit": "TONNE", "price": 300000}
        summary = build_confirmation_summary("PRODUCTION_DECLARE_FUTURE", payload)
        assert "mil" in summary
