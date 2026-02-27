"""Agri-Data MCP Micro-Server (async).

Domain: RAG retrieval + vector search.
This server is *data-only*. No prose generation.

Tools exposed:
  - search_agronomy_docs(query, level, top_k) → RAGPayload
  - search_pest_disease(crop, symptoms)        → PestDiseasePayload
  - semantic_search_episodes(user_id, query)   → EpisodesPayload
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import AsyncMCPServer

logger = logging.getLogger("MCP.AgriDataServer")


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


# ────────────────────── Server ────────────────────────────────────────────

class AgriDataMCPServer(AsyncMCPServer):
    """Async MCP server for agronomy RAG and vector search."""

    name = "agri_data"

    def __init__(self, retriever=None, session_factory=None) -> None:
        self._retriever = retriever
        self._session_factory = session_factory
        super().__init__()

    def _lazy_retriever(self):
        if self._retriever is None:
            try:
                from agriconnect.rag.retriever import AgileRetriever
                self._retriever = AgileRetriever()
                logger.info("AgileRetriever loaded (lazy)")
            except Exception as exc:
                logger.error("AgileRetriever unavailable: %s", exc)
        return self._retriever

    def _register_tools(self) -> None:
        self.register(
            name="search_agronomy_docs",
            description="Recherche sémantique dans la base documentaire agronomique",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "level": {"type": "string", "enum": ["debutant", "intermediaire", "expert"], "default": "debutant"},
                    "top_k": {"type": "integer", "default": 4},
                },
                "required": ["query"],
            },
            handler=self._search_agronomy_docs,
        )
        self.register(
            name="search_pest_disease",
            description="Identifie ravageurs et maladies à partir des symptômes décrits",
            input_schema={
                "type": "object",
                "properties": {
                    "crop": {"type": "string"},
                    "symptoms": {"type": "string", "description": "Description libre des symptômes"},
                    "top_k": {"type": "integer", "default": 3},
                },
                "required": ["crop", "symptoms"],
            },
            handler=self._search_pest_disease,
        )
        self.register(
            name="semantic_search_episodes",
            description="Recherche vectorielle dans la mémoire épisodique de l'agriculteur",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["user_id", "query"],
            },
            handler=self._semantic_search_episodes,
        )

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _search_agronomy_docs(self, args: Dict[str, Any]) -> RAGPayload:
        query: str = args["query"]
        level: str = args.get("level", "debutant")
        top_k: int = int(args.get("top_k", 4))

        retriever = self._lazy_retriever()
        if not retriever:
            return RAGPayload(query=query)

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

        return RAGPayload(
            query=query,
            documents=docs,
            context_text="\n---\n".join(context_parts),
            total_found=len(docs),
        )

    async def _search_pest_disease(self, args: Dict[str, Any]) -> PestDiseasePayload:
        crop: str = args["crop"]
        symptoms: str = args.get("symptoms", "")
        top_k: int = int(args.get("top_k", 3))

        combined_query = f"maladie ravageur {crop} symptômes: {symptoms}"
        retriever = self._lazy_retriever()
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

        return PestDiseasePayload(crop=crop, matches=matches)

    async def _semantic_search_episodes(self, args: Dict[str, Any]) -> EpisodesPayload:
        """Vector search over episodic memory using pgvector when available."""
        user_id: str = args["user_id"]
        query: str = args["query"]
        top_k: int = int(args.get("top_k", 5))

        if self._session_factory is None:
            logger.warning("No session_factory — episodic search unavailable")
            return EpisodesPayload(user_id=user_id)

        try:
            from sqlalchemy import text as sa_text
            from agriconnect.services.memory.episodic_memory import EpisodicMemoryModel

            # Attempt to encode the query via a lightweight model if available
            embedding_str: Optional[str] = None
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
                _enc = SentenceTransformer("all-MiniLM-L6-v2")
                vec = _enc.encode(query).tolist()
                embedding_str = str(vec)
            except Exception:
                pass  # Fall back to relevance_score ordering

            session = self._session_factory()
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
                    # Fallback: ORM query sorted by relevance_score
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

            return EpisodesPayload(user_id=user_id, episodes=episodes)
        except Exception as exc:
            logger.error("semantic_search_episodes failed: %s", exc)
            return EpisodesPayload(user_id=user_id)


if __name__ == "__main__":
    import asyncio, json

    server = AgriDataMCPServer()
    print("Tools:", [t["name"] for t in server.list_tools()])
