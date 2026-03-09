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
    def __init__(self, url: str, dim: int = 384, namespace: str = "rag"):
        if redis is None:
            raise RuntimeError("redis package is required for RedisVectorStore")
        if np is None:
            raise RuntimeError("numpy is required for RedisVectorStore")
        self.client = redis.from_url(url, decode_responses=False)
        self.dim = dim
        self.ns = namespace
        self._docs_key = f"{self.ns}:docs"

    def _doc_key(self, doc_id: str) -> str:
        return f"{self.ns}:doc:{doc_id}"

    def add(self, doc_id: str, text: str, meta: Dict[str, Any], embedding: List[float]) -> None:
        """Add or update a single document chunk."""
        arr = np.asarray(embedding, dtype=np.float32)
        b = arr.tobytes()
        key = self._doc_key(doc_id)
        mapping = {
            b"text": text.encode("utf-8"),
            b"meta": json.dumps(meta).encode("utf-8"),
            b"vec": b,
        }
        # Use pipeline for efficiency
        p = self.client.pipeline()
        p.hset(key, mapping=mapping)
        p.sadd(self._docs_key, doc_id)
        p.execute()

    def _fetch_all_vectors(self) -> List[Tuple[str, np.ndarray, str, Dict[str, Any]]]:
        """Return list of (doc_id, vec_array, text, meta) for all docs."""
        ids = [i.decode("utf-8") if isinstance(i, bytes) else i for i in self.client.smembers(self._docs_key)]
        if not ids:
            return []
        p = self.client.pipeline()
        for doc_id in ids:
            p.hget(self._doc_key(doc_id), b"vec")
            p.hget(self._doc_key(doc_id), b"text")
            p.hget(self._doc_key(doc_id), b"meta")
        res = p.execute()
        out = []
        for i in range(0, len(res), 3):
            vec_b = res[i]
            text_b = res[i + 1]
            meta_b = res[i + 2]
            try:
                vec = np.frombuffer(vec_b, dtype=np.float32) if vec_b else np.zeros(self.dim, dtype=np.float32)
            except Exception:
                vec = np.zeros(self.dim, dtype=np.float32)
            text = text_b.decode("utf-8") if text_b else ""
            meta = json.loads(meta_b.decode("utf-8")) if meta_b else {}
            out.append((ids[i // 3], vec, text, meta))
        return out

    def query(self, query_embedding: List[float], k: int = 5) -> List[Dict[str, Any]]:
        """Return top-k nearest neighbours as list of dicts: {id, score, text, meta}."""
        q = np.asarray(query_embedding, dtype=np.float32)
        rows = self._fetch_all_vectors()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        vecs = np.stack([r[1] for r in rows], axis=0)
        # cosine similarity
        q_norm = q / (np.linalg.norm(q) + 1e-10)
        vecs_norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-10)
        sims = vecs_norm.dot(q_norm)
        idx = sims.argsort()[::-1][:k]
        results = []
        for i in idx:
            results.append({
                "id": ids[i],
                "score": float(sims[i]),
                "text": rows[i][2],
                "meta": rows[i][3],
            })
        return results

    def delete(self, doc_id: str) -> None:
        key = self._doc_key(doc_id)
        p = self.client.pipeline()
        p.delete(key)
        p.srem(self._docs_key, doc_id)
        p.execute()
