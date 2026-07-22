"""Rendering — un module par stratégie de réponse (découpage Phase 3).

Le dispatcher vit dans ``nodes/response_handlers.py::final_response`` ;
chaque handler reçoit un ``RenderContext`` et retourne le patch d'état
``{"final_response": str, "ag_ui_component": dict|None, ...}``.
"""

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    label_for_field,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import (
    render_onboarding,
    render_ask_missing_field,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.confirm import (
    render_confirmation,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.menus import (
    render_selection_menu,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.success import (
    render_success,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import (
    render_error,
    render_recovery,
    render_interruption,
    render_clarification,
)

__all__ = [
    "RenderContext",
    "label_for_field",
    "render_onboarding",
    "render_ask_missing_field",
    "render_confirmation",
    "render_selection_menu",
    "render_success",
    "render_error",
    "render_recovery",
    "render_interruption",
    "render_clarification",
]
