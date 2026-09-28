"""Décision d'attribution CERTIFIÉE d'un appel d'offres (Phase B2b).

L'acheteur ne désigne jamais un gagnant sur un numéro de ligne : il confirme un objet FIGÉ portant
le producteur, le prix commercial, sa BASE, la quantité de l'enchère et le TOTAL dérivé. Cet objet est
ce que la confirmation affiche, ce que l'exécution revalide (empreinte) et ce qui est gelé sur la commande
(`orders.award_pricing_snapshot`). Aucune base n'est jamais redéduite au moment d'attribuer : un bid dont la
base est inconnue (bid antérieur à B2a) n'est PAS attribuable — il faut la requalifier auprès du producteur.

Module pur : aucune E/S.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from ladini.core.formatting import fmt_num
from ladini.domain.bid_pricing_flow import comparable_total, render_pricing_label
from ladini.domain.commercial_offer import PriceBasis, unit_display
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingReliability,
    PricingSnapshotError,
    bid_pricing_view,
    quantize_money,
    to_decimal,
)

DECISION_VERSION = 1


class AwardNotPossible(Exception):
    """Attribution refusée avant toute écriture. `reason` est un code stable (tests, messages, logs)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class BidBasisUnknown(AwardNotPossible):
    """Bid sans base de prix certifiée : on DEMANDE au producteur, on ne devine pas et on n'attribue pas."""

    def __init__(self) -> None:
        super().__init__(
            "bid_basis_unknown",
            "Le prix de cette offre n'indique pas s'il est par unité, par conditionnement ou pour l'ensemble. "
            "Le producteur doit d'abord préciser sa base de prix avant de pouvoir la retenir.",
        )


