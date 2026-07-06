from typing import Any, Dict, Optional, Tuple

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger, _READ_GOALS
from agriconnect.graphs.agents.market_coach.utils import canonical_unit_label

logger = get_node_logger("ConfirmationGateNode")


def _fmt_num(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _resolve_units(payload: Dict[str, Any], default_unit: str = "KG") -> Tuple[str, str]:
    conversion = payload.get("unit_conversion") or {}
    converted_unit = payload.get("unit_mentioned") or conversion.get("to_unit") or default_unit
    display_unit = payload.get("unit_display") or conversion.get("from_unit") or converted_unit
    canonical_display = canonical_unit_label(display_unit, canonical_unit_label(default_unit))
    canonical_converted = canonical_unit_label(converted_unit, canonical_unit_label(default_unit))
    return canonical_display, canonical_converted


def _format_quantity(payload: Dict[str, Any], default_unit: str = "KG") -> Optional[str]:
    converted_qty = payload.get("quantity_mentioned")
    if converted_qty in (None, "", [], {}):
        return None

    display_qty = payload.get("quantity_display")
    if display_qty in (None, "", [], {}):
        display_qty = payload.get("original_quantity_mentioned")

    display_unit, converted_unit = _resolve_units(payload, default_unit)

    if display_qty not in (None, "", [], {}):
        return f"{_fmt_num(display_qty)} {display_unit}".strip()

    return f"{_fmt_num(converted_qty)} {converted_unit}".strip()


def _build_confirmation_summary(goal: str, payload: Dict[str, Any]) -> str:
    if goal == "DECLARE_CROP_CYCLE":
        production_type = str(payload.get("production_type") or "CROP").upper()
        product = payload.get("product") or payload.get("species") or "production"
        default_unit = payload.get("unit") or ("KG" if production_type == "CROP" else "HEAD")
        quantity_line = _format_quantity(payload, default_unit)
        display_unit, converted_unit = _resolve_units(payload, default_unit)
        price = payload.get("price_mentioned") or payload.get("price_per_unit")
        eta = payload.get("estimated_available_at") or payload.get("expected_harvest_date")
        farm = payload.get("farm_name") or payload.get("farm_id")

        lines = [
            f"Type : {production_type}",
            f"Produit : {product}",
            f"Quantité prévue : {quantity_line}" if quantity_line else None,
            (
                f"Prix unitaire : {_fmt_num(price)} FCFA/{display_unit or converted_unit}"
                if price not in (None, "", [], {})
                else None
            ),
            f"Disponible vers : {eta}" if eta not in (None, "", [], {}) else None,
            f"Exploitation : {farm}" if farm not in (None, "", [], {}) else None,
        ]
        bullet_list = "\n".join(f"- {line}" for line in lines if line)
        summary = "Déclaration d'un lot futur"
        return f"{summary} :\n{bullet_list}" if bullet_list else summary

    product = payload.get("product")
    price = payload.get("price_mentioned")
    quantity_line = _format_quantity(payload)
    display_unit, converted_unit = _resolve_units(payload)
    price_unit = display_unit or converted_unit
    price_unit = canonical_unit_label(price_unit)
    price_fmt = _fmt_num(price) if price not in (None, "", [], {}) else None

    mapping = {
        "SALES_PUBLISH_PRODUCT": (
            f"Vente de {quantity_line} de {product}"
            f" à {price_fmt} FCFA/{price_unit}." if price_fmt else f"Vente de {quantity_line} de {product}."
        ) if quantity_line else None,
        "SALES_RECORD_DIRECT": (
            f"Enregistrement d'une vente directe : {quantity_line} de {product}"
            f" à {price_fmt} FCFA." if price_fmt else f"Enregistrement d'une vente directe : {quantity_line} de {product}."
        ) if quantity_line else None,
        "PROCUREMENT_CREATE_REQUEST": (
            f"Lancement d'un appel d'offres pour {quantity_line} de {product}"
            f" au prix plafond de {price_fmt} FCFA." if price_fmt else f"Lancement d'un appel d'offres pour {quantity_line} de {product}."
        ) if quantity_line else None,
        "SALES_PLACE_BID": (
            f"Soumission d'une offre de {price_fmt} FCFA sur cette enchère."
            if price_fmt else "Soumission d'une offre sur cette enchère."
        ),
        "SALES_ACCEPT_CONTRACT": "Validation finale du contrat avec l'acheteur.",
        "PROCUREMENT_ACCEPT_OFFER": "Acceptation de l'offre du producteur sélectionné.",
        "PROCUREMENT_SELECT_WINNER": "Sélection de l'offre gagnante.",
        "STOCK_REGISTER_HARVEST": (
            f"Enregistrement d'une récolte : {quantity_line} de {product} en stock."
            if quantity_line else "Enregistrement d'une récolte en stock."
        ),
        "STOCK_RECORD_MOVEMENT": (
            f"Mouvement de stock : {quantity_line} de {product}."
            if quantity_line else "Mouvement de stock enregistré."
        ),
        "STOCK_ADJUST": (
            f"Modification du stock de {product} à {quantity_line}."
            if quantity_line else f"Modification du stock de {product}."
        ),
        "STOCK_REMOVE_PARTIAL": (
            f"Retrait de {quantity_line} de {product} du stock."
            if quantity_line else f"Retrait partiel du stock pour {product}."
        ),
        "STOCK_DELETE": f"Suppression définitive du lot de {product}.",
        "FINANCE_LOG_EXPENSE": (
            f"Enregistrement d'une dépense de {price_fmt} FCFA ({product})."
            if price_fmt else f"Enregistrement d'une dépense pour {product}."
        ),
        "FARM_CREATE": f"Déclaration d'une nouvelle exploitation.",
        "CROP_RECORD_INTERVENTION": f"Enregistrement d'une intervention agronomique.",
    }
    summary = mapping.get(goal)
    if summary:
        return summary
    fallback_quantity = quantity_line or payload.get("quantity_display") or payload.get("quantity_mentioned")
    if fallback_quantity not in (None, "", [], {}):
        return (
            f"Validation de l'opération : {goal}"
            f" (quantité : {fallback_quantity}, unité : {display_unit or converted_unit})"
        )
    return f"Validation de l'opération : {goal}"

async def confirmation_gate(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Gère l'état d'approbation explicite avant l'écriture en base de données."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    event = str(state.get("interpreted_event") or "").upper()

    if goal in _READ_GOALS:
        return {
            "is_certified": True,
            "execution_authorized": True,
            "waiting_for_confirmation": False,
            "status": "EXECUTING",
            "ag_ui_component": None,
        }

    if state.get("waiting_for_confirmation"):
        if event == "CONFIRM":
            return {
                "is_certified": True,
                "execution_authorized": True,
                "waiting_for_confirmation": False,
                "status": "EXECUTING",
                "ag_ui_component": None,
            }
        if event == "REJECT":
            return {
                "is_certified": False,
                "execution_authorized": False,
                "waiting_for_confirmation": False,
                "current_goal": None,
                "transaction_payload": {"__reset__": True},
                "missing_fields": [],
                "completed_fields": [],
                "goal_status": "COMPLETED",
                "status": "COMPLETED",
                "response_strategy": "CLARIFICATION",
                "final_response": "Opération annulée. Que souhaitez-vous faire ?",
                "ag_ui_component": None,
            }

    summary = _build_confirmation_summary(goal, payload)
    return {
        "waiting_for_confirmation": True,
        "is_certified": False,
        "execution_authorized": False,
        "confirmation_summary": summary,
        "expected_input": "CONFIRMATION",
        "last_agent_question": summary,
        "status": "WAITING_CONFIRMATION",
        "goal_status": "WAITING_CONFIRMATION",
        "response_strategy": "CONFIRMATION",
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "FormConfirmation"],
            "kwargs": {
                "title": "Confirmation requise",
                "summary": summary,
                "submit_label": "Confirmer",
                "cancel_label": "Annuler",
                "metadata": {"goal": goal},
            },
        },
    }
