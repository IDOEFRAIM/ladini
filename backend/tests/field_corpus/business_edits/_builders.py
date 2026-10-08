"""Ce que le MODÈLE doit comprendre (mode scripté) — jamais le comportement du domaine, qui reste réel."""
from __future__ import annotations

from typing import Any, Dict


def edit(spec: Dict[str, Any], *, correction: bool = False) -> Dict[str, Any]:
    ents: Dict[str, Any] = {"cart_edit": spec}
    if correction:
        ents["is_correction"] = True
    return {"new_task": {"disposition": "NEW_TASK", "intent": "BUYER_EDIT_CART", "confidence": 0.9, "entities": ents}}


def sale(entities: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "new_task": {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {**entities, "is_correction": True},
        }
    }


def refine(**kw: Any) -> Dict[str, Any]:
    return {"disposition": "ACTION", "action": "SELECT_PRODUCER", "reference": {"reference_type": "REFINEMENT", **kw}}


def objection_price() -> Dict[str, Any]:
    return refine(objection="PRICE")
