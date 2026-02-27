"""
Agri-RAG MCP Server (Asynchronous).
Focus: Semantic Search, Agronomy Documents, and Episodic Memory.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .base import AsyncMCPServer
from agriconnect.rag.retriever import AgileRetriever

logger = logging.getLogger("MCP.AgriRAGServer")

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

# ────────────────────── Server ────────────────────────────────────────────

class AgriRAGMCPServer(AsyncMCPServer):
    """Async MCP server for agronomy RAG and vector search."""

    name = "agri_rag"

    def __init__(self, retriever=None) -> None:
        self._retriever = retriever
        super().__init__()

    def _lazy_retriever(self):
        """Initialise le retriever seulement si nécessaire pour économiser la RAM."""
        if self._retriever is None:
            try:
                self._retriever = AgileRetriever()
                logger.info("AgileRetriever chargé avec succès.")
            except Exception as exc:
                logger.error("Impossible de charger AgileRetriever: %s", exc)
        return self._retriever

    def _register_tools(self) -> None:
        # --- Recherche Documentaire ---
        self.register(
            name="search_agronomy_docs",
            description="Recherche sémantique dans les guides et fiches techniques agronomiques.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "La question technique ou le problème"},
                    "level": {"type": "string", "enum": ["debutant", "intermediaire", "expert"], "default": "debutant"},
                    "top_k": {"type": "integer", "default": 4},
                },
                "required": ["query"],
            },
            handler=self._search_agronomy_docs,
        )

        # --- Recherche Mémoire Épisodique (Vectorielle) ---
        self.register(
            name="search_past_interactions",
            description="Recherche dans l'historique significatif (mémoire) d'un utilisateur.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "query": {"type": "string", "description": "Ce que l'utilisateur a dit ou fait par le passé"},
                    "top_k": {"type": "integer", "default": 3},
                },
                "required": ["user_id", "query"],
            },
            handler=self._search_episodes,
        )

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _search_agronomy_docs(self, args: Dict[str, Any]) -> RAGPayload:
        query = args["query"]
        level = args.get("level", "debutant")
        top_k = int(args.get("top_k", 4))

        retriever = self._lazy_retriever()
        if not retriever:
            return RAGPayload(query=query)

        # Appel au moteur RAG (AgileRetriever)
        nodes = retriever.search(query, user_level=level, top_k=top_k)
        
        docs = []
        context_parts = []

        for n in (nodes or []):
            # Extraction propre que ce soit un Node ou un dict
            text = n.node.get_content() if hasattr(n, "node") else n.get("text", "")
            meta = n.node.metadata if hasattr(n, "node") else n.get("metadata", {})
            score = float(getattr(n, "score", 0.0))

            docs.append(RAGDocument(
                title=meta.get("title", "Document"),
                excerpt=text[:500], # Tronqué pour le payload
                source=meta.get("filename", "internal_db"),
                score=score,
                metadata=meta,
            ))
            context_parts.append(f"SOURCE: {meta.get('title')}\nCONTENU: {text}\n")

        return RAGPayload(
            query=query,
            documents=docs,
            context_text="\n---\n".join(context_parts),
            total_found=len(docs),
        )

    async def _search_episodes(self, args: Dict[str, Any]) -> List[EpisodeSummary]:
        """Simulation de recherche vectorielle via le retriever sur la table episodic_memories."""
        user_id = args["user_id"]
        query = args["query"]
        top_k = args.get("top_k", 3)

        retriever = self._lazy_retriever()
        # Ici on utilise une méthode spécifique du retriever pour la mémoire
        # ou un filtrage par métadonnées metadata={'user_id': user_id}
        results = retriever.search_memory(query, user_id=user_id, top_k=top_k)

        return [
            EpisodeSummary(
                episode_id=r.get("id", "0"),
                user_id=user_id,
                summary=r.get("text", ""),
                category=r.get("category"),
                relevance_score=float(r.get("score", 0.0))
            ) for r in results
        ]

if __name__ == "__main__":
    import asyncio
    
    async def run_test():
        server = AgriRAGMCPServer()
        print(f"🚀 Serveur RAG '{server.name}' initialisé.")
        print("Outils disponibles:", [t["name"] for t in server.list_tools()])

    asyncio.run(run_test())