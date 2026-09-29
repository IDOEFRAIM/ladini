"""Contrat d'architecture Phase B2c.5 : PROCUREMENT_CREATE_REQUEST partage LE modele commercial de
SALES_PUBLISH_PRODUCT/PRODUCTION_DECLARE_FUTURE, jamais un second moteur ; RECURRING confirme son
plafond de prix avant creation ; aucun nouveau writer transactionnel n'accepte un prix ambigu.

1. `PROCUREMENT_CREATE_REQUEST` est dans le MEME gate set (`_COMMERCIAL_OFFER_GOALS`) que les deux
   autres goals commerciaux - un seul adaptateur legacy -> `CommercialOffer`, aucune 2e fabrique.
2. `ProcurementDraft.execution_payload` derive prix/unite/quantite de l'offre certifiee (jamais des
   champs plats bruts) quand une offre est presente, et refuse PER_PACKAGE fail-closed.
3. La confirmation (`confirmation_summary.py` ET `nodes/rendering/confirm.py`) projette l'offre
   certifiee - jamais `payload["price"]` brut reinterprete comme "par unite".
4. `render_procurement_price_line` reste l'UNIQUE implementation, reutilisee par
   `confirmation_summary.py` (jamais redefinie).
5. `RecurringNeedDraft.render_summary` affiche le plafond de prix AVANT confirmation - jusqu'ici il
   n'etait jamais montre au buyer.
6. `record_sale` (deja classifie "bookkeeping d'un total deja certain", mandat B2c.5 §18) reste
   inchange - toujours un `CommercialPricingSnapshot` TOTAL_LOT via `declared_sale_snapshot_columns`.
"""
from __future__ import annotations

from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"

VALIDATION = SRC / "graphs" / "agents" / "market_coach" / "nodes" / "validation.py"
PROCUREMENT_DRAFT = SRC / "graphs" / "agents" / "market_coach" / "domain" / "procurement_draft.py"
CONFIRMATION_SUMMARY = SRC / "graphs" / "agents" / "market_coach" / "services" / "ui" / "confirmation_summary.py"
CONFIRM_RENDERING = SRC / "graphs" / "agents" / "market_coach" / "nodes" / "rendering" / "confirm.py"
RECURRING_DRAFT = SRC / "graphs" / "agents" / "market_coach" / "domain" / "recurring_need_draft.py"
PRICING_PERSISTENCE = SRC / "services" / "database" / "pricing_persistence.py"


class TestProcurementSharesTheSingleCommercialOfferGate:
    def test_procurement_create_request_is_in_the_same_gate_set(self):
        from ladini.graphs.agents.market_coach.nodes import (
            validation as validation_module,
        )
        from ladini.graphs.agents.market_coach.services.domain.commercial_gate import (
            SALES_GOAL,
        )

        assert SALES_GOAL in validation_module._COMMERCIAL_OFFER_GOALS
        assert "PRODUCTION_DECLARE_FUTURE" in validation_module._COMMERCIAL_OFFER_GOALS
        assert "PROCUREMENT_CREATE_REQUEST" in validation_module._COMMERCIAL_OFFER_GOALS

    def test_the_gate_call_is_not_duplicated_a_second_time(self):
        src = VALIDATION.read_text(encoding="utf-8")
        assert src.count("evaluate_sales_publish_state(state, payload)") == 1

    def test_procurement_no_longer_falls_back_to_the_narrow_reconcile_price_basis_gate(self):
        """`_PRICE_BASIS_GOALS and goal_upper not in _COMMERCIAL_OFFER_GOALS` exclut
        structurellement PROCUREMENT_CREATE_REQUEST desormais qu'il rejoint le gate ci-dessus."""
        from ladini.graphs.agents.market_coach.nodes import (
            validation as validation_module,
        )

        assert "PROCUREMENT_CREATE_REQUEST" in validation_module._PRICE_BASIS_GOALS
        assert "PROCUREMENT_CREATE_REQUEST" in validation_module._COMMERCIAL_OFFER_GOALS


class TestProcurementDraftDerivesFromTheCertifiedOfferOnly:
    def test_execution_payload_reads_offer_execution_payload_not_raw_price_when_present(self):
        src = PROCUREMENT_DRAFT.read_text(encoding="utf-8")
        assert "offer_execution_payload(offer)" in src

    def test_per_package_is_refused_never_silently_converted(self):
        src = PROCUREMENT_DRAFT.read_text(encoding="utf-8")
        assert "PriceBasis.PER_PACKAGE" in src
        assert "conditionnement" in src


class TestConfirmationNeverRendersTheRawPayloadWhenAnOfferExists:
    def test_confirmation_summary_checks_the_certified_offer_first(self):
        src = CONFIRMATION_SUMMARY.read_text(encoding="utf-8")
        assert 'if goal == "PROCUREMENT_CREATE_REQUEST":' in src
        assert "CommercialOffer.from_dict(payload.get(\"commercial_offer\"))" in src

    def test_confirm_rendering_uses_procurement_draft_render_summary(self):
        src = CONFIRM_RENDERING.read_text(encoding="utf-8")
        assert "ProcurementDraft" in src
        assert "_procurement_draft.render_summary()" in src


class TestSingleCanonicalProcurementPriceLineImplementation:
    def test_render_procurement_price_line_lives_in_procurement_draft(self):
        src = PROCUREMENT_DRAFT.read_text(encoding="utf-8")
        assert "def render_procurement_price_line(" in src

    def test_confirmation_summary_imports_it_never_redefines(self):
        src = CONFIRMATION_SUMMARY.read_text(encoding="utf-8")
        assert "def render_procurement_price_line(" not in src
        assert "render_procurement_price_line" in src

    def test_both_modules_expose_the_same_function_object(self):
        from ladini.graphs.agents.market_coach.domain import procurement_draft
        from ladini.graphs.agents.market_coach.services.ui import confirmation_summary

        assert confirmation_summary.render_procurement_price_line is procurement_draft.render_procurement_price_line


class TestRecurringNeedConfirmsItsPriceCapBeforeCreation:
    def test_render_summary_source_shows_max_price_per_unit(self):
        src = RECURRING_DRAFT.read_text(encoding="utf-8")
        assert "max_price_per_unit" in src.split("def render_summary")[1].split("def ")[0]


class TestRecordSaleIsUnchangedAlreadyCertified:
    def test_declared_sale_still_builds_a_total_lot_snapshot(self):
        src = PRICING_PERSISTENCE.read_text(encoding="utf-8")
        assert "def declared_sale_snapshot_columns" in src
        assert "build_total_lot_order_item_snapshot" in src

    def test_record_sale_still_yields_a_certified_total_lot_snapshot(self):
        from ladini.services.database.pricing_persistence import (
            declared_sale_snapshot_columns,
        )

        columns = declared_sale_snapshot_columns(
            total_amount=25000, quantity=50, unit="KG", price_at_sale=500,
        )
        assert columns["price_basis"] == "TOTAL_LOT"
        assert float(columns["commercial_price_amount"]) == 25000.0
