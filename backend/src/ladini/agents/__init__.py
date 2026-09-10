"""Common agent utilities for Ladini — DRY architecture.

Modules:
  - onboarding: step-based new user registration
  - reducers: shared LangGraph state reducers (canonical source)
  - task_handler: legacy TaskHandler, consommé par nodes/executor.py

(Audit dead-code, 2026-09-10) `dispatcher.py`, `gateway.py`, `identity.py` et
`forms.py` ont été mis en quarantaine (`garbage/`) : zéro appelant réel de
leurs symboles exportés dans tout le repo (Market Coach a fini par se
construire ses propres équivalents — dispatch via `registry.py`/`actions/`,
accès MCP via `services/mcp/gateway.py`, identité via
`MarketRuntime.bind_user`, moteur de formulaire DRY retiré côté MarketCoach).
Ce fichier les important tous les 4 inconditionnellement, ils étaient chargés
en mémoire à CHAQUE démarrage dès qu'un seul module avait besoin de
`reducers` — retirés d'ici pour que ce coût de chargement disparaisse aussi.
Voir `garbage/README.md` pour le détail de la vérification.
"""

from .onboarding import (
    OnboardingResult,
    OnboardingState,
    OnboardingStep,
    run_onboarding_step,
)
from .reducers import (
    _KEEP,
    merge_dict,
    replace_list,
    replace_value,
)

__all__ = [
    # Onboarding
    "OnboardingResult",
    "OnboardingState",
    "OnboardingStep",
    "run_onboarding_step",
    # Reducers (shared state primitives)
    "_KEEP",
    "merge_dict",
    "replace_list",
    "replace_value",
]
