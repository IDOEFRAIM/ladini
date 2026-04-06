from __future__ import annotations

import json
import logging
import os
import re
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("AgriConnect.Services.RAG")


class RagService:
    """Application-level RAG service independent from transport/protocol layers."""

    def __init__(
        self,
        retriever: Any = None,
        retriever_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._retriever = retriever
        self._retriever_factory = retriever_factory

    @staticmethod
    def _safe_query_timeout() -> float:
        raw = (os.getenv("AGRICONNECT_RAG_QUERY_TIMEOUT_S", "25") or "25").strip()
        try:
            timeout = float(raw)
        except ValueError:
            logger.warning("Invalid AGRICONNECT_RAG_QUERY_TIMEOUT_S=%r, fallback to 25s", raw)
            return 25.0
        return max(1.0, min(timeout, 120.0))

    @staticmethod
    def _sanitize_level(level: str) -> str:
        allowed = {"debutant", "intermediaire", "expert"}
        normalized = (level or "debutant").strip().lower()
        return normalized if normalized in allowed else "debutant"

    @staticmethod
    def _sanitize_top_k(top_k: int) -> int:
        try:
            value = int(top_k)
        except Exception:
            return 4
        return max(1, min(value, 50))

    @staticmethod
    def _normalize_node(item: Any) -> Tuple[str, Dict[str, Any], float]:
        if hasattr(item, "node"):
            node = item.node
            text = str(node.get_content() if hasattr(node, "get_content") else "")
            meta = dict(getattr(node, "metadata", {}) or {})
            score = float(getattr(item, "score", 0.0) or 0.0)
            return text, meta, score

        if isinstance(item, dict):
            text = str(item.get("text") or item.get("content") or "")
            meta = dict(item.get("metadata") or item.get("meta") or {})
            score = float(item.get("score", 0.0) or 0.0)
            return text, meta, score

        text = str(getattr(item, "text", "") or "")
        meta = dict(getattr(item, "metadata", {}) or {})
        score = float(getattr(item, "score", 0.0) or 0.0)
        return text, meta, score

    @property
    def retriever_loaded(self) -> bool:
        return self._retriever is not None

    def _get_retriever(self) -> Any:
        if self._retriever is not None:
            return self._retriever
        factory = self._retriever_factory
        if factory is None:
            from agriconnect.rag.retriever import AgileRetriever

            factory = AgileRetriever
        try:
            logger.info("Initializing AgileRetriever from RagService")
            self._retriever = factory()
            return self._retriever
        except Exception as exc:
            logger.error("Retriever initialization failed: %s", exc)
            raise RuntimeError(f"Retriever non disponible : {exc}") from exc

    async def warmup(self) -> bool:
        try:
            retriever = await asyncio.to_thread(self._get_retriever)
            try:
                return bool(getattr(retriever, "ready", True))
            except Exception:
                return True
        except Exception as exc:
            logger.exception("RagService warmup failed: %s", exc)
            return False

    def _search_documents_sync(self, query: str, level: str = "debutant", top_k: int = 4) -> Dict[str, Any]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return {
                "query": "",
                "documents": [],
                "context_text": "",
                "total_found": 0,
                "status": "invalid_query",
            }

        level = self._sanitize_level(level)
        top_k = self._sanitize_top_k(top_k)
        retriever = self._get_retriever()
        nodes = retriever.search(normalized_query, user_level=level)
        nodes = (nodes or [])[:top_k]

        docs: List[Dict[str, Any]] = []
        context_parts: List[str] = []

        for n in nodes:
            text, meta, score = self._normalize_node(n)

            title_candidate = (
                meta.get("title")
                or meta.get("filename")
                or (os.path.basename(meta.get("source")) if meta.get("source") else None)
                or (os.path.basename(meta.get("file_path")) if meta.get("file_path") else None)
                or "Document sans titre"
            )

            excerpt_raw = text[:1200]
            excerpt_clean = re.sub(r'"[a-zA-Z0-9_\\-]+"\\s*:\\s*"[^"]+"', " ", excerpt_raw)
            excerpt_clean = re.sub(r"\\b(?:[A-Za-z]:)?[\\/][^\\s\\\\]{5,200}", " ", excerpt_clean)
            excerpt_clean = re.sub(r"https?://\\S+", " ", excerpt_clean)
            excerpt_clean = re.sub(r"\\s+", " ", excerpt_clean).strip()

            source = meta.get("file_path", meta.get("source", meta.get("filename", "db_interne")))
            docs.append(
                {
                    "title": title_candidate,
                    "excerpt": excerpt_clean,
                    "source": source,
                    "score": score,
                    "metadata": meta,
                }
            )
            context_parts.append(f"SOURCE: {title_candidate}\\nCONTENU: {text}\\n")

        return {
            "query": normalized_query,
            "documents": docs,
            "context_text": "\n---\n".join(context_parts),
            "total_found": len(docs),
        }

    async def search_documents(self, query: str, level: str = "debutant", top_k: int = 4) -> Dict[str, Any]:
        timeout_s = self._safe_query_timeout()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._search_documents_sync, query, level, top_k),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("RAG search timeout after %.1fs", timeout_s)
            return {
                "query": query,
                "documents": [],
                "context_text": "",
                "total_found": 0,
                "status": "timeout",
            }
        except Exception as exc:
            logger.exception("RAG search failed: %s", exc)
            return {
                "query": query,
                "documents": [],
                "context_text": "",
                "total_found": 0,
                "status": "error",
            }

    async def search_memory(self, user_id: str, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        retriever = self._get_retriever()
        query = (query or "").strip()
        if not query:
            return []
        top_k = self._sanitize_top_k(top_k)
        results = retriever.search_memory(user_id=user_id, query=query, top_k=top_k)
        episodes: List[Dict[str, Any]] = []
        for r in (results or []):
            episodes.append(
                {
                    "episode_id": str(r.get("id", "0")),
                    "user_id": user_id,
                    "summary": r.get("text", r.get("summary", "")),
                    "category": r.get("category"),
                    "relevance_score": float(r.get("score", 0.0)),
                }
            )
        return episodes

    async def status(self) -> Dict[str, Any]:
        return {
            "status": "ready" if self.retriever_loaded else "not_initialized",
            "engine": "AgileRetriever",
        }

    async def ping(self) -> Dict[str, Any]:
        return await self.status()

    @staticmethod
    def dumps(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)
