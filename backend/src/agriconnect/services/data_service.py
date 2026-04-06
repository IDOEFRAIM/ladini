from __future__ import annotations

import logging
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from agriconnect.protocols.shared.schemas import EpisodeSummary, RAGDocument, RAGPayload

logger = logging.getLogger("agriconnect.services.data")


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


class EpisodesPayload(BaseModel):
    user_id: str
    episodes: List[EpisodeSummary] = Field(default_factory=list)


class DataService:
    """Domain service to expose data retrieval operations used by MCP providers.

    This extracts RAG/data lookup logic out of transport/providers so it can be
    reused and unit-tested independently.
    """

    def __init__(
        self,
        retriever: Any = None,
        retriever_factory: Any = None,
        session_factory: Any = None,
    ) -> None:
        self._retriever = retriever
        self._retriever_factory = retriever_factory
        self._session_factory = session_factory

    def _lazy_retriever(self) -> Any:
        if self._retriever is None:
            try:
                factory = self._retriever_factory
                if factory is None:
                    from agriconnect.rag.retriever import AgileRetriever

                    factory = AgileRetriever
                self._retriever = factory()
                logger.info("AgileRetriever loaded in DataService")
            except Exception as exc:
                logger.error("AgileRetriever unavailable: %s", exc)
        return self._retriever

    async def search_agronomy_docs(self, query: str, level: str = "debutant", top_k: int = 4, ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"DataService search: '{query}' level={level}")

        retriever = self._lazy_retriever()
        if not retriever:
            return RAGPayload(query=query).model_dump_json(indent=2)

        try:
            nodes = retriever.search(query, user_level=level)
        except TypeError:
            nodes = retriever.search(query, user_level=level)

        if isinstance(nodes, list):
            nodes = nodes[:top_k]

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

            docs.append(
                RAGDocument(
                    title=meta.get("source", "Document"),
                    excerpt=text[:500],
                    source=meta.get("source", "unknown"),
                    score=score,
                    metadata=meta,
                )
            )
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

    async def search_pest_disease(self, crop: str, symptoms: str, top_k: int = 3, ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"Pest/disease search: crop={crop} symptoms='{symptoms}'")

        combined_query = f"maladie ravageur {crop} symptomes: {symptoms}"
        retriever = self._lazy_retriever()
        matches: List[PestDiseaseMatch] = []

        if retriever:
            try:
                nodes = retriever.search(combined_query, user_level="expert")
            except TypeError:
                nodes = retriever.search(combined_query, user_level="expert")

            if isinstance(nodes, list):
                nodes = nodes[:top_k]
            for n in (nodes or []):
                text = n.node.get_content() if hasattr(n, "node") else n.get("text", "")
                meta = n.node.metadata if hasattr(n, "node") else n.get("metadata", {})
                matches.append(
                    PestDiseaseMatch(
                        name=meta.get("title", "Pathologie inconnue"),
                        crop=crop,
                        symptoms=[symptoms],
                        treatment=text[:200],
                        prevention="",
                        severity=meta.get("severity", "UNKNOWN"),
                    )
                )

        return PestDiseasePayload(crop=crop, matches=matches).model_dump_json(indent=2)

    async def semantic_search_episodes(self, user_id: str, query: str, top_k: int = 5, ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"Episodic search user={user_id}: '{query}'")

        if self._session_factory is None:
            logger.warning("No session_factory - episodic search unavailable")
            return EpisodesPayload(user_id=user_id).model_dump_json(indent=2)

        try:
            from sqlalchemy import text as sa_text
            from agriconnect.services.memory.episodic_memory import EpisodicMemoryModel

            embedding_str: Optional[str] = None
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                enc = SentenceTransformer("all-MiniLM-L6-v2")
                vec = enc.encode(query).tolist()
                embedding_str = str(vec)
            except Exception:
                pass

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
                            relevance_score=float(getattr(ep, "relevance_score", 0)),
                        )
                        for ep in eps
                    ]
            finally:
                session.close()

            return EpisodesPayload(user_id=user_id, episodes=episodes).model_dump_json(indent=2)
        except Exception as exc:
            logger.error("semantic_search_episodes failed: %s", exc)
            return EpisodesPayload(user_id=user_id).model_dump_json(indent=2)

    async def ping(self) -> dict:
        return {
            "status": "ready",
            "retriever_loaded": bool(self._retriever),
            "session_factory_bound": bool(self._session_factory),
        }
