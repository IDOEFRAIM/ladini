"""LLM_ROUTER — sélection dynamique du modèle Groq selon le goal courant.

Politique : les nœuds d'infrastructure (normalisation d'entrée, modération
sécurité, nettoyage d'état) n'ont besoin d'aucun raisonnement métier — un
modèle rapide/économique suffit. Tout le reste (interprétation d'intent,
planification de goal, génération de réponse...) a besoin du modèle de
raisonnement.

Aucun nom de modèle n'est codé en dur ici : la correspondance goal -> modèle
vit dans `settings.ROUTING_MAP` (core/settings.py, dérivée de
`LLM_MODEL`/`LLM_MODEL_REASONING`) — ce module ne fait que la consulter.
"""

from __future__ import annotations

from typing import Optional

from agriconnect.core.settings import settings

# Goals routés vers le profil FAST — même liste que les clés explicites de
# `settings.ROUTING_MAP` (voir `_build_routing_map` dans core/settings.py) :
# ce module ne fait qu'exprimer la MÊME politique en termes de PROFIL plutôt
# que de nom de modèle, pour le LLM Gateway (2026-09-02) — voir
# `graphs/agents/market_coach/llm_gateway/`. Zéro changement de comportement
# de routage : les mêmes 3 goals restent "rapide", tout le reste reste
# "raisonnement".
_FAST_PROFILE_GOALS = frozenset(
    {"INPUT_NORMALIZATION", "SECURITY_MODERATION", "STATE_CLEANER"}
)


def get_profile_for_goal(goal: Optional[str]):
    """Retourne le `LLMProfile` (FAST/REASONING) pour `goal` — à consommer
    par `MarketRuntime.llm_gateway.complete(profile=...)`, jamais par un nom
    de modèle en dur dans un node métier (§5 du brief LLM Gateway)."""
    from agriconnect.graphs.agents.market_coach.llm_gateway.types import LLMProfile

    key = str(goal or "").strip().upper()
    if key in _FAST_PROFILE_GOALS:
        return LLMProfile.FAST
    return LLMProfile.REASONING


def get_model_for_goal(goal: Optional[str]) -> str:
    """Retourne le modèle Groq approprié pour `goal`.

    Résolution :
    1. `settings.ROUTING_MAP[goal]` si le goal y est explicitement listé
       (par défaut : INPUT_NORMALIZATION, SECURITY_MODERATION, STATE_CLEANER
       -> modèle rapide `settings.LLM_MODEL`).
    2. Sinon `settings.ROUTING_MAP["__default__"]` — modèle de raisonnement,
       pour tout goal métier complexe (MARKET_BROWSE_REQUESTS, GOAL_PLANNING,
       et tout autre intent non listé explicitement).
    3. Filet de sécurité ultime si `ROUTING_MAP` est vide/mal configuré :
       `settings.LLM_MODEL`.
    """
    key = str(goal or "").strip().upper()
    routing_map = getattr(settings, "ROUTING_MAP", None) or {}
    if key in routing_map:
        return routing_map[key]
    return routing_map.get("__default__") or settings.LLM_MODEL


__all__ = ["get_model_for_goal", "get_profile_for_goal"]
