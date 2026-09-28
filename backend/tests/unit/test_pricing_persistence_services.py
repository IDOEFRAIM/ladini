"""Couche service du contrat de persistance (Phase B2a) : pont ORM <-> domaine, sans base.

Ce qui est verrouillé ici : l'offre certifiée voyage du draft à `create_product` SANS jamais être
recomposée par un appelant ; toute divergence legacy/snapshot échoue AVANT l'écriture ; un bid neuf
porte sa base ; un bid ancien reste « base inconnue » ; l'attribution d'un appel d'offres suit la base
déclarée du bid (et non « prix × quantité de l'enchère »)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from ladini.domain.commercial_offer import (
    CommercialOffer,
    CommercialQuantity,
    InventoryQuantity,
    PackageDefinition,
    PackageStatus,
    PriceBasis,
    Pricing,
    Provenance,
    derive_normalized,
)
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingReliability,
    bid_pricing_view,
)
from ladini.graphs.agents.market_coach.actions.sales_dto import (
    SalesPublishProductPayload,
)
from ladini.graphs.agents.market_coach.domain.sales import (
    SalesPublishProductCommand,
    SalesService,
)
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
)
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.pricing_persistence import (
    award_total_and_snapshot,
    bid_snapshot_columns,
    certify_commercial_offer,
    declared_sale_snapshot_columns,
    invalidate_commercial_pricing_on_edit,
    order_item_snapshot_columns,
    reprice_bid_columns,
)

D = Decimal


def _with_normalized(offer: CommercialOffer) -> CommercialOffer:
    from dataclasses import replace

    return replace(offer, normalized=derive_normalized(offer))


def _sachet_offer() -> CommercialOffer:
    return _with_normalized(CommercialOffer(
        product="lait",
        commercial_quantity=CommercialQuantity(50.0, "LITRE", Provenance.USER_EXPLICIT),
        inventory_quantity=InventoryQuantity(50.0, "LITRE", Provenance.USER_EXPLICIT),
        pricing=Pricing(500.0, PriceBasis.PER_PACKAGE, "FCFA", Provenance.USER_EXPLICIT, Provenance.USER_EXPLICIT),
        package=PackageDefinition("SACHET", 0.5, "LITRE", PackageStatus.KNOWN, Provenance.QUESTION_CONTEXT_EXPLICIT),
    ))


def _tool_args_for(offer: CommercialOffer) -> dict:
    """Le chemin RÉEL draft -> DTO -> commande -> arguments de l'outil `create_product`."""
    draft = SalesPublishDraft.new(
        "d1", product="lait", quantity=50.0, unit="LITRE", price=500.0, commercial_offer=offer.to_dict()
    )
    dto = SalesPublishProductPayload.from_payload(draft.execution_payload())
    command = SalesPublishProductCommand(
        producer_id="+22670000001", product=dto.product, quantity=dto.quantity, unit=dto.unit,
        price=dto.price, description=dto.description, category_label=dto.category_label,
        pricing_tiers=dto.pricing_tiers, commercial_offer=dto.commercial_offer,
    )
    return dict(SalesService(context=SimpleNamespace()).publish_product(command).tool_args)


