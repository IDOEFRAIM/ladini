from __future__ import annotations

"""Post-LLM contract validation for critical buyer intents."""

from typing import Any, Dict, Optional, Tuple, Type

from pydantic import BaseModel, Field, ValidationError, model_validator


class AddToCartContract(BaseModel):
    product: str = Field(..., min_length=2)
    quantity_mentioned: float = Field(..., gt=0)
    unit_mentioned: Optional[str]
    phone: str = Field(..., min_length=8)

    @model_validator(mode="before")
    @classmethod
    def _coerce_product(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        product = data.get("product") or data.get("product_name")
        if product:
            data["product"] = str(product)
        quantity = data.get("quantity_mentioned") or data.get("quantity")
        if quantity is not None:
            try:
                data["quantity_mentioned"] = float(quantity)
            except (TypeError, ValueError):
                pass
        return data


class NegotiationContract(BaseModel):
    product: str = Field(..., min_length=2)
    price_mentioned: float = Field(..., gt=0)
    phone: str = Field(..., min_length=8)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        if data.get("price") and not data.get("price_mentioned"):
            data["price_mentioned"] = data["price"]
        if data.get("product_name") and not data.get("product"):
            data["product"] = data["product_name"]
        return data


class ListOrdersContract(BaseModel):
    phone: str = Field(..., min_length=8)


class OrderStatusContract(ListOrdersContract):
    order_id: str = Field(..., min_length=6)


CONTRACTS: Dict[str, Type[BaseModel]] = {
    "BUYER_ADD_TO_CART": AddToCartContract,
    "BUYER_NEGOTIATE_PRICE": NegotiationContract,
    "BUYER_LIST_ORDERS": ListOrdersContract,
    "BUYER_CHECK_ORDER_STATUS": OrderStatusContract,
}


def enforce_contract(intent: str, payload: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    model = CONTRACTS.get(intent)
    if not model:
        return True, None, None
    try:
        model(**payload)
        return True, None, None
    except ValidationError as exc:  # pragma: no cover - formatting only
        first_error = exc.errors()[0]
        field = ".".join(str(p) for p in first_error.get("loc", []) if isinstance(p, str)) or None
        msg = first_error.get("msg") or "Entrée invalide."
        return False, msg, field


__all__ = ["enforce_contract", "CONTRACTS"]
