"""
Agri-RAG MCP Server (FastMCP).
Domain: Semantic Search in agronomy documents and episodic memory.

Tools:
  - search_agronomy_docs(query, level, top_k)  → RAGPayload (JSON)
  - search_past_interactions(user_id, query)    → EpisodeSummary list (JSON)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.AgriRAGServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect RAG MCP Server")

# ────────────────────── Lazy singleton ────────────────────────────────────

_retriever = None
_retriever_loading = False


def _lazy_retriever():
    """Non-blocking lazy initialisation of the retriever.

    To avoid lengthy imports (transformers/torch) blocking MCP calls,
    start a background thread to load the retriever on first demand and
    return None immediately if not ready. Subsequent calls will use the
    loaded retriever when available.
    """
    global _retriever, _retriever_loading
    if _retriever is not None:
        return _retriever

    if _retriever_loading:
        # Already being loaded in background
        return None

    # Start background loader
    import threading

    def _load():
        global _retriever, _retriever_loading
        try:
            from agriconnect.rag.retriever import AgileRetriever
            _retriever = AgileRetriever()
            logger.info("AgileRetriever chargé avec succès (background).")
        except Exception as exc:
            logger.exception("Impossible de charger AgileRetriever en background: %s", exc)
        finally:
            _retriever_loading = False

    _retriever_loading = True
    t = threading.Thread(target=_load, name="agile_retriever_loader", daemon=True)
    t.start()
    return None


# ────────────────────── Pydantic response models ──────────────────────────

class RAGDocument(BaseModel):
    title: str
    excerpt: str
    source: str
    score: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RAGPayload(BaseModel):
    query: str
    documents: List[RAGDocument] = Field(default_factory=list)
    context_text: str = ""
    total_found: int = 0


class EpisodeSummary(BaseModel):
    episode_id: str
    user_id: str
    summary: str
    category: Optional[str] = None
    relevance_score: float = 0.0


# ────────────────────── Tools ─────────────────────────────────────────────

@mcp.tool()
async def search_agronomy_docs(
    query: str,
    level: str = "debutant",
    top_k: int = 4,
    ctx: Context = None,
) -> str:
    """Recherche sémantique dans les guides et fiches techniques agronomiques.

    Args:
        query: La question technique ou le problème
        level: Niveau utilisateur (debutant, intermediaire, expert)
        top_k: Nombre max de résultats
        ctx: MCP context for logging

    Returns:
        JSON RAGPayload with documents and assembled context_text
    """
    if ctx:
        await ctx.info(f"RAG search: '{query}' level={level} top_k={top_k}")

    retriever = _lazy_retriever()
    if not retriever:
        return RAGPayload(query=query).model_dump_json(indent=2)

    nodes = retriever.search(query, user_level=level)
    nodes = (nodes or [])[:top_k]

    docs: List[RAGDocument] = []
    context_parts: List[str] = []

    for n in nodes:
        text = n.node.get_content() if hasattr(n, "node") else n.get("text", "")
        meta = n.node.metadata if hasattr(n, "node") else n.get("metadata", {})
        score = float(getattr(n, "score", 0.0))

        docs.append(RAGDocument(
            title=meta.get("title", "Document"),
            excerpt=text[:500],
            source=meta.get("filename", "internal_db"),
            score=score,
            metadata=meta,
        ))
        context_parts.append(f"SOURCE: {meta.get('title')}\nCONTENU: {text}\n")

    if ctx:
        await ctx.report_progress(progress=len(docs), total=len(docs), message=f"Found {len(docs)} documents")

    payload = RAGPayload(
        query=query,
        documents=docs,
        context_text="\n---\n".join(context_parts),
        total_found=len(docs),
    )
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def search_past_interactions(
    user_id: str,
    query: str,
    top_k: int = 3,
    ctx: Context = None,
) -> str:
    """Recherche dans l'historique significatif (mémoire épisodique) d'un utilisateur.

    Args:
        user_id: Identifiant de l'utilisateur
        query: Ce que l'utilisateur a dit ou fait par le passé
        top_k: Nombre max de résultats
        ctx: MCP context for logging

    Returns:
        JSON list of EpisodeSummary
    """
    if ctx:
        await ctx.info(f"Episodic search for user={user_id}: '{query}'")

    retriever = _lazy_retriever()
    if not retriever:
        return "[]"

    results = retriever.search_memory(query, user_id=user_id, top_k=top_k)
    episodes = [
        EpisodeSummary(
            episode_id=r.get("id", "0"),
            user_id=user_id,
            summary=r.get("text", ""),
            category=r.get("category"),
            relevance_score=float(r.get("score", 0.0)),
        )
        for r in (results or [])
    ]
    return json.dumps([e.model_dump() for e in episodes], ensure_ascii=False, indent=2)


# ────────────────────── Resources ─────────────────────────────────────────

@mcp.resource("rag://status")
async def rag_server_status() -> str:
    """Health-check for the RAG server."""
    r = _lazy_retriever()
    return json.dumps({"status": "ok", "retriever_loaded": bool(r)})


# ────────────────────── Backward-compatible class wrapper ─────────────────

class AgriRAGMCPServer:
    """Compat wrapper: existing callers can still use call_tool(name, args).

    New code should import the FastMCP tool functions directly or
    run the server via ``mcp.run()``.
    """

    name = "agri_rag"

    def __init__(self, retriever=None):
        global _retriever
        if retriever is not None:
            _retriever = retriever

    @staticmethod
    def list_tools():
        return [
            {"name": "search_agronomy_docs", "description": "Recherche sémantique dans les guides agronomiques."},
            {"name": "search_past_interactions", "description": "Recherche dans la mémoire épisodique."},
        ]

    @staticmethod
    async def _dispatch(name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "search_agronomy_docs": search_agronomy_docs,
            "search_past_interactions": search_past_interactions,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown RAG tool: {name}")
        return await fn(**args)

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        import asyncio
        import time
        try:
            # Prefer getting a running loop to avoid DeprecationWarning when
            # called from a sync context with no event loop running.
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        # Ensure retriever is loaded (wait briefly for background loader),
        # otherwise attempt a synchronous load so CLI callers get results.
        # Kick off background loader if not already; wait briefly for it to finish.
        global _retriever, _retriever_loading
        if _retriever is None:
            _lazy_retriever()
            waited = 0.0
            while _retriever is None and _retriever_loading and waited < 3.0:
                time.sleep(0.5)
                waited += 0.5

        raw = loop.run_until_complete(self._dispatch(name, arguments))
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect RAG MCP Server")
    mcp.run()
