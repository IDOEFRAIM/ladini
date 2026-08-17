"""Rendering — contexte partagé, labels, helpers de format et composants AG-UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from agriconnect.core.formatting import fmt_num as _shared_fmt_num
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG

logger = logging.getLogger("AgriConnect.Market.Rendering")


# =====================================================================
# CONTEXTE DE RENDU — calculé une fois par le dispatcher
# =====================================================================


@dataclass(slots=True)
class RenderContext:
    state: Dict[str, Any]
    mc_runtime: Any
    strategy: str
    status: str
    goal: Optional[str]
    salutation: str
    payload: Dict[str, Any]


# =====================================================================
# LABELS & RAISONS MÉTIER
# =====================================================================

_DEFAULT_LABEL_MAP = {
    "product": "produit",
    "quantity": "quantité",
    "price": "prix",
    "unit": "unité",
    "zone": "zone de production",
}


def label_for_field(goal: Optional[str], field: Optional[str]) -> str:
    if not field:
        return "cette information"
    clean_field = field.replace("_mentioned", "").replace("_for_sale", "")
    goal_config = INTENT_CONFIG.get(goal or "") or {}
    label_map = goal_config.get("label_map") or {}
    return label_map.get(
        field,
        label_map.get(
            clean_field,
            _DEFAULT_LABEL_MAP.get(
                field, _DEFAULT_LABEL_MAP.get(clean_field, clean_field)
            ),
        ),
    )


# POURQUOI chaque champ compte (valeur métier montrée à l'utilisateur)
FIELD_BUSINESS_REASON: Dict[str, str] = {
    "product": "pour cibler les bons acheteurs et bien catégoriser votre offre",
    "quantity": "pour que les acheteurs sachent exactement ce qui est disponible",
    "price": "pour positionner votre offre de manière compétitive sur le marché",
    "unit": "pour éviter toute confusion lors de la livraison",
    "zone": "pour connecter avec les acheteurs de votre région",
    "stock_id": "pour identifier précisément le lot concerné",
    "auction_id": "pour répondre à la bonne demande d'achat",
    "bid_id": "pour valider la bonne transaction",
    "movement_type": "pour que votre inventaire reste exact (Entrée, Sortie ou Perte)",
    "farm_id": "pour rattacher l'opération à la bonne exploitation",
    "deadline": "pour que les producteurs sachent quand répondre",
    "description": "pour donner envie aux acheteurs (qualité, variété, fraîcheur...)",
}


# =====================================================================
# GOAL RESOLUTION & DÉPLIAGE DÉFENSIF
# =====================================================================


def resolve_goal_for_ui(state: Dict[str, Any]) -> Optional[str]:
    """Résout un goal stable pour les métadonnées AG-UI (jamais 'UNKNOWN')."""
    working = state.get("working_memory") or {}
    candidates = [
        state.get("current_goal"),
        working.get("active_goal"),
        working.get("locked_intent"),
        state.get("suspended_goal"),
        state.get("detected_intent"),
    ]
    for cand in candidates:
        if cand in (None, ""):
            continue
        goal = str(cand).upper().strip()
        if goal and goal != "UNKNOWN":
            return goal
    return None


def unwrap_execution_result(exec_result: Dict[str, Any]) -> Dict[str, Any]:
    """Déplie défensivement les réponses MCP imbriquées sous `data`.

    Certains clients retournent un payload enveloppé de la forme:
    {"status": "success", "data": {"status": "success", "message": ..., "data": [...]}}
    On normalise ici pour que le renderer SUCCESS lise toujours le bon niveau.
    """
    current = exec_result if isinstance(exec_result, dict) else {}
    for _ in range(3):
        nested = current.get("data")
        if not isinstance(nested, dict):
            break
        # Enveloppe `{"data": {"raw_result": "<json string>"}}` : certains
        # transports MCP renvoient le vrai payload sérialisé sous `raw_result`.
        raw = nested.get("raw_result")
        if isinstance(raw, str) and raw.strip():
            try:
                from agriconnect.graphs.agents.market_coach.utils import ensure_dict

                parsed = ensure_dict(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed:
                outer_message = current.get("message")
                current = dict(parsed)
                if outer_message and not current.get("message"):
                    current["message"] = outer_message
                continue
        has_wrapper_shape = any(
            k in nested for k in ("status", "data", "message", "error")
        )
        if not has_wrapper_shape:
            break
        outer_message = current.get("message")
        current = dict(nested)
        if outer_message and not current.get("message"):
            current["message"] = outer_message
    return current


# =====================================================================
# FORMAT HELPERS
# =====================================================================


def fmt_num(val: Any) -> str:
    """50.0 -> '50', 12.5 -> '12.5'. Voir core/formatting.py — jamais de
    notation scientifique (un paysan ne comprend pas "1.5e+06 FCFA")."""
    formatted = _shared_fmt_num(val)
    return formatted or "0"


def fmt_date(date_val: Any) -> Optional[str]:
    if not date_val:
        return None
    if isinstance(date_val, datetime):
        return date_val.strftime("%d/%m/%Y")
    text = str(date_val)
    try:
        cleaned = text.replace("Z", "+00:00") if text.endswith("Z") else text
        dt_val = datetime.fromisoformat(cleaned)
        return dt_val.strftime("%d/%m/%Y")
    except Exception:
        return text


# =====================================================================
# COMPOSANTS AG-UI — constructeurs uniques (zéro dict inline dupliqué)
# =====================================================================


def status_component(kind: str, **kwargs: Any) -> Dict[str, Any]:
    return {
        "lc_type": "constructor",
        "id": ["ag_ui", "StatusComponent"],
        "kwargs": {"type": kind, **kwargs},
    }


def list_menu_component(
    title: str, options: List[Dict[str, str]], metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "lc_type": "constructor",
        "id": ["ag_ui", "ListMenu"],
        "kwargs": {"title": title, "options": options, "metadata": metadata or {}},
    }


def indexed_options(candidates: List[str]) -> List[Dict[str, str]]:
    return [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)]


# =====================================================================
# CORRECTIONS UTILISATEUR (accusé de réception des changements de valeur)
# =====================================================================

_USER_FACING_CORRECTIONS = {"product", "quantity", "price", "unit", "zone"}


def apply_corrections(
    state: Dict[str, Any], response: Dict[str, Any]
) -> Dict[str, Any]:
    status = str(state.get("status") or "").upper().strip()
    strategy = str(state.get("response_strategy") or "").upper().strip()
    if status == "COMPLETED" or strategy == "SUCCESS":
        working = state.get("working_memory") or {}
        if working.get("recent_corrections"):
            working = dict(working)
            working["recent_corrections"] = None
            response["working_memory"] = working
        return response
    working = state.get("working_memory") or {}
    corrections_raw = working.get("recent_corrections") or {}

    parts: List[str] = []
    if isinstance(corrections_raw, dict):
        for field, delta in corrections_raw.items():
            if field not in _USER_FACING_CORRECTIONS:
                continue
            label = label_for_field(state.get("current_goal"), field)
            parts.append(f"*{label.title()}* mis à jour : {delta}")
    else:
        for change in list(corrections_raw or []):
            if isinstance(change, dict):
                for field, delta in change.items():
                    if field not in _USER_FACING_CORRECTIONS:
                        continue
                    label = label_for_field(state.get("current_goal"), field)
                    parts.append(f"*{label.title()}* mis à jour : {delta}")
            else:
                parts.append(str(change))

    if parts:
        acknowledgement = "\n\n" + "\n".join(parts)
        response["final_response"] = (
            f"{response.get('final_response', '')}{acknowledgement}"
        )
        working = dict(working)
        working["recent_corrections"] = None
        response["working_memory"] = working
    return response
