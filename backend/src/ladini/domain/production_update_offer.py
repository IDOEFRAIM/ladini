"""Correction de prix d'une PRODUCTION FUTURE certifiée (Phase B2c.7).

## Le bug fermé ici

`PRODUCTION_UPDATE_FUTURE` (`flows/producer/flow.py::_resolve_cycle_for_update`) extrayait le prix
d'une correction avec une regex nue (« prix|coûte|vaut|N fcfa ») puis `update_production_fields`
écrivait `MarketOffer.price_per_unit = <float brut>` — sans base ni provenance — exactement la
classe de bug déjà fermée pour les bids (B2b), `PROCUREMENT_CREATE_REQUEST` (B2c.5) et la
négociation (B2c.6). Sur un lot de 10 TONNE, « 4 millions » pouvait devenir 4 000 000 FCFA/TONNE
(une erreur ×10) au lieu d'un budget de 4 000 000 FCFA pour tout le lot.

Pire : cette écriture ne touchait JAMAIS `MarketOffer.pricing_snapshot` (le snapshot certifié posé
par `declare_future_production`, B2c.3). Après une « correction », la colonne brute portait le
nouveau prix (peut-être faux) pendant que le snapshot affirmait encore l'ANCIEN prix/base — et la
recherche acheteur (`market_offer_pricing_view`, B2c.4) lit le snapshot en priorité : l'acheteur
voyait le prix périmé.

Ce module ne réinvente aucun moteur : il réutilise `bid_pricing_flow.parse_bid_price` (extraction
montant/base/provenance) contre la quantité/unité DU LOT, et `BidPriceParse.snapshot(...)` pour le
`CommercialPricingSnapshot`. Il gèle le résultat dans un objet immuable (empreinte, clé
d'idempotence) que le producteur confirme, et dont le service DB re-dérive le snapshot + la colonne
legacy `price_per_unit` — jamais un float fourni « à la main ».

`PER_PACKAGE` est refusé (fail-closed, comme `declare_future_production`) : `MarketOffer` n'a ni
conditionnement ni paliers.

Module pur : aucune E/S.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from ladini.core.formatting import fmt_num
from ladini.domain.commercial_offer import PriceBasis, unit_display
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingSnapshotError,
    quantize_money,
    quantize_normalized,
    render_pricing_label,
    to_decimal,
)

OFFER_VERSION = 1


class ProductionPriceNotCertifiable(Exception):
    """Le prix analysé ne peut pas s'appliquer à ce lot (conditionnement non supporté, unité
    incompatible…) — refusé AVANT toute écriture, jamais deviné."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class CertifiedProductionPriceCorrection:
    """Prix corrigé FIGÉ d'un lot : ce que le producteur confirme est ce qui est persisté."""

    cycle_id: str
    product_label: str
    pricing: CommercialPricingSnapshot
    quantity: Decimal
    unit: str
    total: Decimal
    price_per_unit: Decimal
    offer_version: int = OFFER_VERSION

    def _canonical(self) -> Dict[str, Any]:
        p = self.pricing.to_dict()
        return {
            "v": self.offer_version,
            "cycle_id": self.cycle_id,
            "pricing": {
                k: p[k]
                for k in (
                    "schema_version", "currency", "commercial_price_amount", "price_basis",
                    "price_unit", "package_type", "package_content_amount", "package_content_unit",
                )
            },
            "quantity": format(self.quantity.normalize(), "f"),
            "unit": self.unit,
            "total": format(self.total.normalize(), "f"),
        }

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self._canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def idempotency_key(self) -> str:
        return f"production_price:{self.cycle_id}:{self.fingerprint[:16]}"

    def to_state(self) -> Dict[str, Any]:
        """Forme JSON (`working_memory.update_pending`) — ce que la confirmation gèle."""
        return {
            "cycle_id": self.cycle_id,
            "product_label": self.product_label,
            "pricing": self.pricing.to_dict(),
            "quantity": format(self.quantity.normalize(), "f"),
            "unit": self.unit,
            "total": format(self.total, "f"),
            "price_per_unit": format(self.price_per_unit, "f"),
            "offer_version": self.offer_version,
            "fingerprint": self.fingerprint,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_state(cls, data: Any) -> Optional["CertifiedProductionPriceCorrection"]:
        if not isinstance(data, Mapping):
            return None
        try:
            pricing = CommercialPricingSnapshot.from_dict(data.get("pricing"))
            if pricing is None:
                return None
            offer = cls(
                cycle_id=str(data["cycle_id"]),
                product_label=str(data.get("product_label") or "ce lot"),
                pricing=pricing,
                quantity=to_decimal(data["quantity"]),
                unit=str(data["unit"]),
                total=to_decimal(data["total"]),
                price_per_unit=to_decimal(data["price_per_unit"]),
                offer_version=int(data.get("offer_version") or OFFER_VERSION),
            )
        except (KeyError, ValueError, PricingSnapshotError):
            return None
        # empreinte stockée ≠ termes recalculés : l'état a été altéré, l'offre est rejetée
        if data.get("fingerprint") and data["fingerprint"] != offer.fingerprint:
            return None
        return offer

    def recap_line(self) -> str:
        """Ligne de récap : la base DITE par le producteur, jamais un « FCFA/unité » qu'il n'a pas dit."""
        qty = float(self.quantity)
        label = f"{render_pricing_label(self.pricing)}"
        if self.pricing.price_basis == PriceBasis.TOTAL_LOT:
            return (
                f"- Nouveau prix : {label} "
                f"(soit {fmt_num(float(self.total))} FCFA pour les {fmt_num(qty)} {unit_display(self.unit, qty)})"
            )
        return f"- Nouveau prix : {label} (soit {fmt_num(float(self.total))} FCFA pour {fmt_num(qty)} {unit_display(self.unit, qty)})"


def build_production_price_correction(
    *,
    cycle_id: str,
    product_label: str,
    pricing: CommercialPricingSnapshot,
    quantity: Any,
    unit: str,
) -> CertifiedProductionPriceCorrection:
    """Construit la correction certifiée depuis un snapshot déjà RÉSOLU (`BidPriceParse.snapshot(...)`).

    `total_for` porte déjà le refus d'une unité incompatible ; ce module en dérive le prix par unité
    legacy de `MarketOffer.price_per_unit` (Decimal, jamais un float)."""
    if pricing.price_basis == PriceBasis.PER_PACKAGE:
        raise ProductionPriceNotCertifiable(
            "production_package_price_unsupported",
            "Le prix par conditionnement (« la caisse de 25 kg ») n'est pas pris en charge pour une "
            "production future. Indiquez un prix par unité ou pour l'ensemble du lot.",
        )
    try:
        total = pricing.total_for(quantity, unit)
    except PricingSnapshotError as exc:
        raise ProductionPriceNotCertifiable(
            "production_pricing_invalid", f"Ce prix ne peut pas être appliqué à ce lot ({exc})."
        ) from exc
    qty = to_decimal(quantity, field="quantity")
    return CertifiedProductionPriceCorrection(
        cycle_id=str(cycle_id),
        product_label=str(product_label or "ce lot"),
        pricing=pricing,
        quantity=qty,
        unit=str(unit).upper(),
        total=quantize_money(total),
        price_per_unit=quantize_normalized(total / qty),
    )


__all__ = [
    "OFFER_VERSION",
    "ProductionPriceNotCertifiable",
    "CertifiedProductionPriceCorrection",
    "build_production_price_correction",
]
