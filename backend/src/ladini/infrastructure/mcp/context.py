"""Contexte d'identité MCP — identité (user/téléphone/session) propagée par ContextVar.

Ce module ne porte QUE le contexte d'identité consommé par le runtime MCP
(`runtime.py`, `base.py`) et le market_coach. L'ancien serveur « contexte
optimisé / mémoire épisodique » (build_context, enrich_state, record_interaction,
MCPContextServer) a été supprimé avec la direction produit « conseil agronomique
/ mémoire utilisateur », abandonnée.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generator, Optional

# ────────────────────── Scoped context state ──────────────────────────────


@dataclass(frozen=True)
class FarmerContext:
    user_id: str
    phone_number: str
    session_id: str
    language: str = "fr"


_MCP_CONTEXT: ContextVar[Optional[FarmerContext]] = ContextVar(
    "ladini_mcp_context", default=None
)


def set_mcp_context(context: Optional[FarmerContext]) -> None:
    _MCP_CONTEXT.set(context)


def get_mcp_context(default: Optional[FarmerContext] = None) -> Optional[FarmerContext]:
    return _MCP_CONTEXT.get(default)


@contextmanager
def mcp_context_scope(context: Optional[FarmerContext]) -> Generator[None, None, None]:
    token = _MCP_CONTEXT.set(context)
    try:
        yield
    finally:
        _MCP_CONTEXT.reset(token)
