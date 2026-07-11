from __future__ import annotations

"""Contract validation node to enforce post-LLM payload schemas."""

from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.interpreter.contracts import enforce_contract

_FIELD_TO_EXPECTED = {
    "product": "PRODUCT",
    "product_name": "PRODUCT",
    "quantity": "QUANTITY",
    "unit": "UNIT",
    "price": "PRICE",
    "order_id": "SELECTION",
    "phone": None,
}


def _expected_from_field(field: Optional[str], fallback: str) -> str:
    if not field:
        return fallback
    return _FIELD_TO_EXPECTED.get(field, fallback)


async def contract_validation(state: Dict[str, Any], _mc_runtime: Any = None, **_: Any) -> Dict[str, Any]:
    intent = str(state.get("current_goal") or state.get("detected_intent") or "").upper()
    if not intent:
        return {"contract_validation_failed": False}

    payload = state.get("transaction_payload") or {}
    success, message, field = enforce_contract(intent, payload)
    if success:
        return {
            "contract_validation_failed": False,
        }

    expected_input = _expected_from_field(field, str(state.get("expected_input") or "NONE").upper())
    final_response = (
        "❗️ Certaines informations manquent ou sont invalides. "
        + (message or "Pouvez-vous préciser les détails requis ?")
    )
    return {
        "status": "WAITING_INPUT",
        "expected_input": expected_input,
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": final_response,
        "contract_validation_failed": True,
    }


__all__ = ["contract_validation"]
