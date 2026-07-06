from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from sqlalchemy import text

from agriconnect.core.db import get_engine, resolve_database_url

logger = logging.getLogger("agriconnect.rag.pgvector_store")


class PgVectorStore:
    """PostgreSQL/pgvector-backed vector store.

    The store uses `agri_vector.document_chunks` to align runtime retrieval with
    the existing Airflow ingestion DAGs.
    """

    def __init__(self, db_url: str | None = None, dim: int = 384):
        self.db_url = (db_url or "").strip() or resolve_database_url(required=True)
        self.engine = get_engine(self.db_url)
        self.dim = int(dim)
        self.ready = False
        self._ensure_schema()

    def is_embedding_query(self, q: Any = None) -> bool:
        return True

    def _ensure_schema(self) -> None:
        ddl = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            "CREATE SCHEMA IF NOT EXISTS agri_vector",
            """
            CREATE TABLE IF NOT EXISTS agri_vector.document_chunks (
                id BIGSERIAL PRIMARY KEY,
                doc_ref TEXT NOT NULL,
                doc_type TEXT,
                category TEXT,
                zone_name TEXT,
                content TEXT NOT NULL,
                content_md5 TEXT NOT NULL,
                embedding vector,
                metadata JSONB,
                source_uri TEXT,
                chunk_version INTEGER NOT NULL DEFAULT 1,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_ref_md5_ver
            ON agri_vector.document_chunks (doc_ref, content_md5, chunk_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_cosine
            ON agri_vector.document_chunks
            USING ivfflat (embedding vector_cosine_ops)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_document_chunks_active
            ON agri_vector.document_chunks (is_active)
            """,
        ]
        try:
            with self.engine.begin() as conn:
                for stmt in ddl:
                    conn.execute(text(stmt))
            self.ready = True
        except Exception as exc:
            logger.warning("PgVector store schema init failed: %s", exc)
            self.ready = False

    @staticmethod
    def _embedding_literal(embedding: Sequence[float]) -> str:
        return "[" + ",".join(str(float(x)) for x in embedding) + "]"

    @staticmethod
    def _md5(text_value: str) -> str:
        return hashlib.md5((text_value or "").encode("utf-8", errors="ignore")).hexdigest()

    def _iter_node_payloads(self, nodes: Iterable[Any]) -> Iterable[Tuple[str, str, Dict[str, Any], List[float]]]:
        for n in nodes:
            node_id = str(getattr(n, "id_", "") or getattr(n, "node_id", "") or "")
            node_text = str(getattr(n, "text", "") or "")
            node_meta = dict(getattr(n, "metadata", {}) or {})
            node_emb = list(getattr(n, "embedding", None) or [])
            if not node_id:
                node_id = f"doc_{self._md5(node_text)[:12]}"
            if not node_text or not node_emb:
                continue
            yield node_id, node_text, node_meta, node_emb

    def add(self, *args, **kwargs) -> None:
        """Support both APIs:
        - add(nodes)
        - add(doc_id, text, meta, embedding)
        """
        rows: List[Tuple[str, str, Dict[str, Any], List[float]]] = []
        if len(args) == 1 and isinstance(args[0], list):
            rows.extend(self._iter_node_payloads(args[0]))
        elif len(args) >= 4:
            doc_id, text_value, meta, embedding = args[:4]
            rows.append((str(doc_id), str(text_value or ""), dict(meta or {}), list(embedding or [])))
        else:
            raise TypeError("Unsupported add signature for PgVectorStore")

        if not rows:
            return

        sql = text(
            """
            INSERT INTO agri_vector.document_chunks (
                doc_ref, doc_type, category, zone_name, content, content_md5,
                embedding, metadata, source_uri, chunk_version, is_active
            ) VALUES (
                :doc_ref, :doc_type, :category, :zone_name, :content, :content_md5,
                CAST(:embedding_literal AS vector), CAST(:metadata_json AS jsonb), :source_uri, :chunk_version, TRUE
            )
            ON CONFLICT (doc_ref, content_md5, chunk_version)
            DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                source_uri = EXCLUDED.source_uri,
                is_active = TRUE,
                updated_at = NOW()
            """
        )

        with self.engine.begin() as conn:
            for doc_ref, text_value, meta, emb in rows:
                if len(emb) != self.dim:
                    continue
                payload = {
                    "doc_ref": doc_ref,
                    "doc_type": str(meta.get("doc_type") or "raw_data"),
                    "category": str(meta.get("category") or "ingested"),
                    "zone_name": meta.get("zone") or meta.get("zone_name"),
                    "content": text_value,
                    "content_md5": self._md5(text_value),
                    "embedding_literal": self._embedding_literal(emb),
                    "metadata_json": json.dumps(meta, ensure_ascii=False),
                    "source_uri": str(meta.get("source_uri") or meta.get("source") or doc_ref),
                    "chunk_version": int(meta.get("chunk_version") or 1),
                }
                conn.execute(sql, payload)

    def _extract_embedding(self, obj: Any) -> Tuple[List[float], int]:
        default_top_k = 5
        if isinstance(obj, (list, tuple)):
            return list(obj), default_top_k

        for attr in ("query_embedding", "embedding", "vector", "values"):
            value = getattr(obj, attr, None)
            if value is not None:
                top_k = int(getattr(obj, "similarity_top_k", default_top_k) or default_top_k)
                return list(value), top_k

        raise TypeError(f"Unsupported query embedding type: {type(obj)}")

    def _make_result(self, nodes, similarities, ids):
        try:
            from llama_index.core.vector_stores.types import VectorStoreQueryResult

            return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
        except Exception:
            return SimpleNamespace(nodes=nodes, similarities=similarities, ids=ids)

    def query(self, query_embedding: Any, k: int = 5):
        try:
            q_vec, top_k = self._extract_embedding(query_embedding)
        except Exception as exc:
            logger.warning("PgVector query embedding extraction failed: %s", exc)
            return self._make_result([], [], [])

        if not q_vec:
            return self._make_result([], [], [])
        top_k = int(top_k or k or 5)
        emb_literal = self._embedding_literal(q_vec)

        sql = text(
            """
            SELECT
                doc_ref,
                content,
                metadata,
                (1 - (embedding <=> CAST(:embedding_literal AS vector))) AS score
            FROM agri_vector.document_chunks
            WHERE is_active = TRUE
              AND embedding IS NOT NULL
              AND vector_dims(embedding) = :dim
            ORDER BY embedding <=> CAST(:embedding_literal AS vector)
            LIMIT :k
            """
        )

        out = []
        with self.engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "embedding_literal": emb_literal,
                    "dim": self.dim,
                    "k": top_k,
                },
            ).fetchall()

        for row in rows:
            meta = row[2] or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            out.append(
                {
                    "id": str(row[0]),
                    "text": str(row[1] or ""),
                    "meta": dict(meta or {}),
                    "score": float(row[3] or 0.0),
                }
            )

        try:
            from llama_index.core.schema import TextNode

            nodes = [TextNode(text=o["text"], metadata=o["meta"], id_=o["id"]) for o in out]
        except Exception:
            nodes = [SimpleNamespace(node_id=o["id"], get_content=(lambda o=o: o["text"]), metadata=o["meta"]) for o in out]

        similarities = [float(o["score"]) for o in out]
        ids = [o["id"] for o in out]
        return self._make_result(nodes, similarities, ids)
