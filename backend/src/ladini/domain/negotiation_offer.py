"""Prix de négociation acheteur CERTIFIÉ — le plafond qu'une `Auction` de négociation directe
persiste (Phase B2c.6).

## Le bug fermé ici

`negotiation.py` (`initiate_negotiation_session` / `update_negotiation_offer`) écrivait
`Auction.max_price_per_unit` depuis un float brut extrait du message, sans base ni provenance —
exactement la classe de bug déjà fermée pour les bids (B2b, `bid_pricing_flow.py`) et pour
`PROCUREMENT_CREATE_REQUEST` (B2c.5). Sur une enchère de 10 TONNE, « 4 millions » pouvait devenir
4 000 000 FCFA/TONNE (une erreur ×10) au lieu d'un budget de 4 000 000 FCFA pour tout le lot.

Ce module ne réinvente aucun moteur : il réutilise `bid_pricing_flow.parse_bid_price` (même
extraction montant/base/provenance que les bids) contre la quantité/unité de LA négociation, puis
dérive le plafond PAR UNITÉ que `Auction.max_price_per_unit` peut effectivement stocker — aucune
migration `Auction.pricing_snapshot` n'existe, donc la base ORIGINALE (TOTAL_LOT vs PER_BASE_UNIT)
n'est jamais persistée telle quelle : elle reste vivante dans l'état transactionnel certifié
(`negotiation_context`, gelé comme `CertifiedAwardDecision` l'est déjà pour l'attribution d'un bid)
et dans la confirmation affichée — jamais mentie à l'utilisateur en « X FCFA/TONNE » quand il a dit
« X FCFA pour tout ».

Module pur : aucune E/S.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from ladini.core.formatting import fmt_num
from ladini.domain.bid_pricing_flow import render_pricing_label
from ladini.domain.commercial_offer import PriceBasis, unit_display
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingSnapshotError,
    quantize_money,
    quantize_normalized,
    to_decimal,
)

OFFER_VERSION = 1


class NegotiationPriceNotCertifiable(Exception):
    """Le prix analysé ne peut pas être appliqué à cette enchère (conditionnement non divisible,
    unité incompatible…) — refusé AVANT toute écriture, jamais deviné."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class CertifiedNegotiationOffer:
    """Prix de négociation FIGÉ : ce que l'acheteur confirme est ce qui est persisté.

    `auction_id` est `None` tant que l'enchère n'existe pas encore (ouverture d'une négociation) ;
    rempli pour une correction de prix sur une négociation déjà ouverte (contre-offre)."""

    product_id: str
    product_name: str
    buyer_phone: str
    pricing: CommercialPricingSnapshot
    auction_quantity: Decimal
    auction_unit: str
    total: Decimal
    ceiling_per_unit: Decimal
    auction_id: Optional[str] = None
    offer_version: int = OFFER_VERSION

    def _canonical(self) -> Dict[str, Any]:
        p = self.pricing.to_dict()
        return {
            "v": self.offer_version,
            "product_id": self.product_id,
            "buyer_phone": self.buyer_phone,
            "auction_id": self.auction_id,
            "pricing": {
                k: p[k]
                for k in (
                    "schema_version", "currency", "commercial_price_amount", "price_basis",
                    "price_unit", "package_type", "package_content_amount", "package_content_unit",
                )
            },
            "auction_quantity": format(self.auction_quantity.normalize(), "f"),
            "auction_unit": self.auction_unit,
            "total": format(self.total.normalize(), "f"),
        }

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self._canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def idempotency_key(self) -> str:
        """Une même offre (mêmes termes) ne s'écrit qu'UNE fois ; des termes changés = une autre
        clé, donc jamais un rejeu silencieux d'anciens termes sur un double « oui »/retry."""
        return f"negotiation_offer:{self.buyer_phone}:{self.fingerprint[:16]}"

    def to_state(self) -> Dict[str, Any]:
        """Forme JSON (`negotiation_context`) — ce que la confirmation gèle."""
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "buyer_phone": self.buyer_phone,
            "pricing": self.pricing.to_dict(),
            "auction_quantity": format(self.auction_quantity.normalize(), "f"),
            "auction_unit": self.auction_unit,
            "total": format(self.total, "f"),
            "ceiling_per_unit": format(self.ceiling_per_unit, "f"),
            "auction_id": self.auction_id,
            "offer_version": self.offer_version,
            "fingerprint": self.fingerprint,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_state(cls, data: Any) -> Optional["CertifiedNegotiationOffer"]:
        if not isinstance(data, Mapping):
            return None
        try:
            pricing = CommercialPricingSnapshot.from_dict(data.get("pricing"))
            if pricing is None:
                return None
            offer = cls(
                product_id=str(data["product_id"]),
                product_name=str(data.get("product_name") or "ce produit"),
                buyer_phone=str(data["buyer_phone"]),
                pricing=pricing,
                auction_quantity=to_decimal(data["auction_quantity"]),
                auction_unit=str(data["auction_unit"]),
                total=to_decimal(data["total"]),
                ceiling_per_unit=to_decimal(data["ceiling_per_unit"]),
                auction_id=str(data["auction_id"]) if data.get("auction_id") else None,
                offer_version=int(data.get("offer_version") or OFFER_VERSION),
            )
        except (KeyError, ValueError, PricingSnapshotError):
            return None
        # une offre dont l'empreinte stockée ne correspond plus à ses termes a été altérée : rejetée
        # (voir Golden F : `transaction_payload`/état brut corrompu entre confirmation et confirm).
        if data.get("fingerprint") and data["fingerprint"] != offer.fingerprint:
            return None
        return offer

    def confirmation_text(self, *, intro: str) -> str:
        """Ce que l'acheteur confirme, jamais une base réinterprétée. Projection PURE de l'offre
        figée : TOTAL_LOT affiche le budget total, jamais un prix/unité qu'il n'a pas dit."""
        qty = float(self.auction_quantity)
        lines = [intro, "", f"💰 {render_pricing_label(self.pricing)}"]
        if self.pricing.price_basis == PriceBasis.TOTAL_LOT:
            lines.append(
                f"⚖️ Budget maximal total : *{fmt_num(float(self.total))} FCFA* pour l'ensemble "
                f"des *{fmt_num(qty)} {unit_display(self.auction_unit, qty)}*."
            )
        else:
            lines.append(f"⚖️ Quantité : *{fmt_num(qty)} {unit_display(self.auction_unit, qty)}*")
            lines.append(f"🧾 Budget correspondant : *{fmt_num(float(self.total))} FCFA*")
        return "\n".join(lines)


