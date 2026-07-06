from __future__ import annotations

from typing import Any, Dict, Optional, List
from pydantic import BaseModel


class GenericResult(BaseModel):
    status: str
    data: Optional[Any] = None
    message: Optional[str] = None


class PrepareTransactionInput(BaseModel):
    product_id: str
    quantity_kg: float
    price_fcfa_per_unit: float
    buyer_phone: str
    zone_id: Optional[str] = None
    source: Optional[str] = "WHATSAPP"


class UpdateStockInput(BaseModel):
    farm_id: str
    item_name: str
    quantity_change: float
    reason: str


class CreateAgentActionInput(BaseModel):
    agent_name: str
    action_type: str
    payload: Dict[str, Any]
    user_id: Optional[str] = None
    priority: Optional[str] = "MEDIUM"
    order_id: Optional[str] = None
    ai_reasoning: Optional[str] = None


class LogAuditInput(BaseModel):
    actor_id: str
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    old_value: Optional[Dict[str, Any]] = None
    new_value: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None


class CreateProductInput(BaseModel):
    producer_id: str
    name: str
    price: float
    quantity_for_sale: float
    unit: Optional[str] = "KG"
    category_label: Optional[str] = "Céréales"
    sub_category_id: Optional[str] = None
    description: Optional[str] = None
    local_names: Optional[List[str]] = None


class CreateAuctionInput(BaseModel):
    buyer_id: str
    sub_category_id: str
    quantity: float
    max_price_per_unit: float
    deadline: str
    unit: Optional[str] = "TONNE"
    target_zone_id: Optional[str] = None


def _wrap(res: Any) -> str:
    """Wrap a service result into a GenericResult JSON string."""
    if isinstance(res, dict) and (
        res.get("error")
        or res.get("status") in ("FAILED", "REJECTED", "NOT_FOUND", "ALREADY_HANDLED")
    ):
        g = GenericResult(
            status="error",
            data=res,
            message=res.get("message") or res.get("error"),
        )
    else:
        g = GenericResult(status="ok", data=res)
    return g.model_dump_json(indent=2)
