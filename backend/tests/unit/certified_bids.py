"""Bids CERTIFIÉS pour les doublures de test (Phase B2b).

Depuis B2b un nouveau bid porte sa base de prix et un bid sans base n'est jamais attribuable. Les
doublures de session historiques fabriquaient des bids `offered_price`-seul ; ce helper leur ajoute les
colonnes du contrat (`PER_BASE_UNIT` sur l'unité de l'enchère) SANS changer ce que chaque test prouve."""
from __future__ import annotations

from typing import Any, Dict

from ladini.domain.commercial_pricing_snapshot import build_bid_pricing_snapshot


def certified_bid_columns(
    amount: Any, *, unit: str = "TONNE", quantity: Any = 10, basis: str = "PER_BASE_UNIT"
) -> Dict[str, Any]:
    """Colonnes `bids.*` d'un bid « <amount> par <unit> » sur une enchère de <quantity> <unit>."""
    return build_bid_pricing_snapshot(
        amount=amount,
        basis=basis,
        price_unit=unit if basis == "PER_BASE_UNIT" else None,
        auction_quantity=quantity,
        auction_unit=unit,
        source="TEST_FIXTURE",
    ).to_bid_columns()