@dataclass(frozen=True)
class CertifiedAwardDecision:
    """Terme de l'attribution, figé. `fingerprint` en est la signature canonique."""

    auction_id: str
    bid_id: str
    producer_id: str
    buyer_id: str
    producer_name: str
    pricing: CommercialPricingSnapshot
    auction_quantity: Decimal
    auction_unit: str
    award_total: Decimal
    decision_version: int = DECISION_VERSION

    def _canonical(self) -> Dict[str, Any]:
        p = self.pricing.to_dict()
        return {
            "v": self.decision_version,
            "auction_id": self.auction_id,
            "bid_id": self.bid_id,
            "producer_id": self.producer_id,
            "buyer_id": self.buyer_id,
            "pricing": {
                k: p[k]
                for k in (
                    "schema_version", "currency", "commercial_price_amount", "price_basis", "price_unit",
                    "package_type", "package_content_amount", "package_content_unit",
                )
            },
            "auction_quantity": format(self.auction_quantity.normalize(), "f"),
            "auction_unit": self.auction_unit,
            "award_total": format(self.award_total.normalize(), "f"),
        }

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self._canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def idempotency_key(self) -> str:
        """Clé d'exécution : une même décision (mêmes termes) n'attribue qu'UNE fois ; des termes changés = une
        autre clé, donc jamais un rejeu silencieux sur d'anciens termes."""
        return f"award:{self.auction_id}:{self.bid_id}:{self.fingerprint[:16]}"

    def frozen_snapshot(self) -> Dict[str, Any]:
        """Contenu de `orders.award_pricing_snapshot` : le snapshot du bid + les termes de l'attribution."""
        frozen: Dict[str, Any] = self.pricing.to_dict()
        frozen["award"] = {
            "auction_id": self.auction_id,
            "bid_id": self.bid_id,
            "producer_id": self.producer_id,
            "auction_quantity": format(self.auction_quantity.normalize(), "f"),
            "auction_unit": self.auction_unit,
            "total_amount": format(self.award_total, "f"),
            "fingerprint": self.fingerprint,
            "decision_version": self.decision_version,
        }
        return frozen

    def to_state(self) -> Dict[str, Any]:
        """Forme JSON (working_memory) de la décision — ce que la confirmation gèle."""
        return {
            "auction_id": self.auction_id,
            "bid_id": self.bid_id,
            "producer_id": self.producer_id,
            "buyer_id": self.buyer_id,
            "producer_name": self.producer_name,
            "pricing": self.pricing.to_dict(),
            "auction_quantity": format(self.auction_quantity.normalize(), "f"),
            "auction_unit": self.auction_unit,
            "award_total": format(self.award_total, "f"),
            "decision_version": self.decision_version,
            "fingerprint": self.fingerprint,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_state(cls, data: Any) -> Optional["CertifiedAwardDecision"]:
        if not isinstance(data, Mapping):
            return None
        try:
            pricing = CommercialPricingSnapshot.from_dict(data.get("pricing"))
            if pricing is None:
                return None
            decision = cls(
                auction_id=str(data["auction_id"]),
                bid_id=str(data["bid_id"]),
                producer_id=str(data["producer_id"]),
                buyer_id=str(data["buyer_id"]),
                producer_name=str(data.get("producer_name") or "ce producteur"),
                pricing=pricing,
                auction_quantity=to_decimal(data["auction_quantity"]),
                auction_unit=str(data["auction_unit"]),
                award_total=to_decimal(data["award_total"]),
                decision_version=int(data.get("decision_version") or DECISION_VERSION),
            )
        except (KeyError, ValueError, PricingSnapshotError):
            return None
        # une décision dont l'empreinte stockée ne correspond plus à ses termes a été altérée : rejetée.
        if data.get("fingerprint") and data["fingerprint"] != decision.fingerprint:
            return None
        return decision

    def confirmation_text(self, product: str) -> str:
        """Ce que l'acheteur confirme : producteur, prix commercial + base, quantité, total. Projection PURE
        de la décision figée."""
        qty = float(self.auction_quantity)
        return (
            f"🤝 Vous sélectionnez *{self.producer_name}* pour *{product}*.\n\n"
            f"💰 Offre : *{render_pricing_label(self.pricing)}*\n"
            f"⚖️ Quantité : *{fmt_num(qty)} {unit_display(self.auction_unit, qty)}*\n"
            f"🧾 Total : *{fmt_num(float(self.award_total))} FCFA*\n\n"
            "⚠️ Cette action *clôture l'appel d'offres* et crée la commande."
        )


def build_award_decision(
    *,
    auction_id: Any,
    bid_id: Any,
    producer_id: Any,
    buyer_id: Any,
    producer_name: Optional[str],
    pricing: Optional[CommercialPricingSnapshot],
    auction_quantity: Any,
    auction_unit: str,
) -> CertifiedAwardDecision:
    """Construit la décision depuis le snapshot CERTIFIÉ du bid. Refuse, sans jamais déduire la base :
    - bid sans snapshot -> `BidBasisUnknown` ;
    - base incompatible avec l'enchère / conditionnement non divisible -> `AwardNotPossible`."""
    if pricing is None:
        raise BidBasisUnknown()
    try:
        total = pricing.total_for(auction_quantity, auction_unit)
    except PricingSnapshotError as exc:
        raise AwardNotPossible(
            "award_pricing_invalid",
            f"Le prix de cette offre ne peut pas être appliqué à l'enchère ({exc}).",
        ) from exc
    return CertifiedAwardDecision(
        auction_id=str(auction_id),
        bid_id=str(bid_id),
        producer_id=str(producer_id),
        buyer_id=str(buyer_id),
        producer_name=str(producer_name or "ce producteur"),
        pricing=pricing,
        auction_quantity=to_decimal(auction_quantity),
        auction_unit=str(auction_unit).upper(),
        award_total=quantize_money(total),
    )


# ---------------------------------------------------------------------------
# Comparaison des offres (jamais sur le montant brut)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BidComparison:
    bid_id: str
    label: str  # sémantique ORIGINALE du bid
    reliability: PricingReliability
    total: Optional[Decimal]  # total comparable, ou None
    normalized: Optional[str]  # « 450 FCFA/KG » — affiché EN SECOND, jamais à la place du label
    comparable: bool
    reason: Optional[str] = None


def compare_bid(bid: Any, auction_quantity: Any, auction_unit: str) -> BidComparison:
    """Sémantique + total comparable d'UN bid. Bid sans base : non comparable, à requalifier."""
    view = bid_pricing_view(bid)
    bid_id = str(getattr(bid, "id", "") or "")
    if view.snapshot is None:
        return BidComparison(
            bid_id=bid_id,
            label=f"{fmt_num(float(view.amount)) if view.amount is not None else '?'} FCFA (base de prix inconnue)",
            reliability=view.reliability,
            total=None,
            normalized=None,
            comparable=False,
            reason="bid_basis_unknown",
        )
    snap = view.snapshot
    total = comparable_total(snap, auction_quantity, auction_unit)
    normalized = None
    if snap.normalized_unit_price is not None and snap.normalized_unit:
        normalized = f"{fmt_num(float(snap.normalized_unit_price))} FCFA/{unit_display(snap.normalized_unit)}"
    return BidComparison(
        bid_id=bid_id,
        label=render_pricing_label(snap),
        reliability=PricingReliability.CERTIFIED,
        total=total,
        normalized=normalized,
        comparable=total is not None,
        reason=None if total is not None else "not_comparable_to_auction",
    )


def rank_comparisons(items: List[BidComparison]) -> List[BidComparison]:
    """Moins cher d'abord parmi les comparables ; les non-comparables VIENNENT APRÈS (jamais classés au hasard)."""
    comparable = sorted((i for i in items if i.comparable and i.total is not None), key=lambda i: i.total)  # type: ignore[arg-type,return-value]
    rest = [i for i in items if not i.comparable]
    return comparable + rest


__all__ = [
    "DECISION_VERSION",
    "AwardNotPossible",
    "BidBasisUnknown",
    "CertifiedAwardDecision",
    "BidComparison",
    "build_award_decision",
    "compare_bid",
    "rank_comparisons",
    "PriceBasis",
]