class TestCreateProductCertification:
    def test_the_certified_offer_reaches_the_tool_and_is_certified_there(self):
        args = _tool_args_for(_sachet_offer())
        assert args["commercial_offer"]["pricing"]["basis"] == "PER_PACKAGE"
        # pricing_tiers du chemin réel : validés comme le fait create_product (tier_id serveur)
        from ladini.domain.pricing_tiers import tiers_to_dicts, validate_pricing_tiers

        tiers = tiers_to_dicts(validate_pricing_tiers(args["pricing_tiers"], args["unit"]))
        stored = certify_commercial_offer(
            args["commercial_offer"], price=args["price"], unit=args["unit"],
            quantity_for_sale=args["quantity_for_sale"], pricing_tiers=tiers,
        )
        snap = CommercialPricingSnapshot.from_dict(stored)
        assert (snap.commercial_price_amount, snap.price_basis, snap.package_type) == (D("500"), PriceBasis.PER_PACKAGE, "SACHET")
        assert args["price"] == 1000.0  # projection legacy = prix normalisé

    def test_without_an_offer_the_product_stays_legacy(self):
        assert certify_commercial_offer(None, price=300, unit="KG", quantity_for_sale=10, pricing_tiers=None) is None

    def test_legacy_price_that_contradicts_the_offer_is_rejected_before_writing(self):
        args = _tool_args_for(_sachet_offer())
        with pytest.raises(BusinessRuleException):
            certify_commercial_offer(
                args["commercial_offer"], price=500.0,  # 500/sachet posé comme prix/L
                unit=args["unit"], quantity_for_sale=args["quantity_for_sale"], pricing_tiers=args["pricing_tiers"],
            )

    def test_inventory_that_contradicts_the_offer_is_rejected(self):
        args = _tool_args_for(_sachet_offer())
        with pytest.raises(BusinessRuleException):
            certify_commercial_offer(
                args["commercial_offer"], price=args["price"], unit=args["unit"],
                quantity_for_sale=999.0, pricing_tiers=args["pricing_tiers"],
            )

    def test_a_package_price_without_the_matching_tier_is_rejected(self):
        args = _tool_args_for(_sachet_offer())
        with pytest.raises(BusinessRuleException):
            certify_commercial_offer(
                args["commercial_offer"], price=args["price"], unit=args["unit"],
                quantity_for_sale=args["quantity_for_sale"], pricing_tiers=None,
            )

    def test_an_unreadable_or_uncertifiable_offer_is_rejected(self):
        with pytest.raises(BusinessRuleException):
            certify_commercial_offer({"garbage": True}, price=1, unit="KG", quantity_for_sale=1, pricing_tiers=None)
        incomplete = _sachet_offer().to_dict()
        incomplete["package"]["content_amount"] = None
        with pytest.raises(BusinessRuleException):
            certify_commercial_offer(incomplete, price=1000, unit="LITRE", quantity_for_sale=50, pricing_tiers=None)

    def test_a_caller_cannot_smuggle_a_snapshot_through_the_dto(self):
        payload = SalesPublishProductPayload.from_payload(
            {"product": "lait", "quantity": 1, "price": 1, "unit": "LITRE", "commercial_pricing": {"schema_version": 1}}
        )
        assert not hasattr(payload, "commercial_pricing")


class TestBidPersistence:
    AUCTION = SimpleNamespace(id="a1", quantity=D("10"), unit="TONNE")

    def test_a_new_bid_carries_its_basis(self):
        cols = bid_snapshot_columns(amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction=self.AUCTION)
        assert cols["offered_price"] == D("450000") and cols["offered_price_basis"] == "PER_BASE_UNIT"
        assert cols["offered_price_unit"] == "TONNE" and cols["pricing_snapshot_version"] == 1

    def test_a_basis_incompatible_with_the_auction_is_rejected(self):
        with pytest.raises(BusinessRuleException):
            bid_snapshot_columns(amount=450, basis="PER_BASE_UNIT", price_unit="LITRE", auction=self.AUCTION)

    def test_a_missing_basis_is_rejected_at_the_certified_entry_point(self):
        with pytest.raises(BusinessRuleException):
            bid_snapshot_columns(amount=450000, basis=None, price_unit=None, auction=self.AUCTION)

    def test_repricing_a_certified_bid_keeps_its_basis(self):
        bid = SimpleNamespace(**bid_snapshot_columns(amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction=self.AUCTION))
        cols = reprice_bid_columns(bid, self.AUCTION, 430000)
        assert cols["offered_price"] == D("430000") and cols["offered_price_basis"] == "PER_BASE_UNIT"
        assert cols["normalized_unit_price"] == D("430.0000")

    def test_repricing_a_legacy_bid_does_not_invent_a_basis(self):
        legacy = SimpleNamespace(offered_price=D("450000"), offered_price_basis=None, pricing_snapshot_version=None)
        assert reprice_bid_columns(legacy, self.AUCTION, 430000) == {"offered_price": 430000.0}


