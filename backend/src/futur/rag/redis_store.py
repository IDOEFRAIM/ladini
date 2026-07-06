"""Simple Redis-backed vector store for RAG (brute-force search).

This is a pragmatic, small-capacity implementation that stores chunk
texts and embeddings in Redis hashes and performs nearest-neighbour search
by fetching vectors and computing cosine similarity locally. It's NOT
intended for very large corpora but is useful for prototyping and small
deployments where Redis is already available.

Schema (per chunk):
  HSET doc:{id} text "..." meta "{...}" vec BINARY
  SADD docs {id}

Fields:
  - text: chunk text
  - meta: JSON metadata
  - vec: float32 bytes (numpy)

Usage:
  store = RedisVectorStore(redis_url, dim=384)
  store.add(id, text, meta, embedding)
  results = store.query(query_embedding, k=5)

For production use with large corpora prefer RedisSearch/Vector index (HNSW)
or an external vector DB (FAISS, Milvus, Pinecone, etc.).
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple, Optional

try:
    import numpy as np
except Exception:  # pragma: no cover - deps
    np = None

try:
    import redis
except Exception:
    redis = None

logger = logging.getLogger("agriconnect.rag.redis_store")


class RedisVectorStore:
    def __init__(
        self,
        url: str,
        dim: int = 384,
        namespace: str = "rag",
        socket_timeout: int = 20,
        retry_on_timeout: bool = True,
        decode_responses: bool = True,
    ):
        if redis is None:
            raise RuntimeError("redis package is required for RedisVectorStore")
        if np is None:
            raise RuntimeError("numpy is required for RedisVectorStore")
        self.decode_responses = bool(decode_responses)
        try:
            self.client = redis.from_url(
                url,
                decode_responses=self.decode_responses,
                socket_timeout=socket_timeout,
                retry_on_timeout=retry_on_timeout,
                encoding="latin-1",
            )
        except TypeError:
            self.client = redis.from_url(url, decode_responses=self.decode_responses, socket_timeout=socket_timeout)
        self.dim = dim
        self.ns = namespace
        # Use a shared hash tag so all keys map to one cluster slot.
        # This avoids CROSSSLOT failures for pipeline/multi-key operations.
        self._slot_tag = "{docs}"
        self._docs_key = f"{self.ns}:{self._slot_tag}:ids"

        # Compatibility shim: signal that this store can be queried by embeddings
        try:
            if not hasattr(self, "is_embedding_query"):
                def _is_embedding_query(q=None):
                    return True
                setattr(self, "is_embedding_query", _is_embedding_query)
        except Exception:
            pass

    def _doc_key(self, doc_id: str) -> str:
        return f"{self.ns}:{self._slot_tag}:doc:{doc_id}"

    def add(self, doc_id: str, text: str, meta: Dict[str, Any], embedding: List[float]) -> None:
        """Add or update a single document chunk."""
        arr = np.asarray(embedding, dtype=np.float32)
        b = arr.tobytes()
        key = self._doc_key(doc_id)
        mapping = {
            "text": text.encode("utf-8", errors="ignore"),
            "meta": json.dumps(meta, ensure_ascii=False).encode("utf-8", errors="ignore"),
            "vec": b,
        }
        # Use pipeline for efficiency
        p = self.client.pipeline()
        p.hset(key, mapping=mapping)
        p.sadd(self._docs_key, doc_id)
        p.execute()

    def add_many(self, items: List[Dict[str, Any]], batch_size: int = 50) -> None:
        """Batch insert documents with a single pipeline execution per batch."""
        if not items:
            return
        size = max(1, int(batch_size or 50))
        for start in range(0, len(items), size):
            batch = items[start : start + size]
            p = self.client.pipeline()
            for item in batch:
                doc_id = str(item.get("id") or "")
                if not doc_id:
                    continue
                arr = np.asarray(item.get("embedding") or [], dtype=np.float32)
                key = self._doc_key(doc_id)
                p.hset(
                    key,
                    mapping={
                        "text": str(item.get("text") or "").encode("utf-8", errors="ignore"),
                        "meta": json.dumps(item.get("meta") or {}, ensure_ascii=False).encode("utf-8", errors="ignore"),
                        "vec": arr.tobytes(),
                    },
                )
                p.sadd(self._docs_key, doc_id)
            p.execute()

    def _fetch_all_vectors(self) -> List[Tuple[str, np.ndarray, str, Dict[str, Any]]]:
        """Return list of (doc_id, vec_array, text, meta) for all docs."""
        ids = [i.decode("utf-8") if isinstance(i, bytes) else i for i in self.client.smembers(self._docs_key)]
        if not ids:
            return []
        p = self.client.pipeline()
        for doc_id in ids:
            p.hget(self._doc_key(doc_id), "vec")
            p.hget(self._doc_key(doc_id), "text")
            p.hget(self._doc_key(doc_id), "meta")
        res = p.execute()
        out = []
        for i in range(0, len(res), 3):
            vec_b = res[i]
            text_b = res[i + 1]
            meta_b = res[i + 2]
            if isinstance(vec_b, str):
                vec_b = vec_b.encode("latin-1", errors="ignore")
            try:
                vec = np.frombuffer(vec_b, dtype=np.float32) if vec_b else np.zeros(self.dim, dtype=np.float32)
            except Exception:
                vec = np.zeros(self.dim, dtype=np.float32)
            if isinstance(text_b, bytes):
                text = text_b.decode("utf-8", errors="ignore")
            else:
                text = text_b or ""
            if isinstance(meta_b, bytes):
                meta = json.loads(meta_b.decode("utf-8")) if meta_b else {}
            else:
                try:
                    meta = json.loads(meta_b) if meta_b else {}
                except Exception:
                    meta = {}
            out.append((ids[i // 3], vec, text, meta))
        return out

    def _extract_embedding(self, obj):
        # Accept raw list/ndarray or object (e.g. VectorStoreQuery) with common attr names
        if isinstance(obj, (list, tuple)):
            return np.asarray(obj, dtype=np.float32)
        try:
            if isinstance(obj, np.ndarray):
                return obj.astype(np.float32)
        except Exception:
            pass
        for attr in ("embedding", "query_embedding", "query_vector", "vector", "values"):
            v = getattr(obj, attr, None)
            if v is not None:
                try:
                    return np.asarray(v, dtype=np.float32)
                except Exception:
                    continue
        try:
            tolist = getattr(obj, "tolist", None) or getattr(obj, "to_list", None)
            if callable(tolist):
                return np.asarray(tolist(), dtype=np.float32)
        except Exception:
            pass
        raise TypeError("Unsupported query_embedding type: %r" % (type(obj),))

    def _make_result(self, nodes, similarities, ids):
        try:
            from llama_index.core.vector_stores.types import VectorStoreQueryResult

            return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
        except Exception:
            return SimpleNamespace(nodes=nodes, similarities=similarities, ids=ids)

    def query(self, query_embedding, k: int = 5):
        """Return top-k nearest neighbours as a VectorStoreQueryResult-like object."""
        q = None
        try:
            q = self._extract_embedding(query_embedding)
        except Exception:
            # Last resort: try to coerce directly
            q = np.asarray(query_embedding, dtype=np.float32)

        try:
            rows = self._fetch_all_vectors()
        except Exception as e:
            # If Redis is unavailable or connection reset occurs, log and return empty result
            logger.warning("RedisVectorStore: failed to fetch vectors from Redis: %s", e)
            return self._make_result([], [], [])
        if not rows:
            return self._make_result([], [], [])
        ids = [r[0] for r in rows]
        vecs = np.stack([r[1] for r in rows], axis=0)
        # cosine similarity
        q_norm = q / (np.linalg.norm(q) + 1e-10)
        vecs_norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-10)
        sims = vecs_norm.dot(q_norm)
        idx = sims.argsort()[::-1][:k]
        out = []
        for i in idx:
            out.append({
                "id": ids[i],
                "score": float(sims[i]),
                "text": rows[i][2],
                "meta": rows[i][3],
            })

        # Build node objects compatible with llama_index where possible
        try:
            from llama_index.core.schema import TextNode

            nodes = [TextNode(text=(o.get("text") or ""), metadata=(o.get("meta") or {}), id_=o["id"]) for o in out]
        except Exception:
            nodes = [SimpleNamespace(node_id=o["id"], get_content=(lambda o=o: o.get("text")), extra_info=o.get("meta")) for o in out]

        similarities = [float(o.get("score") or 0.0) for o in out]
        ids = [o["id"] for o in out]
        return self._make_result(nodes, similarities, ids)

    def delete(self, doc_id: str) -> None:
        key = self._doc_key(doc_id)
        p = self.client.pipeline()
        p.delete(key)
        p.srem(self._docs_key, doc_id)
        p.execute()
