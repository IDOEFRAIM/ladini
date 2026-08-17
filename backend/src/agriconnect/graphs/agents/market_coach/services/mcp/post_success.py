"""Post-success proactive suggestions.

Extracted from ``nodes/executor.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_POST_SUCCESS_SUGGESTIONS: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": 'Astuce : consultez vos offres en disant "voir mon stock".',
    "PROCUREMENT_CREATE_REQUEST": (
        "📢 Appel d'offres envoyé. Dès qu'un producteur propose une offre, "
        'je vous recontacte pour valider ou ajuster. Vous pouvez répondre "suivre mes appels" '
        "pour consulter l'état des réponses."
    ),
    "SALES_PLACE_BID": 'Vous pouvez suivre vos offres avec "mes enchères".',
    "STOCK_REGISTER_HARVEST": 'Vous pouvez maintenant mettre en vente avec "publier produit".',
    "STOCK_RECORD_MOVEMENT": "Votre inventaire a été mis à jour.",
}


def post_success_suggestion(goal: str, payload: Dict[str, Any]) -> Optional[str]:
    return _POST_SUCCESS_SUGGESTIONS.get(goal)


__all__ = ["post_success_suggestion"]
