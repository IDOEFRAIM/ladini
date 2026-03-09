"""
Agri-Data MCP Server (FastMCP).
Domain: RAG retrieval + vector search + pest/disease identification.

Tools:
  - search_agronomy_docs(query, level, top_k)       → RAGPayload
  - search_pest_disease(crop, symptoms, top_k)       → PestDiseasePayload
  - semantic_search_episodes(user_id, query, top_k)  → EpisodesPayload
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.AgriDataServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect Data MCP Server")

# ────────────────────── Lazy singletons ───────────────────────────────────

_retriever = None
_session_factory = None


def _lazy_retriever():
    global _retriever
    if _retriever is None:
        try:
            from agriconnect.rag.retriever import AgileRetriever
            _retriever = AgileRetriever()
            logger.info("AgileRetriever loaded (lazy)")
        except Exception as exc:
            logger.error("AgileRetriever unavailable: %s", exc)
    return _retriever


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


class PestDiseaseMatch(BaseModel):
    name: str
    crop: str
    symptoms: List[str] = Field(default_factory=list)
    treatment: str = ""
    prevention: str = ""
    severity: str = "UNKNOWN"


class PestDiseasePayload(BaseModel):
    crop: str
    matches: List[PestDiseaseMatch] = Field(default_factory=list)


class EpisodeSummary(BaseModel):
    episode_id: str
    user_id: str
    summary: str
    category: Optional[str] = None
    crop: Optional[str] = None
    relevance_score: float = 0.0


class EpisodesPayload(BaseModel):
    user_id: str
    episodes: List[EpisodeSummary] = Field(default_factory=list)


# ────────────────────── Tools ─────────────────────────────────────────────

@mcp.tool()
async def search_agronomy_docs(
    query: str,
    level: str = "debutant",
    top_k: int = 4,
    ctx: Context = None,
) -> str:
    """Recherche sémantique dans la base documentaire agronomique.

    Args:
        query: Question technique ou problème
        level: debutant | intermediaire | expert
        top_k: Nombre max de résultats
        ctx: MCP context for logging

    Returns:
        JSON RAGPayload
    """
    if ctx:
        await ctx.info(f"Data search: '{query}' level={level}")

    retriever = _lazy_retriever()
    if not retriever:
        return RAGPayload(query=query).model_dump_json(indent=2)

    nodes = retriever.search(query, user_level=level, top_k=top_k)
    docs: List[RAGDocument] = []
    context_parts: List[str] = []

    for n in (nodes or []):
        if hasattr(n, "node"):
            text = n.node.get_content()
            meta = n.node.metadata
            score = float(n.score or 0)
        else:
            text = n.get("text", "")
            meta = n.get("metadata", {})
            score = float(n.get("score", 0))

        docs.append(RAGDocument(
            title=meta.get("source", "Document"),
            excerpt=text[:500],
            source=meta.get("source", "unknown"),
            score=score,
            metadata=meta,
        ))
        context_parts.append(f"[{meta.get('source', 'Doc')}]\n{text}\n")

    if ctx:
        await ctx.report_progress(progress=len(docs), total=len(docs))

    payload = RAGPayload(
        query=query,
        documents=docs,
        context_text="\n---\n".join(context_parts),
        total_found=len(docs),
    )
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def search_pest_disease(
    crop: str,
    symptoms: str,
    top_k: int = 3,
    ctx: Context = None,
) -> str:
    """Identifie ravageurs et maladies à partir des symptômes décrits.

    Args:
        crop: Culture concernée (ex: maïs, coton)
        symptoms: Description libre des symptômes
        top_k: Nombre max de résultats
        ctx: MCP context for logging

    Returns:
        JSON PestDiseasePayload
    """
    if ctx:
        await ctx.info(f"Pest/disease search: crop={crop} symptoms='{symptoms}'")

    combined_query = f"maladie ravageur {crop} symptômes: {symptoms}"
    retriever = _lazy_retriever()
    matches: List[PestDiseaseMatch] = []

    if retriever:
        nodes = retriever.search(combined_query, user_level="expert", top_k=top_k)
        for n in (nodes or []):
            text = n.node.get_content() if hasattr(n, "node") else n.get("text", "")
            meta = n.node.metadata if hasattr(n, "node") else n.get("metadata", {})
            matches.append(PestDiseaseMatch(
                name=meta.get("title", "Pathologie inconnue"),
                crop=crop,
                symptoms=[symptoms],
                treatment=text[:200],
                prevention="",
                severity=meta.get("severity", "UNKNOWN"),
            ))

    payload = PestDiseasePayload(crop=crop, matches=matches)
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def semantic_search_episodes(
    user_id: str,
    query: str,
    top_k: int = 5,
    ctx: Context = None,
) -> str:
    """Recherche vectorielle dans la mémoire épisodique de l'agriculteur.

    Args:
        user_id: Identifiant de l'utilisateur
        query: Requête de recherche
        top_k: Nombre max de résultats
        ctx: MCP context for logging

    Returns:
        JSON EpisodesPayload
    """
    if ctx:
        await ctx.info(f"Episodic search user={user_id}: '{query}'")

    if _session_factory is None:
        logger.warning("No session_factory — episodic search unavailable")
        return EpisodesPayload(user_id=user_id).model_dump_json(indent=2)

    try:
        from sqlalchemy import text as sa_text
        from agriconnect.services.memory.episodic_memory import EpisodicMemoryModel

        embedding_str: Optional[str] = None
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            _enc = SentenceTransformer("all-MiniLM-L6-v2")
            vec = _enc.encode(query).tolist()
            embedding_str = str(vec)
        except Exception:
            pass

        session = _session_factory()
        try:
            if embedding_str:
                rows = session.execute(
                    sa_text(
                        f"SELECT * FROM {EpisodicMemoryModel.__table__.name} "
                        "WHERE user_id = :uid ORDER BY embedding <-> :emb LIMIT :lim"
                    ),
                    {"uid": user_id, "emb": embedding_str, "lim": top_k},
                )
                episodes = [
                    EpisodeSummary(
                        episode_id=str(r._mapping.get("id", "")),
                        user_id=user_id,
                        summary=r._mapping.get("content", ""),
                        category=r._mapping.get("category"),
                        crop=r._mapping.get("crop"),
                        relevance_score=float(r._mapping.get("relevance_score", 0)),
                    )
                    for r in rows
                ]
            else:
                eps = (
                    session.query(EpisodicMemoryModel)
                    .filter_by(user_id=user_id)
                    .order_by(EpisodicMemoryModel.relevance_score.desc())
                    .limit(top_k)
                    .all()
                )
                episodes = [
                    EpisodeSummary(
                        episode_id=str(ep.id),
                        user_id=user_id,
                        summary=ep.content if hasattr(ep, "content") else "",
                        category=getattr(ep, "category", None),
                        crop=getattr(ep, "crop", None),
                        relevance_score=float(getattr(ep, "relevance_score", 0)),
                    )
                    for ep in eps
                ]
        finally:
            session.close()

        payload = EpisodesPayload(user_id=user_id, episodes=episodes)
        return payload.model_dump_json(indent=2)
    except Exception as exc:
        logger.error("semantic_search_episodes failed: %s", exc)
        return EpisodesPayload(user_id=user_id).model_dump_json(indent=2)


# ────────────────────── Backward-compatible class wrapper ─────────────────

class AgriDataMCPServer:
    """Compat wrapper for in-process callers."""

    name = "agri_data"

    def __init__(self, retriever=None, session_factory=None):
        global _retriever, _session_factory
        if retriever is not None:
            _retriever = retriever
        if session_factory is not None:
            _session_factory = session_factory

    @staticmethod
    def list_tools():
        return [
            {"name": "search_agronomy_docs", "description": "Recherche sémantique documentaire agronomique"},
            {"name": "search_pest_disease", "description": "Identifie ravageurs et maladies"},
            {"name": "semantic_search_episodes", "description": "Recherche vectorielle mémoire épisodique"},
        ]

    @staticmethod
    async def _dispatch(name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "search_agronomy_docs": search_agronomy_docs,
            "search_pest_disease": search_pest_disease,
            "semantic_search_episodes": semantic_search_episodes,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown data tool: {name}")
        return await fn(**args)

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        raw = loop.run_until_complete(self._dispatch(name, arguments))
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect Data MCP Server")
    mcp.run()
