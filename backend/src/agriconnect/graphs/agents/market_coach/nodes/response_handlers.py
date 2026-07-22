"""Market — final_response : dispatcher de rendu par stratégie (Phase 3).

Le monolithe historique (~940 lignes, 9 stratégies dans une seule fonction)
est découpé dans ``nodes/rendering/`` — un module par famille de stratégies :

    rendering/common.py   — RenderContext, labels, formats, composants AG-UI
    rendering/ask.py      — ONBOARDING, ASK_MISSING_FIELD (+ question LLM)
    rendering/confirm.py  — CONFIRMATION
    rendering/menus.py    — SELECTION_MENU
    rendering/success.py  — SUCCESS / COMPLETED (résultats d'outils)
    rendering/feedback.py — ERROR, RECOVERY, INTERRUPTION_HANDLER, fallback

Ce module ne fait plus que : calculer le RenderContext, choisir le handler,
et préserver la règle historique « status COMPLETED sans stratégie dédiée
⇒ rendu SUCCESS ».
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import normalize_slot_keys
from agriconnect.graphs.agents.market_coach.nodes.rendering import (
    RenderContext,
    label_for_field,
    render_ask_missing_field,
    render_clarification,
    render_confirmation,
    render_error,
    render_interruption,
    render_onboarding,
    render_recovery,
    render_selection_menu,
    render_success,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    resolve_goal_for_ui,
)

logger = logging.getLogger("AgriConnect.Market.ResponseHandlers")

_Handler = Callable[[RenderContext], Awaitable[Dict[str, Any]]]

# Stratégies évaluées AVANT la règle SUCCESS-par-status (ordre historique).
_PRE_SUCCESS: Dict[str, _Handler] = {
    "ONBOARDING": render_onboarding,
    "ASK_MISSING_FIELD": render_ask_missing_field,
    "CONFIRMATION": render_confirmation,
    "SELECTION_MENU": render_selection_menu,
}

# Stratégies évaluées APRÈS (un status COMPLETED les court-circuite vers SUCCESS
# — comportement historique à préserver : RECOVERY/INTERRUPTION ne sont PAS
# dans l'ensemble d'exclusion).
_POST_SUCCESS: Dict[str, _Handler] = {
    "ERROR": render_error,
    "RECOVERY": render_recovery,
    "INTERRUPTION_HANDLER": render_interruption,
}

_SUCCESS_EXCLUDED = frozenset({
    "SELECTION_MENU", "ASK_MISSING_FIELD", "CONFIRMATION",
    "CLARIFICATION", "ERROR", "ONBOARDING",
})


def _select_handler(strategy: str, status: str) -> _Handler:
    handler = _PRE_SUCCESS.get(strategy)
    if handler is not None:
        return handler
    if strategy == "SUCCESS" or (status == "COMPLETED" and strategy not in _SUCCESS_EXCLUDED):
        return render_success
    return _POST_SUCCESS.get(strategy, render_clarification)


async def final_response(state: MarketAgentState, mc_runtime: Any) -> Dict[str, Any]:
    """Compose le message final + composant AG-UI selon la stratégie du tour.

    Source de vérité unique du texte final : ne jamais réutiliser un
    ``final_response`` pré-calculé ici-même (les handlers décident, par
    stratégie, s'ils peuvent le réutiliser).
    """
    strategy = str(state.get("response_strategy") or "CLARIFICATION").upper().strip()
    status = str(state.get("status") or "").upper().strip()
    user_name = state.get("user_name") or ""

    ctx = RenderContext(
        state=state,
        mc_runtime=mc_runtime,
        strategy=strategy,
        status=status,
        goal=resolve_goal_for_ui(state),
        salutation=f"{user_name}, " if user_name else "",
        payload=normalize_slot_keys(state.get("transaction_payload") or {}),
    )

    handler = _select_handler(strategy, status)
    return await handler(ctx)


# Alias de compat — consommé par nodes/validation.py et nodes/cognitive.py.
_label_for_field = label_for_field

__all__ = [
    "final_response",
    "_label_for_field",
]
