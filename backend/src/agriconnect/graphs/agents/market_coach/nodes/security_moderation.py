from typing import Any, Dict
from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime
import asyncio
import inspect
logger = get_node_logger("SecurityModerationNode")


async def security_moderation(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Vérifie si l'entrée utilisateur contient des risques de sécurité ou des fraudes."""
    text = state.get("normalized_text") or state.get("user_query") or ""
    if not text:
        return {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None}

    security = getattr(mc_runtime, "security", None)
    if security is None or not hasattr(security, "moderate_content"):
        return {"security_status": "SAFE", "trust_score": 0.8, "ag_ui_component": None}

    try:
        _call = security.moderate_content(text)
        if inspect.isawaitable(_call) or asyncio.iscoroutine(_call):
            raw = await asyncio.wait_for(_call, timeout=5.0)
        else:
            raw = _call
    except asyncio.TimeoutError:
        logger.warning("[SecurityModeration] moderate_content timed out — defaulting to SAFE (degraded)")
        return {"security_status": "SAFE", "trust_score": 0.3, "ag_ui_component": None}
    except Exception as mod_exc:
        logger.warning("[SecurityModeration] moderate_content failed: %s — defaulting to SAFE (degraded)", mod_exc)
        return {"security_status": "SAFE", "trust_score": 0.3, "ag_ui_component": None}

    if not isinstance(raw, dict):
        return {"security_status": "SAFE", "trust_score": 0.5, "ag_ui_component": None}

    status_raw = str(raw.get("status") or "").upper()
    is_scam = bool(raw.get("is_scam"))
    if is_scam or status_raw in {"SCAM_DETECTED", "BLOCKED"}:
        return {
            "security_status": "SCAM_DETECTED",
            "security_reason": raw.get("reason") or "Contenu suspect détecté.",
            "trust_score": 0.0,
            "status": "BLOCKED",
            "response_strategy": "ERROR",
            "final_response": (
                "Désolé, votre message contient des éléments suspects. "
                "Pour votre sécurité, je ne peux pas continuer cette opération."
            ),
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "StatusComponent"],
                "kwargs": {"type": "error", "reason": "Contenu suspect détecté"},
            },
        }

    return {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None}


