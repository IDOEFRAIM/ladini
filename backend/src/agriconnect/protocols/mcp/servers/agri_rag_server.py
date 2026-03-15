"""
Agri-RAG MCP Server (FastMCP).
Version corrigée sans Mock - Connexion directe à AgileRetriever.
"""

from __future__ import annotations

import json
import logging
import asyncio
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context
from agriconnect.rag.retriever import AgileRetriever

# Configuration du logging pour voir les erreurs dans la console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MCP.AgriRAGServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect RAG MCP Server")

# ────────────────────── Singleton Retriever ───────────────────────────────

_retriever = None

def _get_retriever():
    """Récupère ou initialise le retriever. Lève une erreur s'il échoue."""
    global _retriever
    if _retriever is None:
        try:
            logger.info("Tentative d'initialisation de AgileRetriever...")
            _retriever = AgileRetriever()
            logger.info("AgileRetriever chargé avec succès.")
        except Exception as e:
            logger.error(f"CRITICAL: Impossible de charger AgileRetriever: {e}")
            raise RuntimeError(f"Retriever non disponible : {str(e)}")
    return _retriever

# ────────────────────── Pydantic models ───────────────────────────────────

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
    """Recherche sémantique dans les guides agronomiques."""
    # Send an informational event to the caller if transport is alive. The
    # underlying transport (SSE/stdio) may be disconnected which raises an
    # exception when sending; guard to avoid crashing the server.
    if ctx:
        try:
            await ctx.info(f"Recherche RAG : '{query}' (Niveau: {level})")
        except Exception as e:
            logger.debug("ctx.info() failed (client may be disconnected): %s", e)

    try:
        retriever = _get_retriever()
        # Appel au moteur de recherche
        nodes = retriever.search(query, user_level=level)
        nodes = (nodes or [])[:top_k]

        docs = []
        context_parts = []

        for n in nodes:
            # Gestion hybride : supporte les objets LlamaIndex (n.node) ou les dicts
            if hasattr(n, "node"):
                text = n.node.get_content()
                meta = n.node.metadata
                score = float(getattr(n, "score", 0.0))
            else:
                text = n.get("text", "")
                meta = n.get("metadata", {})
                score = float(n.get("score", 0.0))

            # Log metadata keys to debug poor metadata quality
            logger.info("Node metadata: %s", meta)

            # Derive a readable title and a clean source/uri from metadata
            import os as _os
            title_candidate = (
                meta.get("title")
                or meta.get("filename")
                or (lambda p: _os.path.basename(p) if p else None)(meta.get("source"))
                or (lambda p: _os.path.basename(p) if p else None)(meta.get("file_path"))
                or "Document sans titre"
            )

            # Clean the excerpt: remove embedded JSON fragments, file_path keys and collapse whitespace
            try:
                import re as _re

                excerpt_raw = text[:1200]
                # Remove common JSON-like fields that leak into text
                excerpt_clean = _re.sub(r'"[a-zA-Z0-9_\-]+"\s*:\s*"[^"]+"', ' ', excerpt_raw)
                # Remove file paths and long URLs
                excerpt_clean = _re.sub(r'\\b(?:[A-Za-z]:)?[\\/][^\s\\]{5,200}', ' ', excerpt_clean)
                excerpt_clean = _re.sub(r'https?://\S+', ' ', excerpt_clean)
                # Collapse whitespace
                excerpt_clean = _re.sub(r'\s+', ' ', excerpt_clean).strip()
            except Exception:
                excerpt_clean = text[:500]

            doc = RAGDocument(
                title=title_candidate,
                excerpt=excerpt_clean,
                source=meta.get("file_path", meta.get("source", meta.get("filename", "db_interne"))),
                score=score,
                metadata=meta,
            )
            docs.append(doc)
            context_parts.append(f"SOURCE: {doc.title}\nCONTENU: {text}\n")

        payload = RAGPayload(
            query=query,
            documents=docs,
            context_text="\n---\n".join(context_parts),
            total_found=len(docs),
        )
        return payload.model_dump_json(indent=2)

    except Exception as e:
        logger.exception("Erreur lors de search_agronomy_docs")
        return json.dumps({"error": str(e), "query": query})

@mcp.tool()
async def search_past_interactions(
    user_id: str,
    query: str,
    top_k: int = 3,
    ctx: Context = None,
) -> str:
    """Recherche dans la mémoire épisodique de l'utilisateur."""
    try:
        retriever = _get_retriever()
        results = retriever.search_memory(query, user_id=user_id, top_k=top_k)
        
        episodes = []
        for r in (results or []):
            # Extraction sécurisée des données de l'épisode
            ep = EpisodeSummary(
                episode_id=str(r.get("id", "0")),
                user_id=user_id,
                summary=r.get("text", r.get("summary", "")),
                category=r.get("category"),
                relevance_score=float(r.get("score", 0.0)),
            )
            episodes.append(ep)
            
        return json.dumps([e.model_dump() for e in episodes], ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur mémoire épisodique: {e}")
        return "[]"

# ────────────────────── Resources & Status ────────────────────────────────

@mcp.resource("rag://status")
async def rag_server_status() -> str:
    """Vérifie l'état de la connexion au moteur RAG."""
    global _retriever
    status = "ready" if _retriever else "not_initialized"
    return json.dumps({"status": status, "engine": "AgileRetriever"})

# ────────────────────── Wrapper Compatibilité ─────────────────────────────

class AgriRAGMCPServer:
    """Wrapper pour compatibilité avec les anciens appels synchrones."""
    def __init__(self, retriever=None):
        global _retriever
        if retriever:
            _retriever = retriever

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        # Run the selected coroutine in a fresh event loop to avoid
        # "no current event loop in thread" errors when called from
        # a threadpool/executor.
        if name == "search_agronomy_docs":
            coro = search_agronomy_docs(**arguments)
        elif name == "search_past_interactions":
            coro = search_past_interactions(**arguments)
        elif name == "list_tools":
            # Provide a synchronous-compatible listing for tool discovery
            return {"ok": True, "data": [
                {"name": "search_agronomy_docs", "description": "Semantic search in agronomy guides"},
                {"name": "search_past_interactions", "description": "Search user episodic memory"},
                {"name": "rag://status", "description": "RAG server status resource"},
            ]}
        else:
            return {"ok": False, "error": f"Tool {name} inconnu"}

        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            result = new_loop.run_until_complete(coro)
        finally:
            try:
                new_loop.run_until_complete(new_loop.shutdown_asyncgens())
            except Exception:
                pass
            new_loop.close()
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass

        return {"ok": True, "data": json.loads(result)}

    def list_tools(self) -> list:
        """Return a stable list of tools exposed by this RAG server (sync helper)."""
        return [
            {"name": "search_agronomy_docs", "description": "Semantic search in agronomy guides"},
            {"name": "search_past_interactions", "description": "Search user episodic memory"},
            {"name": "rag://status", "description": "RAG server status resource"},
        ]

# ────────────────────── Lancement ─────────────────────────────────────────

if __name__ == "__main__":
    # On force l'init au démarrage pour voir les erreurs de suite
    try:
        _get_retriever()
    except Exception:
        pass 
    
    mcp.run()