"""Pont ORM <-> contrat de persistance de la sémantique commerciale (Phase B2a).

Les règles vivent dans `domain/commercial_pricing_snapshot.py` (pur, sans E/S) ; ce module ne fait
que lire les attributs des lignes ORM et convertir les échecs du domaine en `BusinessRuleException`
— le moment où un dual-write incohérent doit échouer : AVANT le commit, jamais une ligne à moitié
vraie en base.

Trois points d'entrée, un par famille de lignes :

- `order_item_snapshot_columns`  : ligne de commande (achat direct, précommande, allocation) ;
- `bid_snapshot_columns`         : bid neuf (base du prix OBLIGATOIRE) ;
- `award_snapshot_payload`       : instantané d'attribution d'un appel d'offres (`orders.award_pricing_snapshot`).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from ladini.domain.bid_award import (
    AwardNotPossible,
    CertifiedAwardDecision,
    build_award_decision,
    compare_bid,
)
from ladini.domain.commercial_offer import CommercialOffer, PriceBasis
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingSnapshotError,
    assert_legacy_projection_matches,
    award_total,
    bid_pricing_view,
    build_bid_pricing_snapshot,
    build_order_item_pricing_snapshot,
    build_total_lot_order_item_snapshot,
    snapshot_from_offer,
    to_decimal,
)
from ladini.services.database.errors import BusinessRuleException

logger = logging.getLogger("Ladini.PricingPersistence")


_INVENTORY_TOLERANCE = Decimal("0.001")


def certify_commercial_offer(
    commercial_offer: Optional[Dict[str, Any]],
    *,
    price: Any,
    unit: str,
    quantity_for_sale: Any,
    pricing_tiers: Optional[list],
) -> Optional[Dict[str, Any]]:
    """`Product.commercial_pricing` à partir d'une offre SÉRIALISÉE reçue par l'outil MCP.

    L'appelant ne fournit jamais un snapshot : l'offre est reconstruite, RE-VALIDÉE (VALID, base
    certifiée), projetée, puis les champs legacy qu'il a envoyés (`price`, `unit`, `quantity_for_sale`,
    `pricing_tiers`) sont comparés à la projection du snapshot — divergence = rejet AVANT l'écriture.
    `None` -> produit sans offre certifiée (legacy : sémantique inconnue, pas un TOTAL_LOT ni un sachet)."""
    if not commercial_offer:
        return None
    offer = CommercialOffer.from_dict(commercial_offer)
    if offer is None:
        raise BusinessRuleException("Offre commerciale illisible.", reason="commercial_offer_invalid")
    try:
        snapshot = snapshot_from_offer(offer)
        assert_legacy_projection_matches(snapshot, legacy_price=price, legacy_unit=unit)
        inv_amount, inv_unit = snapshot.inventory_quantity_amount, snapshot.inventory_quantity_unit
        if inv_amount is None or abs(inv_amount - to_decimal(quantity_for_sale, field="quantity_for_sale")) > _INVENTORY_TOLERANCE:
            raise PricingSnapshotError(
                f"dual-write: quantity_for_sale {quantity_for_sale} ≠ inventaire du snapshot {inv_amount}"
            )
        if str(inv_unit or "").upper() != str(unit or "").upper():
            raise PricingSnapshotError(f"dual-write: unit {unit} ≠ unité d'inventaire {inv_unit}")
        if snapshot.price_basis == PriceBasis.PER_PACKAGE:
            match = [
                t for t in (pricing_tiers or [])
                if str(t.get("packaging") or "").upper() == snapshot.package_type
                and abs(to_decimal(t.get("price"), field="tier.price") - snapshot.commercial_price_amount) <= Decimal("0.01")
                and abs(to_decimal(t.get("quantity"), field="tier.quantity") - (snapshot.package_content_amount or Decimal(0))) <= _INVENTORY_TOLERANCE
            ]
            if not match:
                raise PricingSnapshotError("dual-write: aucun palier pricing_tiers ne projette le prix par conditionnement")
    except PricingSnapshotError as exc:
        logger.warning("PRODUCT_PRICING_SNAPSHOT_REJECTED | %s", exc)
        raise BusinessRuleException(
            f"Sémantique de prix incohérente : {exc}", reason="pricing_snapshot_inconsistent"
        ) from exc
    return dict(snapshot.to_dict())


def invalidate_commercial_pricing_on_edit(
    product: Any, *, price_changed: bool, unit_changed: bool, tiers_changed: bool, quantity_changed: bool
) -> bool:
    """Une édition ad hoc (prix, unité, paliers) rend le snapshot certifié FAUX : on le retire plutôt
    que de laisser un `commercial_pricing` qui contredit les champs legacy. Un changement de quantité
    seul ne touche pas la base du prix — sauf pour un lot à prix total (TOTAL_LOT), dont le lot a
    changé. Retourne True si le snapshot a été retiré (=> sémantique redevenue « inconnue »)."""
    current = CommercialPricingSnapshot.from_dict(getattr(product, "commercial_pricing", None))
    if current is None:
        return False
    total_lot_resized = quantity_changed and current.price_basis == PriceBasis.TOTAL_LOT
    if price_changed or unit_changed or tiers_changed or total_lot_resized:
        product.commercial_pricing = None
        return True
    return False


def order_item_snapshot_columns(
    product: Any,
    *,
    quantity: Any,
    price_at_sale: Any,
    tier_id: Optional[str] = None,
    base_unit_quantity: Any = None,
    unit: Optional[str] = None,
    unit_price_override: Any = None,
    price_source: Optional[str] = None,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    """Colonnes `order_items.*` du snapshot immuable de CETTE ligne.

    `unit`/`unit_price_override` : une allocation récurrente porte son propre prix et son unité
    (convenus à l'appariement) — jamais ceux, éventuellement modifiés depuis, du produit."""
    try:
        snapshot = build_order_item_pricing_snapshot(
            product_price=unit_price_override if unit_price_override is not None else getattr(product, "price", None),
            product_unit=unit or getattr(product, "unit", None) or "KG",
            quantity=quantity,
            product_pricing_tiers=getattr(product, "pricing_tiers", None),
            product_commercial_pricing=None if unit_price_override is not None else getattr(product, "commercial_pricing", None),
            tier_id=tier_id,
            base_unit_quantity=base_unit_quantity,
            price_at_sale=price_at_sale,
            currency=currency,
            price_source=price_source,
        )
    except PricingSnapshotError as exc:
        logger.warning(
            "ORDER_ITEM_SNAPSHOT_REJECTED | product=%s | tier=%s | %s",
            getattr(product, "id", None), tier_id, exc,
        )
        raise BusinessRuleException(
            f"Sémantique de prix incohérente pour « {getattr(product, 'name', 'ce produit')} » : {exc}",
            reason="pricing_snapshot_inconsistent",
        ) from exc
    return dict(snapshot.to_order_item_columns())


def declared_sale_snapshot_columns(
    *, total_amount: Any, quantity: Any, unit: str, price_at_sale: Any
) -> Dict[str, Any]:
    """Vente déclarée par le producteur (« 50 kg pour 25 000 ») : TOTAL_LOT exact."""
    try:
        snapshot = build_total_lot_order_item_snapshot(
            total_amount=total_amount, quantity=quantity, unit=unit, price_at_sale=price_at_sale
        )
    except PricingSnapshotError as exc:
        raise BusinessRuleException(
            f"Sémantique de prix incohérente pour la vente déclarée : {exc}",
            reason="pricing_snapshot_inconsistent",
        ) from exc
    return dict(snapshot.to_order_item_columns())


def bid_snapshot_columns(
    *,
    amount: Any,
    basis: Any,
    price_unit: Optional[str],
    auction: Any,
    package_type: Optional[str] = None,
    package_content_amount: Any = None,
    package_content_unit: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Colonnes `bids.*` d'un bid NEUF : la base du prix est obligatoire et validée contre l'unité de
    l'enchère (jamais déduite de celle-ci)."""
    try:
        snapshot = build_bid_pricing_snapshot(
            amount=amount,
            basis=basis,
            price_unit=price_unit,
            auction_quantity=auction.quantity,
            auction_unit=auction.unit,
            package_type=package_type,
            package_content_amount=package_content_amount,
            package_content_unit=package_content_unit,
            source=source,
        )
        # une base PER_BASE_UNIT/PER_PACKAGE doit se rapporter à l'unité de l'enchère
        snapshot.total_for(auction.quantity, auction.unit)
    except PricingSnapshotError as exc:
        raise BusinessRuleException(
            f"Prix de l'offre incohérent avec l'enchère : {exc}", reason="bid_pricing_invalid"
        ) from exc
    return dict(snapshot.to_bid_columns())


def reprice_bid_columns(bid: Any, auction: Any, new_amount: Any) -> Dict[str, Any]:
    """Nouveau montant d'un bid EXISTANT (négociation). Bid certifié : même base, snapshot recalculé
    (normalisé compris). Bid antérieur (sans base) : seul le montant change, la base reste INCONNUE."""
    view = bid_pricing_view(bid)
    if view.snapshot is None:
        return {"offered_price": float(new_amount)}
    snap = view.snapshot
    return bid_snapshot_columns(
        amount=new_amount,
        basis=snap.price_basis,
        price_unit=snap.price_unit,
        auction=auction,
        package_type=snap.package_type,
        package_content_amount=snap.package_content_amount,
        package_content_unit=snap.package_content_unit,
        source=snap.price_source,
    )


def award_total_and_snapshot(bid: Any, auction: Any) -> Tuple[Decimal, Optional[Dict[str, Any]]]:
    """(total, instantané d'attribution) d'un appel d'offres gagné.

    Bid certifié : total selon SA base (`450000/TONNE` × 10 TONNE ; `TOTAL_LOT` = le montant) et
    instantané JSON gelé sur la commande. Bid antérieur : total historique inchangé
    (`offered_price × quantité`, l'hypothèse « par unité de l'enchère » de l'ancien code) mais
    AUCUN instantané — sa base est inconnue, on ne fabrique pas de certitude."""
    view = bid_pricing_view(bid)
    if view.snapshot is None:
        legacy_total = Decimal(str(bid.offered_price)) * Decimal(str(auction.quantity))
        return legacy_total, None
    snap = view.snapshot
    try:
        total = award_total(snap, auction.quantity, auction.unit)
    except PricingSnapshotError as exc:
        raise BusinessRuleException(
            f"Attribution impossible: prix du bid incohérent avec l'enchère ({exc}).",
            reason="award_pricing_invalid",
        ) from exc
    frozen = snap.to_dict()
    frozen["award"] = {
        "auction_quantity": format(Decimal(str(auction.quantity)).normalize(), "f"),
        "auction_unit": str(auction.unit).upper(),
        "total_amount": format(total, "f"),
    }
    return total, frozen


def award_decision_for(
    bid: Any,
    auction: Any,
    *,
    producer_name: Optional[str] = None,
    expected_award: Optional[Dict[str, Any]] = None,
) -> CertifiedAwardDecision:
    """Décision d'attribution CERTIFIÉE d'un bid, ou refus explicite AVANT toute écriture.

    - bid sans base de prix certifiée (antérieur à B2a) -> refus `bid_basis_unknown` : jamais de « par
      tonne » ou « par kg » supposé ; le producteur doit requalifier son prix ;
    - `expected_award` (ce que l'acheteur a CONFIRMÉ) : si l'empreinte des termes actuels diffère
      (prix, base, quantité, total, producteur…) -> `award_terms_changed`, on n'attribue pas sur d'anciens termes."""
    view = bid_pricing_view(bid)
    try:
        decision = build_award_decision(
            auction_id=auction.id,
            bid_id=bid.id,
            producer_id=bid.producer_id,
            buyer_id=auction.buyer_id,
            producer_name=producer_name,
            pricing=view.snapshot,
            auction_quantity=auction.quantity,
            auction_unit=auction.unit,
        )
    except AwardNotPossible as exc:
        if exc.reason == "bid_basis_unknown":
            logger.warning("BID_LEGACY_BASIS_UNKNOWN | auction=%s | bid=%s", auction.id, bid.id)
        raise BusinessRuleException(exc.message, reason=exc.reason) from exc
    if expected_award is not None:
        expected_fp = str((expected_award or {}).get("fingerprint") or "")
        if expected_fp != decision.fingerprint:
            logger.warning(
                "BID_AWARD_TERMS_CHANGED | auction=%s | bid=%s | expected=%s | current=%s",
                auction.id, bid.id, expected_fp[:12], decision.fingerprint[:12],
            )
            raise BusinessRuleException(
                "Les termes de cette offre ont changé depuis votre confirmation (prix, base, quantité ou total). "
                "Consultez les offres à nouveau avant de retenir un gagnant.",
                reason="award_terms_changed",
            )
    return decision


def bid_display_fields(
    bid: Any, auction: Any, *, producer_name: Optional[str] = None, with_decision: bool = True
) -> Dict[str, Any]:
    """Champs d'AFFICHAGE d'un bid — sa propre sémantique de prix, JAMAIS `offered_price` + unité de
    l'enchère. `comparable_total` permet de classer sans jamais mélanger les bases ; un bid sans base est
    marqué `requires_requalification`."""
    cmp = compare_bid(bid, auction.quantity, auction.unit)
    view = bid_pricing_view(bid)
    fields: Dict[str, Any] = {
        "pricing_label": cmp.label,
        "price_basis": view.basis.value if view.basis else None,
        "pricing_reliability": cmp.reliability.value,
        "comparable_total": format(cmp.total, "f") if cmp.total is not None else None,
        "normalized_label": cmp.normalized,
        "requires_requalification": cmp.reason == "bid_basis_unknown",
        "pricing": view.snapshot.to_dict() if view.snapshot is not None else None,
        "award_decision": None,
    }
    if cmp.comparable and with_decision:
        try:
            fields["award_decision"] = award_decision_for(bid, auction, producer_name=producer_name).to_state()
        except BusinessRuleException:
            fields["award_decision"] = None
    return fields


__all__ = [
    "award_decision_for",
    "bid_display_fields",
    "certify_commercial_offer",
    "invalidate_commercial_pricing_on_edit",
    "order_item_snapshot_columns",
    "declared_sale_snapshot_columns",
    "bid_snapshot_columns",
    "reprice_bid_columns",
    "award_total_and_snapshot",
    "CommercialPricingSnapshot",
    "PriceBasis",
]
