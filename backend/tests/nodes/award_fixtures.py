"""Fixtures d'attribution CERTIFIÉE pour les tests de tunnel acheteur (Phase B2b).

Un tunnel d'attribution ne travaille plus sur un prix brut : il confirme et exécute une
`CertifiedAwardDecision`. Ces fabriques produisent (a) la décision figée telle qu'elle vit dans
`working_memory.pending_award`, et (b) la ligne de bid telle que la renvoie `get_auction_bids`."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from ladini.domain.bid_award import CertifiedAwardDecision, build_award_decision
from ladini.domain.bid_pricing_flow import render_pricing_label
from ladini.domain.commercial_pricing_snapshot import build_bid_pricing_snapshot

AUCTION_ID = "a1"
BID_ID = "b1"


def decision(
    amount: Any = 250,
    *,
    basis: str = "PER_BASE_UNIT",
    unit: str = "TONNE",
    quantity: Any = 10,
    bid_id: str = BID_ID,
    auction_id: str = AUCTION_ID,
    producer: str = "Awa",
) -> CertifiedAwardDecision:
    snap = build_bid_pricing_snapshot(
        amount=amount, basis=basis, price_unit=unit if basis == "PER_BASE_UNIT" else None,
        auction_quantity=quantity, auction_unit=unit, source="TEST_FIXTURE",
    )
    return build_award_decision(
        auction_id=auction_id, bid_id=bid_id, producer_id="p1", buyer_id="buyer-1", producer_name=producer,
        pricing=snap, auction_quantity=quantity, auction_unit=unit,
    )


def bid_row(
    amount: Any = 250,
    *,
    status: str = "PENDING",
    legacy: bool = False,
    **decision_kwargs: Any,
) -> Dict[str, Any]:
    """Ligne de bid telle que `get_auction_bids` la renvoie (sémantique de prix + décision certifiée)."""
    bid_id = decision_kwargs.get("bid_id", BID_ID)
    producer = decision_kwargs.get("producer", "Awa")
    if legacy:
        return {
            "bid_id": bid_id, "producer": producer, "price": float(amount), "status": status,
            "pricing_label": f"{amount} FCFA (base de prix inconnue)", "price_basis": None,
            "requires_requalification": True, "comparable_total": None, "award_decision": None,
            "pricing_reliability": "UNKNOWN_BASIS",
        }
    d = decision(amount, **decision_kwargs)
    return {
        "bid_id": bid_id, "producer": producer, "price": float(amount), "status": status,
        "pricing_label": render_pricing_label(d.pricing), "price_basis": d.pricing.price_basis.value,
        "requires_requalification": False, "comparable_total": format(d.award_total, "f"),
        "award_decision": d.to_state(), "pricing_reliability": "CERTIFIED",
    }


def bids_response(*rows: Dict[str, Any], product: str = "riz", status: str = "OPEN") -> Dict[str, Any]:
    return {"status": "success", "bids": list(rows), "auction": {"product": product, "status": status}}


def frozen_state(amount: Any = 250, **kwargs: Any) -> Dict[str, Any]:
    """`working_memory.pending_award` d'une confirmation déjà faite."""
    return decision(amount, **kwargs).to_state()


__all__ = ["decision", "bid_row", "bids_response", "frozen_state", "AUCTION_ID", "BID_ID", "Decimal", "Optional"]
