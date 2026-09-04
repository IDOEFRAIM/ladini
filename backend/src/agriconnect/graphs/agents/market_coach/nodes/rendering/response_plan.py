"""ResponsePlan — contrat explicite DÉCISION → RENDU (refonte architecturale
2026-09-02, mandat §15-17).

Portée délibérément scopée (voir rapport final, section "Legacy restant") :
ce module ne remplace PAS `RenderContext` (`nodes/rendering/common.py`) —
les handlers de `nodes/rendering/*.py` continuent de lire `RenderContext`
(état complet, nécessaire à leur logique existante déjà éprouvée). Ce que ce
module apporte réellement :

1. Un point de construction UNIQUE, juste avant le dispatch vers un handler
   (`nodes/response_handlers.py::final_response`), qui matérialise
   explicitement "ce que le tour a décidé" (`kind` = `response_strategy`,
   `next_interaction` = `pending_interaction` résolu) — au lieu de laisser
   chaque renderer redéduire implicitement l'état depuis des flags épars
   (c'est exactement CE défaut que `render_confirmation` corrigeait déjà
   pour son propre cas, ce module généralise le PATRON à tout le dispatch).
2. La source du champ `next_interaction` de l'événement `CONVERSATION_STATE_
   TRANSITION` (§35) — `ResponsePlan` n'est donc pas un type décoratif : il a
   un consommateur réel dès ce commit (`core/telemetry.py::
   record_state_transition`).

Étendre CHAQUE handler pour ne consommer QUE `ResponsePlan.data` (au lieu de
`RenderContext.state` complet) est un chantier plus large, correctement
différé — voir rapport final.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    PendingInteraction,
)


@dataclass(frozen=True)
class ResponsePlan:
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    next_interaction: PendingInteraction = field(default_factory=PendingInteraction)

    @classmethod
    def from_state(cls, state: Dict[str, Any], *, strategy: str) -> "ResponsePlan":
        from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
            get_pending_interaction,
        )

        return cls(
            kind=strategy,
            data={
                "status": state.get("status"),
                "goal": state.get("current_goal"),
            },
            next_interaction=get_pending_interaction(state),
        )


__all__ = ["ResponsePlan"]