def build_negotiation_offer(
    *,
    product_id: str,
    product_name: str,
    buyer_phone: str,
    pricing: CommercialPricingSnapshot,
    auction_quantity: Any,
    auction_unit: str,
    auction_id: Optional[str] = None,
) -> CertifiedNegotiationOffer:
    """Construit l'offre certifiée depuis un snapshot déjà RÉSOLU (`BidPriceParse.snapshot(...)`).

    `total_for` porte déjà le refus d'un conditionnement non divisible ou d'une unité incompatible —
    ce module ne fait qu'en dériver le plafond par unité, jamais une nouvelle règle de prix."""
    try:
        total = pricing.total_for(auction_quantity, auction_unit)
    except PricingSnapshotError as exc:
        raise NegotiationPriceNotCertifiable(
            "negotiation_pricing_invalid",
            f"Ce prix ne peut pas être appliqué à cette quantité ({exc}).",
        ) from exc
    qty = to_decimal(auction_quantity, field="quantity")
    ceiling = quantize_normalized(total / qty)
    return CertifiedNegotiationOffer(
        product_id=str(product_id),
        product_name=str(product_name or "ce produit"),
        buyer_phone=str(buyer_phone),
        pricing=pricing,
        auction_quantity=qty,
        auction_unit=str(auction_unit).upper(),
        total=quantize_money(total),
        ceiling_per_unit=ceiling,
        auction_id=str(auction_id) if auction_id else None,
    )


__all__ = [
    "OFFER_VERSION",
    "NegotiationPriceNotCertifiable",
    "CertifiedNegotiationOffer",
    "build_negotiation_offer",
]
