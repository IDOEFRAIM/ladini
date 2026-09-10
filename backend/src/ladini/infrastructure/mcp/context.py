"""
MCP Context Server 2.0 — FastMCP rewrite.
==========================================

Semantic Cache Invalidation + Trace Recording.

Tools:
  - build_context(user_id, query, zone, crop) → optimised user context
  - get_token_budget()                        → per-component token budget
  - enrich_state(state_json)                  → enriched agent state
  - record_interaction(user_id, query, …)     → ack

Resources:
  - context://{user_id}  → cached context for a user
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional

from fastmcp import Context, FastMCP

from ladini.infrastructure.mcp.utils import run_coro_blocking

logger = logging.getLogger("MCP.Context")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("Ladini Context MCP Server")

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


# ────────────────────── Module-level state ────────────────────────────────

_optimizer = None
_session_factory = None
_llm_client = None


@dataclass
class _CacheEntry:
    payload: Dict[str, Any]
    expires_at: float


_context_cache: "OrderedDict[str, _CacheEntry]" = OrderedDict()
_default_ttl: int = 300  # 5 min


def _configure(context_optimizer=None, session_factory=None, llm_client=None):
    """Inject dependencies (called once at startup from MCPContextServer ctor)."""
    global _optimizer, _session_factory, _llm_client
    if context_optimizer is not None:
        _optimizer = context_optimizer
    if session_factory is not None:
        _session_factory = session_factory
    if llm_client is not None:
        _llm_client = llm_client


def _lazy_optimizer():
    """Lazy-load the ContextOptimizer if possible."""
    global _optimizer
    if _optimizer is None and _session_factory:
        try:
            from ladini.services.memory import (
                ContextOptimizer,
                EpisodicMemory,
                ProfileExtractor,
                UserFarmProfile,
            )

            _profile = UserFarmProfile(_session_factory)
            _episodic = EpisodicMemory(_session_factory, llm_client=_llm_client)
            _extractor = ProfileExtractor(_llm_client, _profile)
            _optimizer = ContextOptimizer(_profile, _episodic, _extractor)
            logger.info("ContextOptimizer loaded (lazy init)")
        except Exception as exc:
            logger.error("ContextOptimizer unavailable: %s", exc)
    return _optimizer


def _cache_set(user_id: str, payload: Dict[str, Any]) -> None:
    expires = time.monotonic() + _default_ttl
    _context_cache[user_id] = _CacheEntry(payload=payload, expires_at=expires)
    _context_cache.move_to_end(user_id)
    _prune_context_cache()


def _cache_get(user_id: str) -> Optional[Dict[str, Any]]:
    entry = _context_cache.get(user_id)
    if not entry:
        return None
    if entry.expires_at < time.monotonic():
        _context_cache.pop(user_id, None)
        return None
    return entry.payload


def _prune_context_cache() -> None:
    now = time.monotonic()
    stale_keys = [
        key for key, value in _context_cache.items() if value.expires_at < now
    ]
    for key in stale_keys:
        _context_cache.pop(key, None)
    # Keep cache bounded (e.g., 512 entries) to avoid memory leaks
    while len(_context_cache) > 512:
        _context_cache.popitem(last=False)


def _sync_await(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_coro_blocking(coro)
    raise RuntimeError(
        "Synchronous MCPContextServer API called from running event loop; use async equivalent"
    )


# ────────────────────── Tools ─────────────────────────────────────────────


@mcp.tool()
async def build_context(
    user_id: str,
    query: str,
    zone: str = "",
    crop: str = "",
    ctx: Context = None,
) -> str:
    """Construit le contexte utilisateur optimisé pour un agent.

    Combine profil structuré (~80 tokens) + épisodes pertinents (~120 tokens) +
    métadonnées. Remplace 5000 tokens d'historique brut par ~350 tokens ciblés.

    Args:
        user_id: Identifiant de l'agriculteur
        query: Question courante de l'agriculteur
        zone: Zone géographique (optionnel)
        crop: Culture concernée (optionnel)
    """
    if ctx:
        await ctx.info(f"build_context user_id={user_id}")

    optimizer = _lazy_optimizer()
    if not optimizer:
        return json.dumps(
            {
                "error": "ContextOptimizer indisponible",
                "combined_context": "",
                "token_estimate": 0,
            }
        )

    result = optimizer.build_context(
        user_id,
        query,
        zone=zone or None,
        crop=crop or None,
    )
    # cache result
    _cache_set(user_id, result)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def get_token_budget(ctx: Context = None) -> str:
    """Retourne le budget tokens par composant du contexte."""
    if ctx:
        await ctx.info("Returning token budget")
    try:
        from ladini.services.memory.context_optimizer import TOKEN_BUDGETS

        return json.dumps(TOKEN_BUDGETS, ensure_ascii=False)
    except Exception:
        return json.dumps(
            {
                "profile": 80,
                "episodes": 120,
                "metadata": 50,
                "total": 350,
            }
        )


@mcp.tool()
async def enrich_state(state_json: str, ctx: Context = None) -> str:
    """Enrichit un état GlobalAgriState avec le contexte mémoire.

    Args:
        state_json: JSON string de l'état de l'orchestrateur à enrichir
    """
    if ctx:
        await ctx.info("enrich_state")

    state = json.loads(state_json) if isinstance(state_json, str) else state_json
    optimizer = _lazy_optimizer()
    if not optimizer:
        return json.dumps(state, ensure_ascii=False, default=str)

    try:
        enriched = optimizer.enrich_state(state)
        return json.dumps(enriched, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("enrich_state failed: %s", exc)
        return json.dumps(state, ensure_ascii=False, default=str)


@mcp.tool()
async def record_interaction(
    user_id: str,
    query: str,
    response: str,
    agent: str,
    intent: str = "UNKNOWN",
    ctx: Context = None,
) -> str:
    """Enregistre une interaction dans la mémoire épisodique.

    Args:
        user_id: ID utilisateur
        query: Question posée
        response: Réponse fournie
        agent: Agent ayant répondu
        intent: Intent détecté (optionnel)
    """
    if ctx:
        await ctx.info(f"record_interaction user_id={user_id} agent={agent}")

    optimizer = _lazy_optimizer()
    if not optimizer:
        return json.dumps(
            {"status": "skipped", "reason": "ContextOptimizer indisponible"}
        )

    try:
        optimizer.record_interaction(
            user_id=user_id,
            query=query,
            response=response,
            agent=agent,
            intent=intent,
        )
        return json.dumps({"status": "recorded"})
    except Exception as exc:
        logger.warning("record_interaction failed: %s", exc)
        return json.dumps({"status": "error", "reason": str(exc)})


# ────────────────────── Resources ─────────────────────────────────────────


@mcp.resource("context://status")
async def context_status() -> str:
    """Health-check resource for context server."""
    return json.dumps(
        {
            "server": "Ladini Context MCP Server",
            "cached_users": len(_context_cache),
            "optimizer_ready": _lazy_optimizer() is not None,
        }
    )


# ────────────────────── Backward-compatible class wrapper ─────────────────


class MCPContextServer:
    """
    Compat wrapper — drop-in replacement for old MCPContextServer.

    Preserves:
      - ``__init__(context_optimizer, session_factory, llm_client)``
      - ``list_tools()``, ``call_tool(name, args)``
      - ``read_user_context(user_id, query, …)`` convenience method
      - ``check_required_fields(context, required)``
    """

    def __init__(self, context_optimizer=None, session_factory=None, llm_client=None):
        _configure(
            context_optimizer=context_optimizer,
            session_factory=session_factory,
            llm_client=llm_client,
        )
        logger.info("MCP Context Server v2 (FastMCP) initialised")

    # ── MCP interface ────────────────────────────────────────────────

    @staticmethod
    def list_tools() -> list:
        return [
            {
                "name": "build_context",
                "description": "Construit le contexte utilisateur optimisé",
            },
            {"name": "get_token_budget", "description": "Budget tokens par composant"},
            {
                "name": "enrich_state",
                "description": "Enrichit un état avec le contexte mémoire",
            },
            {"name": "record_interaction", "description": "Enregistre une interaction"},
        ]

    @staticmethod
    def list_resources() -> list:
        return [
            {
                "uri": "context://status",
                "name": "Context Status",
                "description": "Health check",
                "mimeType": "application/json",
            },
        ]

    @staticmethod
    async def _dispatch(name: str, args: dict) -> str:
        handlers = {
            "build_context": build_context,
            "get_token_budget": get_token_budget,
            "enrich_state": enrich_state,
            "record_interaction": record_interaction,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown context tool: {name}")
        return await fn(**args)

    async def call_tool_async(self, name: str, arguments: dict) -> dict:
        raw = await self._dispatch(name, arguments)
        return {
            "status": "ok",
            "content": [{"type": "text", "text": raw}],
        }

    def call_tool(self, name: str, arguments: dict) -> dict:
        try:
            return _sync_await(self.call_tool_async(name, arguments))
        except Exception as exc:
            logger.error("call_tool error (%s): %s", name, exc)
            return {"status": "error", "error": str(exc)}

    async def read_resource_async(self, uri: str, params: dict = None) -> dict:
        raw = await context_status()
        return {
            "status": "ok",
            "contents": [{"uri": uri, "mimeType": "application/json", "text": raw}],
        }

    def read_resource(self, uri: str, params: dict = None) -> dict:
        try:
            return _sync_await(self.read_resource_async(uri, params))
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    # ── Convenience methods (agents internes) ────────────────────────

    def read_user_context(
        self,
        user_id: str,
        query: str = "",
        zone: str = "",
        crop: str = "",
        trace_envelope=None,
    ) -> Dict[str, Any]:
        """Shortcut for internal agents.

        Uses semantic cache invalidation (emergency keywords bypass cache).
        """
        try:
            from ladini.protocols.core import CachePolicy

            if query:
                temp = CachePolicy(key="_check")
                bypass = temp.should_bypass(query)
            else:
                bypass = False
        except Exception:
            bypass = False

        cached = _cache_get(user_id)
        if cached and not bypass:
            return cached

        raw = _sync_await(
            build_context(
                user_id=user_id or "anonymous", query=query, zone=zone, crop=crop
            )
        )
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            result = {"user_id": user_id, "cached": False}
        if result.get("error"):
            return {"user_id": user_id, "cached": False}
        return result

    @staticmethod
    def check_required_fields(
        context: Dict[str, Any], required: List[str]
    ) -> Dict[str, Any]:
        """Vérifie que les champs requis sont présents dans le contexte."""
        missing = [f for f in required if not context.get(f)]
        if missing:
            return {"error": "INSUFFICIENT_CONTEXT", "missing": missing}
        return {"status": "ok"}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting Ladini Context MCP Server")
    mcp.run()
