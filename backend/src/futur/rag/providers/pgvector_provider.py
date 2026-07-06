from __future__ import annotations

import hashlib
import json
import logging
from typing import Dict, List

from sqlalchemy import text

from agriconnect.core.db import get_engine, resolve_database_url
from futur.rag.core.models import Node, QueryBundle
from futur.rag.interfaces import BaseVectorStore

logger = logging.getLogger("agriconnect.rag.providers.pgvector")


class PgVectorProvider(BaseVectorStore):
    def __init__(self, db_url: str | None = None, dim: int = 384):
        self.db_url = (db_url or "").strip() or resolve_database_url(required=True)
        self.engine = get_engine(self.db_url)
        self.dim = int(dim)
        self.ready = False
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        ddl = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            "CREATE SCHEMA IF NOT EXISTS agri_vector",
            f"""
            CREATE TABLE IF NOT EXISTS agri_vector.document_chunks (
                id BIGSERIAL PRIMARY KEY,
                doc_ref TEXT NOT NULL,
                doc_type TEXT,
                category TEXT,
                zone_name TEXT,
                content TEXT NOT NULL,
                content_md5 TEXT NOT NULL,
                embedding vector({self.dim}),
                metadata JSONB,
                source_uri TEXT,
                chunk_version INTEGER NOT NULL DEFAULT 1,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                is_indexed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_ref_md5_ver
            ON agri_vector.document_chunks (doc_ref, content_md5, chunk_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_agri_vector_document_chunks_embedding_cosine
            ON agri_vector.document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_agri_vector_document_chunks_is_indexed
            ON agri_vector.document_chunks (is_indexed)
            """,
        ]
        try:
            with self.engine.begin() as conn:
                for stmt in ddl:
                    conn.execute(text(stmt))
            self.ready = True
        except Exception as exc:
            logger.warning("PgVector provider schema init failed: %s", exc)
            self.ready = False

    @staticmethod
    def _embedding_literal(embedding: List[float]) -> str:
        return "[" + ",".join(str(float(x)) for x in embedding) + "]"

    @staticmethod
    def _md5(text_value: str) -> str:
        return hashlib.md5((text_value or "").encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _normalize_cosine_distance(distance: float) -> float:
        # pgvector cosine distance is typically in [0, 2], map to similarity [0, 1].
        d = float(distance or 0.0)
        sim = 1.0 - (d / 2.0)
        if sim < 0.0:
            return 0.0
        if sim > 1.0:
            return 1.0
        return sim

    def _warn_if_large_seq_scan(self, conn, embedding_literal: str, top_k: int, is_active: bool, is_indexed: bool) -> None:
        explain_sql = text(
            """
            EXPLAIN (FORMAT JSON)
            SELECT id
            FROM agri_vector.document_chunks
            WHERE is_active = :is_active
              AND is_indexed = :is_indexed
              AND embedding IS NOT NULL
              AND vector_dims(embedding) = :dim
            ORDER BY embedding <=> CAST(:embedding_literal AS vector)
            LIMIT :k
            """
        )
        try:
            plan_row = conn.execute(
                explain_sql,
                {
                    "embedding_literal": embedding_literal,
                    "dim": self.dim,
                    "k": int(top_k),
                    "is_active": is_active,
                    "is_indexed": is_indexed,
                },
            ).scalar()
            if not plan_row:
                return
            if isinstance(plan_row, str):
                plan_json = json.loads(plan_row)
            else:
                plan_json = plan_row

            root = None
            if isinstance(plan_json, list) and plan_json:
                root = (plan_json[0] or {}).get("Plan")
            elif isinstance(plan_json, dict):
                root = plan_json.get("Plan")
            if not isinstance(root, dict):
                return

            stack = [root]
            while stack:
                node = stack.pop()
                node_type = str(node.get("Node Type") or "")
                relation = str(node.get("Relation Name") or "")
                plan_rows = int(node.get("Plan Rows") or 0)
                if (
                    relation == "document_chunks"
                    and "Seq Scan" in node_type
                    and plan_rows > 1000
                ):
                    logger.warning(
                        "PgVector query may be running without HNSW index (seq scan rows=%s)",
                        plan_rows,
                    )
                    return
                for child in list(node.get("Plans") or []):
                    if isinstance(child, dict):
                        stack.append(child)
        except Exception as exc:
            logger.debug("Could not evaluate pgvector index health plan: %s", exc)

    def add(self, doc_id: str, text_value: str, meta: Dict, embedding: List[float]) -> None:
        if len(embedding or []) != self.dim:
            return
        sql = text(
            """
            INSERT INTO agri_vector.document_chunks (
                doc_ref, doc_type, category, zone_name, content, content_md5,
                embedding, metadata, source_uri, chunk_version, is_active, is_indexed
            ) VALUES (
                :doc_ref, :doc_type, :category, :zone_name, :content, :content_md5,
                CAST(:embedding_literal AS vector), CAST(:metadata_json AS jsonb), :source_uri, :chunk_version, TRUE, :is_indexed
            )
            ON CONFLICT (doc_ref, content_md5, chunk_version)
            DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                source_uri = EXCLUDED.source_uri,
                is_active = TRUE,
                is_indexed = EXCLUDED.is_indexed,
                updated_at = NOW()
            """
        )
        payload = {
            "doc_ref": str(doc_id),
            "doc_type": str((meta or {}).get("doc_type") or "raw_data"),
            "category": str((meta or {}).get("category") or "ingested"),
            "zone_name": (meta or {}).get("zone") or (meta or {}).get("zone_name"),
            "content": str(text_value or ""),
            "content_md5": self._md5(str(text_value or "")),
            "embedding_literal": self._embedding_literal(list(embedding or [])),
            "metadata_json": json.dumps(meta or {}, ensure_ascii=False),
            "source_uri": str((meta or {}).get("source_uri") or (meta or {}).get("source") or doc_id),
            "chunk_version": int((meta or {}).get("chunk_version") or 1),
            "is_indexed": bool((meta or {}).get("is_indexed", True)),
        }
        with self.engine.begin() as conn:
            conn.execute(sql, payload)

    def add_many(self, items: List[Dict], batch_size: int = 50) -> None:
        if not items:
            return
        for item in items:
            self.add(
                doc_id=str(item.get("id") or ""),
                text_value=str(item.get("text") or ""),
                meta=dict(item.get("meta") or {}),
                embedding=list(item.get("embedding") or []),
            )

    def query(self, query_bundle: QueryBundle) -> List[Node]:
        vec = list(query_bundle.vector or [])
        if len(vec) != self.dim:
            return []
        top_k = max(1, int(query_bundle.top_k or 5))
        filters = dict(query_bundle.filters or {})
        is_active = bool(filters.get("is_active", True))
        is_indexed = bool(filters.get("is_indexed", True))

        sql = text(
            """
            SELECT
                doc_ref,
                content,
                metadata,
                (embedding <=> CAST(:embedding_literal AS vector)) AS cosine_distance
            FROM agri_vector.document_chunks
            WHERE is_active = :is_active
              AND is_indexed = :is_indexed
              AND embedding IS NOT NULL
              AND vector_dims(embedding) = :dim
            ORDER BY embedding <=> CAST(:embedding_literal AS vector)
            LIMIT :k
            """
        )
        out: List[Node] = []
        embedding_literal = self._embedding_literal(vec)
        with self.engine.connect() as conn:
            # Bias planner away from full table scans to favor ANN index paths.
            try:
                conn.execute(text("SET LOCAL enable_seqscan = off"))
            except Exception:
                pass
            self._warn_if_large_seq_scan(conn, embedding_literal, top_k, is_active, is_indexed)
            rows = conn.execute(
                sql,
                {
                    "embedding_literal": embedding_literal,
                    "dim": self.dim,
                    "k": top_k,
                    "is_active": is_active,
                    "is_indexed": is_indexed,
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
                Node(
                    id=str(row[0]),
                    text=str(row[1] or ""),
                    metadata=dict(meta or {}),
                    score=self._normalize_cosine_distance(float(row[3] or 0.0)),
                )
            )
        return out

    def delete(self, doc_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM agri_vector.document_chunks WHERE doc_ref = :doc_ref"), {"doc_ref": str(doc_id)})

    def health(self) -> Dict[str, object]:
        ok = False
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            ok = True
        except Exception:
            ok = False
        return {"ok": ok, "ready": bool(self.ready), "backend": "pgvector", "dim": self.dim}
