from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.roles import normalize_role


def make_role_guard(role: str):
    """Nœud d'entrée du graphe — plus un garde de session (refonte double-rôle).

    Avant : bloquait tout goal/outil n'appartenant pas au rôle figé du graphe
    compilé (`role`), ce qui rendait impossible pour un même utilisateur de
    vendre ET acheter dans une même conversation. Le graphe étant désormais
    unique (voir `core/graph_builder.py`) et le domaine résolu PAR GOAL
    (`core/router.py::_goal_domain`), il n'y a plus de "rôle de session" à
    faire respecter ici.

    La sécurité sur les actions sensibles (paiement, OTP de livraison,
    annulation) se fait désormais au plus près de la donnée — dans les
    méthodes DB elles-mêmes (`services/database/*`), qui vérifient que
    l'appelant est bien la partie prenante de LA COMMANDE EN COURS, pas de
    son "profil" global. Ce nœud ne fait plus que renseigner `role`/
    `user_role` par défaut la 1ère fois (valeur d'affichage/préférence,
    jamais un contrôle d'accès).
    """
    role_norm = normalize_role(role)

    async def _role_guard(state: Dict[str, Any], _: Any) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if not state.get("role"):
            patch["role"] = role_norm
        if not (state.get("user_role") or "").strip():
            patch["user_role"] = role_norm
        return patch

    return _role_guard


__all__ = ["make_role_guard"]