class TestAwardTotals:
    AUCTION = SimpleNamespace(id="a1", quantity=D("10"), unit="TONNE")

    def _bid(self, **kw):
        return SimpleNamespace(**bid_snapshot_columns(auction=self.AUCTION, **kw))

    def test_per_tonne_bid_total_and_frozen_snapshot(self):
        total, frozen = award_total_and_snapshot(self._bid(amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE"), self.AUCTION)
        assert total == D("4500000.00")
        assert frozen["price_basis"] == "PER_BASE_UNIT" and frozen["price_unit"] == "TONNE"
        assert frozen["award"]["auction_quantity"] == "10" and frozen["award"]["total_amount"] == "4500000.00"
        assert CommercialPricingSnapshot.from_dict(frozen) is not None

    def test_total_lot_bid_is_not_multiplied_by_the_quantity(self):
        total, frozen = award_total_and_snapshot(self._bid(amount=4_500_000, basis="TOTAL_LOT", price_unit=None), self.AUCTION)
        assert total == D("4500000.00") and frozen["price_basis"] == "TOTAL_LOT"

    def test_legacy_bid_keeps_the_historical_total_and_gets_no_snapshot(self):
        legacy = SimpleNamespace(offered_price=D("450000"), offered_price_basis=None, pricing_snapshot_version=None)
        total, frozen = award_total_and_snapshot(legacy, self.AUCTION)
        assert total == D("4500000") and frozen is None  # comportement historique, base inconnue
        assert bid_pricing_view(legacy).reliability == PricingReliability.UNKNOWN_BASIS


class TestOrderItemColumns:
    PRODUCT = SimpleNamespace(
        id="p1", name="lait", price=D("1000"), unit="LITRE", commercial_pricing=None,
        pricing_tiers=[{"tier_id": "t1", "quantity": 0.5, "unit": "LITRE", "price": 500, "packaging": "sachet"}],
    )

    def test_columns_of_a_tier_purchase(self):
        cols = order_item_snapshot_columns(
            self.PRODUCT, quantity=4, price_at_sale=500, tier_id="t1", base_unit_quantity=2
        )
        assert cols["price_basis"] == "PER_PACKAGE" and cols["package_type"] == "SACHET"
        assert cols["commercial_price_amount"] == D("500") and cols["quantity_unit"] == "SACHET"
        assert cols["pricing_snapshot_version"] == 1

    def test_an_inconsistent_price_fails_before_the_row_is_built(self):
        with pytest.raises(BusinessRuleException):
            order_item_snapshot_columns(self.PRODUCT, quantity=4, price_at_sale=1000, tier_id="t1", base_unit_quantity=2)

    def test_recurring_allocation_uses_its_own_price_and_unit(self):
        product = SimpleNamespace(id="p2", name="tomate", price=D("999"), unit="KG", commercial_pricing=None, pricing_tiers=None)
        cols = order_item_snapshot_columns(
            product, quantity=40, price_at_sale=320.0, unit="KG", unit_price_override=320.0,
            price_source="RECURRING_ALLOCATION",
        )
        assert cols["commercial_price_amount"] == D("320") and cols["price_basis"] == "PER_BASE_UNIT"

    def test_declared_sale_is_a_total_lot(self):
        cols = declared_sale_snapshot_columns(total_amount=25000, quantity=50, unit="KG", price_at_sale=500)
        assert cols["price_basis"] == "TOTAL_LOT" and cols["commercial_price_amount"] == D("25000")


class TestProductEditInvalidatesTheCertifiedSnapshot:
    def _product(self, offer=None):
        from ladini.domain.commercial_pricing_snapshot import snapshot_from_offer

        return SimpleNamespace(commercial_pricing=snapshot_from_offer(offer or _sachet_offer()).to_dict())

    def test_price_unit_or_tier_edits_drop_it(self):
        for kw in ({"price_changed": True}, {"unit_changed": True}, {"tiers_changed": True}):
            p = self._product()
            flags = dict(price_changed=False, unit_changed=False, tiers_changed=False, quantity_changed=False)
            flags.update(kw)
            assert invalidate_commercial_pricing_on_edit(p, **flags) is True
            assert p.commercial_pricing is None

    def test_a_quantity_only_edit_keeps_a_per_package_snapshot(self):
        p = self._product()
        assert invalidate_commercial_pricing_on_edit(
            p, price_changed=False, unit_changed=False, tiers_changed=False, quantity_changed=True
        ) is False
        assert p.commercial_pricing is not None

    def test_a_quantity_edit_resizes_a_total_lot_so_it_is_dropped(self):
        lot = _with_normalized(CommercialOffer(
            product="maïs",
            commercial_quantity=CommercialQuantity(200.0, "TONNE", Provenance.USER_EXPLICIT),
            inventory_quantity=InventoryQuantity(200000.0, "KG", Provenance.UNIT_CONVERSION),
            pricing=Pricing(5_000_000.0, PriceBasis.TOTAL_LOT, "FCFA", Provenance.USER_EXPLICIT, Provenance.USER_EXPLICIT),
        ))
        p = self._product(lot)
        assert invalidate_commercial_pricing_on_edit(
            p, price_changed=False, unit_changed=False, tiers_changed=False, quantity_changed=True
        ) is True
        assert p.commercial_pricing is None

    def test_a_legacy_product_is_untouched(self):
        p = SimpleNamespace(commercial_pricing=None)
        assert invalidate_commercial_pricing_on_edit(
            p, price_changed=True, unit_changed=True, tiers_changed=True, quantity_changed=True
        ) is False
