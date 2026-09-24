"""Aides d'assertion sur l'état RÉDUIT (reducers officiels), jamais sur le patch brut.

Un patch renvoyé par un nœud n'est pas l'état : sur un canal `merge_dict`, une clé absente
du patch signifie « inchangé ». Asserter `key not in patch` prouve donc… que l'ancienne
valeur est conservée. Ces aides réduisent le patch comme LangGraph le fait réellement.
"""
from __future__ import annotations

import typing
from typing import Any, Dict

from ladini.agents.reducers import merge_dict
from ladini.graphs.agents.market_coach.core.state import MarketAgentState

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)
_STALE = "__stale_value_from_a_previous_turn__"


def reducer_for(channel: str):
    meta = getattr(_HINTS.get(channel), "__metadata__", None)
    return meta[0] if meta else None


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Applique `patch` à `state` avec le reducer réel de chaque canal."""
    new_state = dict(state)
    for key, value in patch.items():
        reducer = reducer_for(key)
        new_state[key] = value if reducer is None else reducer(state.get(key), value)
    return new_state


def clears(channel_patch: Dict[str, Any], key: str) -> bool:
    """Vrai si ce patch de canal `merge_dict` efface RÉELLEMENT `key`, même quand l'état
    précédent en contenait une valeur (périmée)."""
    return merge_dict({key: _STALE}, channel_patch).get(key) is None


__all__ = ["apply_patch", "clears", "reducer_for"]
