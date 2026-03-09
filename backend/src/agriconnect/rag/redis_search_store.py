"""RedisSearch-backed Vector Store using HNSW vectors.

Requires Redis Stack (RedisSearch/Vector) or Redis Enterprise with vector support.

This store creates an index `rag:idx` on HASH keys prefixed by `rag:doc:` and
stores vectors in the `vec` field as FLOAT32 binary. It supports `add` and `query`.

Note: This implementation uses the low-level `execute_command` calls to work
across redis-py versions; adjust if a higher-level search client is available.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

try:
    import numpy as np
except Exception:
    np = None

try:
    import redis
except Exception:
    redis = None

logger = logging.getLogger("agriconnect.rag.redis_search_store")


class RedisSearchVectorStore:
    INDEX_NAME = "rag:idx"
    PREFIX = "rag:doc:"

    def __init__(self, url: str, dim: int = 384, namespace: str = "rag"):
        if redis is None:
            raise RuntimeError("redis package is required for RedisSearchVectorStore")
        if np is None:
            raise RuntimeError("numpy is required for RedisSearchVectorStore")
        self.client = redis.from_url(url, decode_responses=False)
        self.dim = dim
        self.ns = namespace
        self._prefix = f"{self.ns}:doc:"
        # Ensure index exists (best-effort)
        try:
            self._ensure_index()
        except Exception as e:
            logger.warning("Could not ensure RedisSearch index: %s", e)

    def _doc_key(self, doc_id: str) -> str:
        return f"{self._prefix}{doc_id}"

    def _ensure_index(self) -> None:
        # Check FT.INFO
        try:
            info = self.client.execute_command("FT.INFO", self.INDEX_NAME)
            # If index exists, inspect dialect support. If dialect_2 is not enabled,
            # re-create the index with DIALECT 2 so KNN queries are supported.
            try:
                info_str = b" ".join(info) if isinstance(info, (list, tuple)) else str(info)
            except Exception:
                info_str = str(info)
            if b"dialect_2" not in (info_str if isinstance(info_str, (bytes, bytearray)) else str(info_str).encode("utf-8")):
                # Drop and recreate with DIALECT 2
                try:
                    logger.info("Existing index missing dialect_2; dropping to recreate with DIALECT 2")
                    self.client.execute_command("FT.DROPINDEX", self.INDEX_NAME)
                except Exception:
                    logger.exception("Failed to drop existing index; will attempt to create with DIALECT 2 anyway")
            else:
                return
        except Exception:
            # Index does not exist; proceed to create it
            pass

        # Create index
        try:
            # FT.CREATE idx ON HASH PREFIX 1 rag:doc: SCHEMA text TEXT meta TEXT vec VECTOR HNSW 6 TYPE FLOAT32 DIM <dim> DISTANCE_METRIC COSINE
            cmd = [
                "FT.CREATE",
                self.INDEX_NAME,
                "ON",
                "HASH",
                "PREFIX",
                "1",
                self._prefix,
                "DIALECT",
                "2",
                "SCHEMA",
                "text",
                "TEXT",
                "meta",
                "TEXT",
                "vec",
                "VECTOR",
                "HNSW",
                "6",
                "TYPE",
                "FLOAT32",
                "DIM",
                str(self.dim),
                "DISTANCE_METRIC",
                "COSINE",
            ]
            # execute_command expects each arg separately
            self.client.execute_command(*cmd)
            logger.info("Created RedisSearch index %s (prefix=%s dim=%d)", self.INDEX_NAME, self._prefix, self.dim)
        except Exception as e:
            logger.warning("Failed to create RedisSearch index: %s", e)

    def add(self, doc_id: str, text: str, meta: Dict[str, Any], embedding: List[float]) -> None:
        arr = np.asarray(embedding, dtype=np.float32)
        b = arr.tobytes()
        key = self._doc_key(doc_id)
        mapping = {
            b"text": text.encode("utf-8"),
            b"meta": json.dumps(meta).encode("utf-8"),
            b"vec": b,
        }
        p = self.client.pipeline()
        p.hset(key, mapping=mapping)
        p.execute()

    def query(self, query_embedding: List[float], k: int = 5) -> List[Dict[str, Any]]:
        # Prepare binary vector param
        vec = np.asarray(query_embedding, dtype=np.float32).tobytes()
        # Build query: KNN. Different RedisSearch versions accept slightly different
        # syntaxes. Try the attribute-style first, then fallback to arrow-style.
        knn_query_variants = [
            f"@vec:[KNN {k} $vec]",
            f"*=>[KNN {k} @vec $vec]",
        ]
        # FT.SEARCH <index> <query> PARAMS 2 vec <blob> RETURN 3 text meta vec LIMIT 0 k
        res = None
        last_err = None
        for knn_query in knn_query_variants:
            try:
                res = self.client.execute_command(
                    "FT.SEARCH",
                    self.INDEX_NAME,
                    knn_query,
                    "PARAMS",
                    "2",
                    "vec",
                    vec,
                    "RETURN",
                    "3",
                    "text",
                    "meta",
                    "vec",
                    "LIMIT",
                    "0",
                    str(k),
                )
                break
            except Exception as e:
                last_err = e
                logger.debug("KNN variant failed: %s -> %s", knn_query, e)

        if res is None:
            logger.exception("RedisSearch query failed: %s", last_err)
            # Fallback: server doesn't accept KNN syntax — perform local in-memory nearest
            # neighbor search by scanning stored vectors and computing cosine similarity.
            try:
                logger.info("Falling back to local cosine search over stored vectors")
                # gather all vectors
                ids = []
                vecs = []
                for key in self.client.scan_iter(match=f"{self._prefix}*"):
                    try:
                        docid = key.decode("utf-8").replace(self._prefix, "")
                    except Exception:
                        docid = key
                    v = self.client.hget(key, b"vec")
                    if not v:
                        continue
                    arr = np.frombuffer(v, dtype=np.float32)
                    if arr.size != self.dim:
                        continue
                    ids.append(docid)
                    vecs.append(arr)
                if not vecs:
                    return []
                mat = np.stack(vecs, axis=0)
                # cosine similarity
                q = np.asarray(query_embedding, dtype=np.float32)
                qn = q / (np.linalg.norm(q) + 1e-12)
                matn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
                scores = matn.dot(qn)
                top_idx = np.argsort(-scores)[:k]
                out = []
                for ix in top_idx:
                    docid = ids[int(ix)]
                    key = f"{self._prefix}{docid}"
                    # fetch fields
                    fields = self.client.hgetall(key)
                    text = fields.get(b"text")
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", errors="ignore")
                    meta = fields.get(b"meta")
                    if isinstance(meta, bytes):
                        try:
                            meta = json.loads(meta.decode("utf-8"))
                        except Exception:
                            meta = {}
                    out.append({"id": docid, "score": float(scores[int(ix)]), "text": text, "meta": meta})
                return out
            except Exception:
                logger.exception("Local fallback search failed")
                return []

        # Parse results: res = [total, id1, [field,val,...], id2, [field,val,...], ...]
        if not res or len(res) < 2:
            return []
        total = res[0]
        out = []
        i = 1
        while i < len(res):
            docid = res[i].decode("utf-8") if isinstance(res[i], bytes) else str(res[i])
            fields = res[i + 1]
            d = {fields[j].decode("utf-8") if isinstance(fields[j], bytes) else fields[j]: fields[j + 1] for j in range(0, len(fields), 2)}
            text = d.get("text")
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            meta = d.get("meta")
            if isinstance(meta, bytes):
                try:
                    meta = json.loads(meta.decode("utf-8"))
                except Exception:
                    meta = {}
            out.append({"id": docid, "score": None, "text": text, "meta": meta})
            i += 2
        return out

    def delete(self, doc_id: str) -> None:
        key = self._doc_key(doc_id)
        self.client.delete(key)
