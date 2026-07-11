from __future__ import annotations
from typing import Any, Dict, Iterable, Optional
from agriconnect.core.logging import get_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import FarmGateway
from agriconnect.graphs.agents.market_coach.core.base import (
    _AUTO_FARM_NOTICE,
    MARKET_VALIDATION_CONFIG,
)

logger = get_logger("AgriConnect.MarketCoach.AutoFarm")

FARM_RULES = MARKET_VALIDATION_CONFIG.farm


def _extract_farm_id(candidates: Iterable[Dict[str, Any]]) -> Optional[str]:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        farm_id = candidate.get("farm_id") or candidate.get("id")
        if farm_id:
            return str(farm_id)
    return None


async def ensure_farm_node(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Garantit la présence d'un farm_id avant les appels critiques.

    Asymmetric behavior (Mission 3):
      - WRITE_REQUIRES_FARM: auto-provision allowed (create farm if none exists).
      - READ_OPTIONAL_FARM: NO auto-provisioning — emit explicit selection prompt.
      - Multi-farm: NEVER auto-select farms[0] — always prompt for user choice.
    """

    goal = (state.get("current_goal") or "").upper()
    if goal not in FARM_RULES.farm_critical_goals:
        return {}

    payload = dict(state.get("transaction_payload") or {})
    if payload.get("farm_id"):
        return {}

    if state.get("farm_creation_attempted"):
        logger.info("[AutoFarm] Tentative déjà effectuée pour cette transaction — aucune action.")
        return {}

    phone = state.get("user_phone") or payload.get("phone")
    if not phone:
        logger.debug("[AutoFarm] Impossible de déterminer le téléphone utilisateur — abandon.")
        return {}

    is_read_intent = goal in FARM_RULES.read_optional_farm
    is_write_intent = goal in FARM_RULES.write_requires_farm
    updates: Dict[str, Any] = {"farm_creation_attempted": True}

    # Short-circuit: check in-memory cache before network call
    cached = state.get("user_farms_cache")
    if isinstance(cached, list) and cached:
        farms_cache = cached
        logger.info("[AutoFarm] Using cached farms (%d entries)", len(farms_cache))
    else:
        farms_cache = []
        try:
            farm_gw = FarmGateway(mc_runtime)
            farms_cache = await farm_gw.list_farms(str(phone))
        except Exception as exc:  # pragma: no cover - log only
            logger.warning("[AutoFarm] Impossible de récupérer les fermes existantes: %s", exc)

    if farms_cache:
        updates["user_farms_cache"] = farms_cache

    farms_list = farms_cache if isinstance(farms_cache, list) else []

    # --- MULTI-FARM AMBIGUITY: always prompt user for explicit choice ---
    if len(farms_list) > 1:
        logger.info("[AutoFarm] %d farms detected — prompting user for selection", len(farms_list))
        lines = ["🌾 *Sur quelle exploitation souhaitez-vous travailler ?*"]
        mapping: Dict[str, str] = {}
        for i, f in enumerate(farms_list, start=1):
            f_id = str(f.get("id") or f.get("farm_id") or "")
            name = f.get("name") or f"Ferme {i}"
            lines.append(f"{i}. *{name}*")
            mapping[str(i)] = f_id
        updates["status"] = "WAITING_INPUT"
        updates["expected_input"] = "SELECTION"
        updates["response_strategy"] = "SELECTION_MENU"
        updates["final_response"] = "\n".join(lines)
        updates["available_mapping"] = mapping
        updates["working_memory"] = {"available_mapping_kind": "farm"}
        return updates

    farm_id = _extract_farm_id(farms_list)

    # --- READ intents: block auto-provisioning ---
    if not farm_id and is_read_intent:
        logger.info("[AutoFarm] No farm for READ intent '%s' — prompting creation", goal)
        updates["status"] = "WAITING_INPUT"
        updates["response_strategy"] = "ASK_CLARIFICATION"
        updates["final_response"] = (
            "Vous n'avez pas encore d'exploitation enregistrée. "
            "Pour consulter vos données, créez d'abord une ferme en disant "
            "par exemple : *\"Créer une ferme Ferme principale à Dakar\"*."
        )
        return updates

    # --- WRITE intents: auto-provision if no farm exists ---
    if not farm_id and not is_write_intent:
        logger.warning("[AutoFarm] Goal '%s' not authorized for auto-provisioning", goal)
        updates["status"] = "WAITING_INPUT"
        updates["response_strategy"] = "ASK_CLARIFICATION"
        updates["final_response"] = (
            "Je ne peux pas créer automatiquement une exploitation pour cette opération. "
            "Merci de préciser l'exploitation à utiliser."
        )
        return updates

    if not farm_id:
        zone_name = state.get("zone_name") or payload.get("zone_name")
        zone_id = state.get("zone_id") or payload.get("zone_id")
        default_name = payload.get("farm_name") or (
            f"Ferme de {state['user_name']}"
            if state.get("user_name")
            else "Ferme principale"
        )
        create_payload = {
            "phone": str(phone).strip(),
            "name": default_name,
            "zone_id": zone_id,
        }
        if zone_name:
            create_payload["location"] = zone_name

        try:
            farm_gw = FarmGateway(mc_runtime)
            farm_data = await farm_gw.create_farm(**{k: v for k, v in create_payload.items() if v})
            farm_id = str(farm_data.get("id") or farm_data.get("farm_id") or "")
            if farm_id:
                updates["auto_farm_notice"] = _AUTO_FARM_NOTICE
        except Exception as exc:  # pragma: no cover - log only
            logger.error("[AutoFarm] create_farm a échoué: %s", exc)
            updates["error_creating_farm"] = True
            return updates

    if not farm_id:
        updates["error_creating_farm"] = True
        return updates

    payload["farm_id"] = farm_id
    stable_entities = dict(state.get("stable_entities") or {})
    stable_entities["farm_id"] = farm_id

    updates["transaction_payload"] = payload
    updates["stable_entities"] = stable_entities
    return updates


__all__ = ["ensure_farm_node"]
