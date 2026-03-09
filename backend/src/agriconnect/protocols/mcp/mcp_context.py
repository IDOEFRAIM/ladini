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

import json
import logging
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.Context")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect Context MCP Server")

# ────────────────────── Module-level state ────────────────────────────────

_optimizer = None
_session_factory = None
_llm_client = None
_context_cache: Dict[str, Dict[str, Any]] = {}
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
            from agriconnect.services.memory import (
                UserFarmProfile,
                EpisodicMemory,
                ProfileExtractor,
                ContextOptimizer,
            )
            _profile = UserFarmProfile(_session_factory)
            _episodic = EpisodicMemory(_session_factory, llm_client=_llm_client)
            _extractor = ProfileExtractor(_llm_client, _profile)
            _optimizer = ContextOptimizer(_profile, _episodic, _extractor)
            logger.info("ContextOptimizer loaded (lazy init)")
        except Exception as exc:
            logger.error("ContextOptimizer unavailable: %s", exc)
    return _optimizer


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
        return json.dumps({
            "error": "ContextOptimizer indisponible",
            "combined_context": "",
            "token_estimate": 0,
        })

    result = optimizer.build_context(
        user_id, query, zone=zone or None, crop=crop or None,
    )
    # cache result
    _context_cache[user_id] = result
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def get_token_budget(ctx: Context = None) -> str:
    """Retourne le budget tokens par composant du contexte."""
    if ctx:
        await ctx.info("Returning token budget")
    try:
        from agriconnect.services.memory.context_optimizer import TOKEN_BUDGETS
        return json.dumps(TOKEN_BUDGETS, ensure_ascii=False)
    except Exception:
        return json.dumps({
            "profile": 80,
            "episodes": 120,
            "metadata": 50,
            "total": 350,
        })


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
        return json.dumps({"status": "skipped", "reason": "ContextOptimizer indisponible"})

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
    return json.dumps({
        "server": "AgriConnect Context MCP Server",
        "cached_users": len(_context_cache),
        "optimizer_ready": _lazy_optimizer() is not None,
    })


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
        _configure(context_optimizer=context_optimizer, session_factory=session_factory, llm_client=llm_client)
        logger.info("MCP Context Server v2 (FastMCP) initialised")

    # ── MCP interface ────────────────────────────────────────────────

    @staticmethod
    def list_tools() -> list:
        return [
            {"name": "build_context", "description": "Construit le contexte utilisateur optimisé"},
            {"name": "get_token_budget", "description": "Budget tokens par composant"},
            {"name": "enrich_state", "description": "Enrichit un état avec le contexte mémoire"},
            {"name": "record_interaction", "description": "Enregistre une interaction"},
        ]

    @staticmethod
    def list_resources() -> list:
        return [
            {"uri": "context://status", "name": "Context Status", "description": "Health check", "mimeType": "application/json"},
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

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Synchronous call_tool returning ``{status, content}`` dict."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            raw = loop.run_until_complete(self._dispatch(name, arguments))
            return {
                "status": "ok",
                "content": [{"type": "text", "text": raw}],
            }
        except Exception as exc:
            logger.error("call_tool error (%s): %s", name, exc)
            return {"status": "error", "error": str(exc)}

    def read_resource(self, uri: str, params: dict = None) -> dict:
        """Synchronous resource reader."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            raw = loop.run_until_complete(context_status())
            return {
                "status": "ok",
                "contents": [{"uri": uri, "mimeType": "application/json", "text": raw}],
            }
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
            from agriconnect.protocols.core import CachePolicy
            if query:
                temp = CachePolicy(key="_check")
                bypass = temp.should_bypass(query)
            else:
                bypass = False
        except Exception:
            bypass = False

        if user_id in _context_cache and not bypass:
            return _context_cache[user_id]

        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        raw = loop.run_until_complete(
            build_context(user_id=user_id or "anonymous", query=query, zone=zone, crop=crop)
        )
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            result = {"user_id": user_id, "cached": False}
        if result.get("error"):
            return {"user_id": user_id, "cached": False}
        return result

    @staticmethod
    def check_required_fields(context: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
        """Vérifie que les champs requis sont présents dans le contexte."""
        missing = [f for f in required if not context.get(f)]
        if missing:
            return {"error": "INSUFFICIENT_CONTEXT", "missing": missing}
        return {"status": "ok"}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect Context MCP Server")
    mcp.run()
