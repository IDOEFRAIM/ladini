from __future__ import annotations

"""Post-LLM contract validation for critical intents (buyer + producer).

Défense en profondeur exécutée par ``nodes/validation.py::validator`` (câblé
Phase 2) : après le contrôle de complétude d'INTENT_CONFIG, le payload est
soumis au contrat Pydantic du goal courant.

RÈGLE DE PARTAGE DES RESPONSABILITÉS :
- la PRÉSENCE des champs est le travail exclusif d'``INTENT_CONFIG.required``
  (+ resolvers/tunnels qui complètent les IDs plus tard) ;
- les contrats ne valident que les VALEURS PRÉSENTES (type, bornes, longueur).
``enforce_contract`` ignore donc les erreurs Pydantic de type ``missing`` —
sinon un contrat exigeant ``order_id`` casserait le flux « liste puis
sélectionne » d'order_tracking.
"""

from typing import Any, Dict, Optional, Tuple, Type

from pydantic import BaseModel, Field, ValidationError, model_validator


def _coerce_number(data: Dict[str, Any], key: str) -> None:
    value = data.get(key)
    if value is None:
        return
    try:
        data[key] = float(value)
    except (TypeError, ValueError):
        pass


class AddToCartContract(BaseModel):
    product: str = Field(..., min_length=2)
    quantity: float = Field(..., gt=0)
    unit: Optional[str] = None
    phone: str = Field(..., min_length=8)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        product = data.get("product")
        if product:
            data["product"] = str(product)
        _coerce_number(data, "quantity")
        return data


class NegotiationContract(BaseModel):
    product: str = Field(..., min_length=2)
    price: float = Field(..., gt=0)
    phone: str = Field(..., min_length=8)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        _coerce_number(data, "price")
        return data


class ListOrdersContract(BaseModel):
    phone: str = Field(..., min_length=8)


class OrderStatusContract(ListOrdersContract):
    order_id: str = Field(..., min_length=6)


class PublishProductContract(BaseModel):
    """SALES_PUBLISH_PRODUCT / SALES_RECORD_DIRECT — publication & vente directe."""
    product: str = Field(..., min_length=2, max_length=80)
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    phone: str = Field(..., min_length=8)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        product = data.get("product")
        if product:
            data["product"] = str(product)
        _coerce_number(data, "quantity")
        _coerce_number(data, "price")
        return data


CONTRACTS: Dict[str, Type[BaseModel]] = {
    # Buyer
    "BUYER_ADD_TO_CART": AddToCartContract,
    "BUYER_NEGOTIATE_PRICE": NegotiationContract,
    "BUYER_LIST_ORDERS": ListOrdersContract,
    "BUYER_CHECK_ORDER_STATUS": OrderStatusContract,
    # Producer
    "SALES_PUBLISH_PRODUCT": PublishProductContract,
    "SALES_RECORD_DIRECT": PublishProductContract,
}


def enforce_contract(intent: str, payload: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Valide `payload` contre le contrat de `intent`.

    Retourne ``(ok, message, champ_fautif)``. Les erreurs de PRÉSENCE
    (type ``missing``) sont ignorées — voir docstring module.
    """
    model = CONTRACTS.get(intent)
    if not model:
        return True, None, None
    try:
        model(**payload)
        return True, None, None
    except ValidationError as exc:
        value_errors = [e for e in exc.errors() if e.get("type") != "missing"]
        if not value_errors:
            return True, None, None
        first_error = value_errors[0]
        field = ".".join(str(p) for p in first_error.get("loc", []) if isinstance(p, str)) or None
        msg = first_error.get("msg") or "Entrée invalide."
        return False, msg, field


__all__ = ["enforce_contract", "CONTRACTS"]
