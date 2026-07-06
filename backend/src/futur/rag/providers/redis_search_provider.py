from __future__ import annotations

import json
import logging
import os
import importlib
import re
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import numpy as np
import redis
from redis.exceptions import ResponseError
from redis.commands.search.field import TextField, VectorField
try:
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType
except Exception:
    _idx_mod = importlib.import_module("redis.commands.search.index_definition")
    IndexDefinition = getattr(_idx_mod, "IndexDefinition")
    IndexType = getattr(_idx_mod, "IndexType")
from redis.commands.search.query import Query

from futur.rag.core.models import Node, QueryBundle
from futur.rag.interfaces import BaseVectorStore
from futur.rag.errors import DimensionMismatchError

logger = logging.getLogger("agriconnect.rag.providers.redis_search")


class RedisSearchProvider(BaseVectorStore):
    INDEX_NAME = "rag:index"
    PREFIX = "rag:{docs}:doc:"

    def __init__(
        self,
        url: str,
        dim: int = 384,
        namespace: str = "rag",
        ensure_index: bool = True,
        index_name: str | None = None,
        allow_degraded_mode: bool | None = None,
        socket_timeout: int = 5,
        socket_connect_timeout: int = 5,
        health_check_interval: int = 30,
        retry_on_timeout: bool = True,
        decode_responses: bool = False,
    ):
        self.index_name = (
            index_name
            or (os.getenv("AGRICONNECT_REDIS_INDEX_NAME", self.INDEX_NAME) or self.INDEX_NAME).strip()
        )
        self.dim = int(dim)
        self.ns = namespace
        prefix_override = (os.getenv("AGRICONNECT_REDIS_DOC_PREFIX", "") or "").strip()
        self._prefix = (
            prefix_override
            if prefix_override
            else (self.PREFIX if self.ns == "rag" else f"{self.ns}:{{docs}}:doc:")
        )
        self._docs_key = f"{self.ns}:{{docs}}:ids"
        self.ready = False
        self.available = True
        self.degraded_mode = False
        self.allow_degraded_mode = (
            bool(allow_degraded_mode)
            if allow_degraded_mode is not None
            else (os.getenv("AGRICONNECT_RAG_ALLOW_DEGRADED", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        )
        redis_url = self._normalize_redis_url(url)
        # Mirror ingestion connection behavior from RedisVectorStore to avoid protocol drift.
        try:
            self.client = redis.from_url(
                redis_url,
                decode_responses=decode_responses,
                socket_timeout=socket_timeout,
                retry_on_timeout=retry_on_timeout,
                encoding="latin-1",
            )
        except TypeError:
            self.client = redis.from_url(
                redis_url,
                decode_responses=decode_responses,
                socket_timeout=socket_timeout,
                retry_on_timeout=retry_on_timeout,
            )
        self.client.ping()
        self.available = self._detect_redisearch_available()
        if not self.available:
            self.ready = False
            self.degraded_mode = bool(self.allow_degraded_mode)
            return
        if ensure_index:
            self.ensure_index()

    @staticmethod
    def _normalize_redis_url(url: str) -> str:
        parsed = urlparse((url or "").strip())
        if parsed.scheme not in {"redis", "rediss"}:
            raise ValueError(f"Unsupported Redis URL scheme: {parsed.scheme!r}")
        return urlunparse(parsed)

    def _detect_redisearch_available(self) -> bool:
        try:
            self.client.execute_command("FT.INFO", self.index_name)
            return True
        except ResponseError as exc:
            msg = str(exc or "").lower()
            if "unknown command" in msg and "ft." in msg:
                logger.warning("RediSearch module not available on Redis endpoint; provider marked unavailable")
                return False
            # If the module exists but index is missing, keep provider available.
            return True
        except Exception:
            # Connection-level issues are handled by caller; keep provider available by default.
            return True

    def _doc_key(self, doc_id: str) -> str:
        return f"{self._prefix}{doc_id}"

    @staticmethod
    def _parse_metadata(value: Any) -> Dict[str, Any]:
        try:
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", errors="ignore")
            if isinstance(value, str):
                return json.loads(value) if value else {}
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        return {}

    def ensure_index(self) -> None:
        if not self.available:
            self.ready = False
            self.degraded_mode = bool(self.allow_degraded_mode)
            return
        try:
            self.client.execute_command("FT.INFO", self.index_name)
            self.ready = True
            self.degraded_mode = False
            return
        except Exception:
            pass

        try:
            schema = (
                TextField("title", weight=2.0),
                TextField("content", weight=1.0),
                TextField("metadata"),
                VectorField(
                    "vec",
                    "HNSW",
                    {
                        "TYPE": "FLOAT32",
                        "DIM": self.dim,
                        "DISTANCE_METRIC": "COSINE",
                    },
                ),
            )
            definition = IndexDefinition(prefix=[self._prefix], index_type=IndexType.HASH)
            self.client.ft(self.index_name).create_index(fields=schema, definition=definition)
            self.ready = True
            self.degraded_mode = False
        except Exception as exc:
            if "Index already exists" in str(exc):
                self.ready = True
                self.degraded_mode = False
                return
            if self.allow_degraded_mode:
                self.ready = False
                self.degraded_mode = True
                return
            raise

    def add(self, doc_id: str, text: str, meta: Dict, embedding: List[float]) -> None:
        arr = np.asarray(embedding or [], dtype=np.float32)
        if arr.shape[0] != self.dim:
            return
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        key = self._doc_key(str(doc_id))
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        p = self.client.pipeline()
        p.hset(
            key,
            mapping={
                "title": str((meta or {}).get("title") or "").encode("utf-8", errors="ignore"),
                "content": str(text or "").encode("utf-8", errors="ignore"),
                "metadata": meta_json.encode("utf-8", errors="ignore"),
                "vec": arr.tobytes(),
                "text": str(text or "").encode("utf-8", errors="ignore"),
                "meta": meta_json.encode("utf-8", errors="ignore"),
            },
        )
        p.sadd(self._docs_key, str(doc_id))
        p.execute()

    def add_many(self, items: List[Dict], batch_size: int = 50) -> None:
        if not items:
            return
        size = max(1, int(batch_size or 50))
        for start in range(0, len(items), size):
            batch = items[start : start + size]
            for item in batch:
                self.add(
                    doc_id=str(item.get("id") or ""),
                    text=str(item.get("text") or ""),
                    meta=dict(item.get("meta") or {}),
                    embedding=list(item.get("embedding") or []),
                )

    @staticmethod
    def _build_lexical_query(text_query: str) -> str:
        terms = [t for t in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9\-]{2,}", str(text_query or "")) if t]
        if not terms:
            return "*"
        escaped = [t.replace("-", "\\-") for t in terms[:8]]
        return "|".join(escaped)

    def lexical_search(self, text_query: str, top_k: int = 3) -> List[Node]:
        if not self.available:
            return []
        if not self.ready and not self.degraded_mode:
            return []
        q_text = self._build_lexical_query(text_query)
        query = Query(q_text).with_scores().return_fields("content", "metadata").paging(0, int(max(1, top_k)))
        if self.ready:
            query = query.dialect(2)
        try:
            res = self.client.ft(self.index_name).search(query)
        except Exception:
            return []

        out: List[Node] = []
        for doc in getattr(res, "docs", []):
            meta = self._parse_metadata(getattr(doc, "metadata", "{}"))
            content_raw = getattr(doc, "content", "")
            text_value = content_raw.decode("utf-8", errors="ignore") if isinstance(content_raw, (bytes, bytearray)) else str(content_raw or "")
            score_raw = getattr(doc, "score", 0.0)
            try:
                score = float(score_raw)
            except Exception:
                score = 0.0
            out.append(
                Node(
                    id=str(getattr(doc, "id", "") or ""),
                    text=text_value,
                    metadata=meta,
                    score=score,
                )
            )
        out.sort(key=lambda n: float(n.score or 0.0), reverse=True)
        return out

    def _search_degraded(self, query_bundle: QueryBundle) -> List[Node]:
        q = np.asarray(query_bundle.vector or [], dtype=np.float32)
        if q.ndim != 1 or int(q.shape[0]) != int(self.dim):
            raise DimensionMismatchError(int(self.dim), int(q.shape[0] if q.ndim == 1 else 0))
        ids = [x.decode("utf-8", errors="ignore") if isinstance(x, (bytes, bytearray)) else str(x) for x in self.client.smembers(self._docs_key)]
        if not ids:
            return []

        p = self.client.pipeline()
        for doc_id in ids:
            key = self._doc_key(doc_id)
            p.hget(key, "vec")
            p.hget(key, "content")
            p.hget(key, "metadata")
            p.hget(key, "text")
            p.hget(key, "meta")
        rows = p.execute()

        out: List[Node] = []
        q_norm = q / (np.linalg.norm(q) + 1e-10)
        for i in range(0, len(rows), 5):
            vec_b = rows[i]
            if isinstance(vec_b, str):
                vec_b = vec_b.encode("latin-1", errors="ignore")
            vec = np.frombuffer(vec_b or b"", dtype=np.float32)
            if vec.shape[0] != self.dim:
                continue
            content_raw = rows[i + 1]
            metadata_raw = rows[i + 2]
            text_raw = rows[i + 3]
            meta_raw = rows[i + 4]
            meta = self._parse_metadata(metadata_raw)
            if not meta:
                meta = self._parse_metadata(meta_raw)
            if "is_indexed" in (meta or {}) and not bool((meta or {}).get("is_indexed", False)):
                continue
            v_norm = vec / (np.linalg.norm(vec) + 1e-10)
            sim = float(v_norm.dot(q_norm))
            raw_text = content_raw if content_raw else text_raw
            text_value = raw_text.decode("utf-8", errors="ignore") if isinstance(raw_text, (bytes, bytearray)) else str(raw_text or "")
            meta = dict(meta or {})
            meta["cosine"] = float(sim)
            out.append(Node(id=ids[i // 5], text=text_value, metadata=meta, score=sim))

        out.sort(key=lambda n: float(n.score or 0.0), reverse=True)
        return out[: max(1, int(query_bundle.top_k or 5))]

    def query(self, query_bundle: QueryBundle) -> List[Node]:
        if not self.available and not self.degraded_mode:
            return []
        q_vec = np.asarray(query_bundle.vector or [], dtype=np.float32)
        if q_vec.ndim != 1 or int(q_vec.shape[0]) != int(self.dim):
            raise DimensionMismatchError(int(self.dim), int(q_vec.shape[0] if q_vec.ndim == 1 else 0))
        if self.degraded_mode:
            return self._search_degraded(query_bundle)

        top_k = max(1, int(query_bundle.top_k or 5))
        vector_bytes = q_vec.tobytes()
        q = (
            Query(f"*=>[KNN {int(top_k)} @vec $vec_param AS score]")
            .sort_by("score")
            .return_fields("content", "metadata", "score")
            .paging(0, int(top_k))
            .dialect(2)
        )
        res = self.client.ft(self.index_name).search(q, query_params={"vec_param": vector_bytes})
        out: List[Node] = []
        for doc in getattr(res, "docs", []):
            meta = self._parse_metadata(getattr(doc, "metadata", "{}"))
            if not bool((meta or {}).get("is_indexed", False)):
                continue
            content_raw = getattr(doc, "content", "")
            text_value = content_raw.decode("utf-8", errors="ignore") if isinstance(content_raw, (bytes, bytearray)) else str(content_raw or "")
            score_raw = getattr(doc, "score", 0.0)
            try:
                score = float(score_raw)
            except Exception:
                score = 0.0
            # Try to compute cosine from stored vec for a consistent score
            try:
                key = self._doc_key(str(getattr(doc, "id", "") or ""))
                vec_b = self.client.hget(key, "vec")
                if isinstance(vec_b, str):
                    vec_b = vec_b.encode("latin-1", errors="ignore")
                vec = np.frombuffer(vec_b or b"", dtype=np.float32)
                if vec.shape[0] == int(self.dim):
                    qn = q_vec / (np.linalg.norm(q_vec) + 1e-10)
                    vn = vec / (np.linalg.norm(vec) + 1e-10)
                    cosine = float(vn.dot(qn))
                    meta = dict(meta or {})
                    meta["cosine"] = cosine
                    node_score = cosine
                else:
                    node_score = score
            except Exception:
                node_score = score
            out.append(Node(id=str(getattr(doc, "id", "") or ""), text=text_value, metadata=meta, score=node_score))
        return out

    def delete(self, doc_id: str) -> None:
        p = self.client.pipeline()
        p.delete(self._doc_key(str(doc_id)))
        p.srem(self._docs_key, str(doc_id))
        p.execute()

    def health(self) -> Dict[str, object]:
        ok = False
        try:
            self.client.ping()
            ok = True
        except Exception:
            ok = False
        return {
            "ok": ok,
            "ready": bool(self.ready),
            "available": bool(self.available),
            "backend": "redis_search",
            "degraded_mode": bool(self.degraded_mode),
            "dim": self.dim,
        }
