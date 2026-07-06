"""Action handlers for the Procurement domain."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg

@register_action("PROCUREMENT_CREATE_REQUEST", mode="WRITE")
def prep_procurement_create_request(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    product = str(require(payload, "product"))
    qty_raw = float(require(payload, "quantity_mentioned"))
    price = float(require(payload, "price_mentioned"))
    qty_kg, unit = normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    
    # 1. Gestion de la date limite
    deadline = payload.get("deadline")
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError:
            deadline = None
    if not isinstance(deadline, datetime) or deadline <= datetime.now():
        deadline = datetime.now() + timedelta(days=30)

    # 2. Extraction des nouveaux champs obligatoires (Logistique)
    delivery_location = payload.get("delivery_location") or payload.get("location") or "Non spécifié"
    
    delivery_deadline = payload.get("delivery_deadline")
    if isinstance(delivery_deadline, str):
        try:
            delivery_deadline = datetime.fromisoformat(delivery_deadline)
        except Exception:  # noqa: BLE001
            delivery_deadline = None
    if not isinstance(delivery_deadline, datetime):
        delivery_deadline = deadline + timedelta(days=3)  # Par défaut : 3 jours après la deadline

    args: Dict[str, Any] = {
        "phone": phone,
        "product_query": product,
        "qty": qty_kg,
        "unit": unit,
        "max_price": price,
        "deadline": deadline,
        "delivery_location": delivery_location,        # Ajouté
        "delivery_deadline": delivery_deadline,        # Ajouté
        "incoterm": payload.get("incoterm", "DDP"),    # Ajouté avec valeur par défaut
        "auto_extend": bool(payload.get("auto_extend", True)),
    }
    
    zone = payload.get("zone_name") or payload.get("zone")
    if zone:
        args["zone_query"] = str(zone)
        
    return "create_auction", args


@register_action("PROCUREMENT_SELECT_WINNER", mode="WRITE")
def prep_procurement_select_winner(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    require(payload, "auction_id")
    bid_id = str(require(payload, "bid_id"))
    return "select_winning_bid", {"bid_id": bid_id}


@register_action("PROCUREMENT_ACCEPT_OFFER", mode="WRITE")
def prep_procurement_accept_offer(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    bid_id = str(require(payload, "bid_id"))
    return "accept_bid", {"bid_id": bid_id}
