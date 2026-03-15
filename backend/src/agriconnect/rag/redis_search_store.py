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
import time
from typing import Any, Dict, List, Tuple
from types import SimpleNamespace

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

    def __init__(
        self,
        url: str,
        dim: int = 384,
        namespace: str = "rag",
        ensure_index: bool = True,
        index_name: str = "rag:idx",
        socket_timeout: int = 20,
        retry_on_timeout: bool = True,
        decode_responses: bool = True,
    ):
        if redis is None:
            raise RuntimeError("redis package is required for RedisSearchVectorStore")
        if np is None:
            raise RuntimeError("numpy is required for RedisSearchVectorStore")
        self.index_name = index_name or self.INDEX_NAME
        self.decode_responses = bool(decode_responses)

        # Use resilient Redis client settings; latin-1 preserves byte round-trips
        # when decode_responses=True for vector payloads.
        try:
            self.client = redis.from_url(
                url,
                decode_responses=self.decode_responses,
                socket_timeout=socket_timeout,
                retry_on_timeout=retry_on_timeout,
                encoding="latin-1",
            )
        except TypeError:
            # Older redis-py may not accept retry options in from_url.
            self.client = redis.from_url(url, decode_responses=self.decode_responses, socket_timeout=socket_timeout)
        self.dim = dim
        self.ns = namespace
        self._prefix = f"{self.ns}:doc:"
        # readiness and dialect flags
        self.ready = False
        self._dialect2 = False
        # Ensure index exists during initialization (blocking by design).
        if ensure_index:
            try:
                self._ensure_index()
            except Exception as e:
                logger.error("Could not ensure RedisSearch index during init: %s", e, exc_info=True)

        # Compatibility shim for llama_index / storage contexts which may
        # check for `is_embedding_query` on the vector store implementation.
        # Expose a method returning True (this store accepts embedding queries).
        try:
            # only set if not already present
            if not hasattr(self, "is_embedding_query"):
                def _is_embedding_query(q=None):
                    return True

                setattr(self, "is_embedding_query", _is_embedding_query)
        except Exception:
            pass

    def _doc_key(self, doc_id: str) -> str:
        return f"{self._prefix}{doc_id}"

    def _ensure_index(self) -> None:
        # Check FT.INFO
        try:
            info = self.client.execute_command("FT.INFO", self.index_name)
            # If index exists, inspect dialect support. If dialect_2 is not enabled,
            # re-create the index with DIALECT 2 so KNN queries are supported.
            try:
                info_str = b" ".join(info) if isinstance(info, (list, tuple)) else str(info)
            except Exception:
                info_str = str(info)
            # detect dialect_2 support
            try:
                has_dialect2 = b"dialect_2" in (info_str if isinstance(info_str, (bytes, bytearray)) else str(info_str).encode("utf-8"))
            except Exception:
                has_dialect2 = False

            if has_dialect2:
                self._dialect2 = True
                self.ready = True
                return
            else:
                # Drop and recreate with DIALECT 2
                try:
                    logger.info("Existing index missing dialect_2; dropping to recreate with DIALECT 2")
                    self.client.execute_command("FT.DROPINDEX", self.index_name)
                except Exception:
                    logger.exception("Failed to drop existing index; will attempt to create with DIALECT 2 anyway")
        except Exception:
            # Index does not exist; proceed to create it
            pass

        # Create index with a small retry/backoff loop to tolerate transient
        # network glitches or Redis cold starts.
        max_attempts = 3
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                # FT.CREATE idx ON HASH PREFIX 1 rag:doc: SCHEMA text TEXT meta TEXT vec VECTOR HNSW 6 TYPE FLOAT32 DIM <dim> DISTANCE_METRIC COSINE
                cmd = [
                    "FT.CREATE",
                    self.index_name,
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
                logger.info("Created RedisSearch index %s (prefix=%s dim=%d)", self.index_name, self._prefix, self.dim)
                self._dialect2 = True
                self.ready = True
                return
            except Exception as e:
                # Some RedisSearch versions/packaging do not accept the DIALECT argument
                # or network timeouts can occur. If this fails, try without DIALECT.
                logger.warning("Failed to create RedisSearch index with DIALECT option (attempt %d/%d): %s", attempt, max_attempts, e)
                try:
                    cmd_no_dialect = [
                        "FT.CREATE",
                        self.index_name,
                        "ON",
                        "HASH",
                        "PREFIX",
                        "1",
                        self._prefix,
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
                    self.client.execute_command(*cmd_no_dialect)
                    logger.info("Created RedisSearch index %s without DIALECT (prefix=%s dim=%d)", self.index_name, self._prefix, self.dim)
                    self._dialect2 = False
                    self.ready = True
                    return
                except Exception as e2:
                    logger.warning("Failed to create RedisSearch index without DIALECT (attempt %d/%d): %s", attempt, max_attempts, e2)
            # backoff before retrying
            if attempt < max_attempts:
                backoff = 0.5 * (2 ** (attempt - 1))
                logger.info("Retrying index creation after %.2fs backoff", backoff)
                time.sleep(backoff)
        # All attempts failed; log and continue. The retriever will fallback to
        # in-memory scan-based search if necessary.
        logger.error("All attempts to create RedisSearch index failed for %s; continuing without index", self.index_name)
        self._dialect2 = False
        self.ready = False

    def add(self, doc_id: str, text: str, meta: Dict[str, Any], embedding: List[float]) -> None:
        arr = np.asarray(embedding, dtype=np.float32)
        b = arr.tobytes()
        key = self._doc_key(doc_id)
        vec_value = b.decode("latin-1") if self.decode_responses else b
        mapping = {
            "text": text,
            "meta": json.dumps(meta, ensure_ascii=False),
            "vec": vec_value,
        }
        p = self.client.pipeline()
        p.hset(key, mapping=mapping)
        p.execute()

    def query(self, query_embedding, k: int = 5):
        # Accept either a raw embedding list/ndarray or an object (e.g. QueryBundle
        # / VectorStoreQuery) from llama_index. Extract embedding from common
        # attribute names when necessary. When called from llama_index, return
        # a VectorStoreQueryResult-like object (with a `.nodes` attribute) rather
        # than a plain list so the retriever code can consume it.
        # Note: We avoid importing heavy llama_index types at module import time
        # and import them lazily here to reduce import overhead.
        def _extract_embedding(obj):
            # direct list/tuple/ndarray
            if isinstance(obj, (list, tuple)):
                return np.asarray(obj, dtype=np.float32)
            try:
                if isinstance(obj, np.ndarray):
                    return obj.astype(np.float32)
            except Exception:
                pass
            # object-like: check common attribute names
            for attr in ("embedding", "query_embedding", "query_vector", "vector", "values"):
                v = getattr(obj, attr, None)
                if v is not None:
                    try:
                        return np.asarray(v, dtype=np.float32)
                    except Exception:
                        continue
            # If object supports tolist or similar
            try:
                v = getattr(obj, "to_list", None) or getattr(obj, "tolist", None)
                if callable(v):
                    return np.asarray(v(), dtype=np.float32)
            except Exception:
                pass
            raise TypeError("Unsupported query_embedding type: %r" % (type(obj),))

        try:
            # If called with a VectorStoreQuery, it may carry top-k and other
            # fields. Try to extract embedding and top-k if provided.
            qobj = query_embedding
            top_k = k
            try:
                # lazy import to access llama_index types
                from llama_index.core.vector_stores.types import VectorStoreQuery
            except Exception:
                VectorStoreQuery = None

            if VectorStoreQuery is not None and isinstance(qobj, VectorStoreQuery):
                top_k = getattr(qobj, "similarity_top_k", k) or k
                emb_arr = _extract_embedding(qobj.query_embedding)
            else:
                emb_arr = _extract_embedding(qobj)
            vec = emb_arr.astype(np.float32).tobytes()
        except Exception as e:
            logger.exception("Unable to extract embedding from query object: %s", e)
            # return empty VectorStoreQueryResult-like object
            return self._make_result([], [], [])
        # Build query: choose preferred KNN syntax based on detected dialect.
        # If dialect_2 detected, use attribute-style. Otherwise try arrow-style first
        # and fallback to attribute-style if it fails.
        if getattr(self, "_dialect2", False):
            knn_query_variants = [f"@vec:[KNN {top_k} $vec]", f"*=>[KNN {top_k} @vec $vec]"]
        else:
            knn_query_variants = [f"*=>[KNN {top_k} @vec $vec]", f"@vec:[KNN {top_k} $vec]"]
        # FT.SEARCH <index> <query> PARAMS 2 vec <blob> RETURN 3 text meta vec LIMIT 0 k
        res = None
        last_err = None
        for knn_query in knn_query_variants:
            try:
                res = self.client.execute_command(
                    "FT.SEARCH",
                    self.index_name,
                    knn_query,
                    "PARAMS",
                    "2",
                    "vec",
                    vec,
                    "RETURN",
                    "2",
                    "text",
                    "meta",
                    "LIMIT",
                    "0",
                    str(top_k),
                    "DIALECT",
                    "2",
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
                # gather vectors with guarded scan to avoid long blocking loops
                ids = []
                vecs = []
                import time

                max_keys = 2000
                scanned = 0
                start = time.time()
                for key in self.client.scan_iter(match=f"{self._prefix}*", count=100):
                    scanned += 1
                    # safety: break if we've scanned too many keys or time exceeded
                    if scanned > max_keys or (time.time() - start) > 3.0:
                        logger.warning("Redis scan_iter aborted early (scanned=%d elapsed=%.2fs)", scanned, time.time() - start)
                        break
                    try:
                        # key may be bytes or str depending on client config
                        if isinstance(key, bytes):
                            docid = key.decode("utf-8").replace(self._prefix, "")
                        else:
                            docid = str(key).replace(self._prefix, "")
                    except Exception:
                        docid = key
                    try:
                        v = self.client.hget(key, b"vec")
                    except Exception as e:
                        logger.debug("hget failed for key %s: %s", key, e)
                        continue
                    if not v:
                        continue
                    if isinstance(v, str):
                        v = v.encode("latin-1", errors="ignore")
                    arr = np.frombuffer(v, dtype=np.float32)
                    if arr.size != self.dim:
                        continue
                    ids.append(docid)
                    vecs.append(arr)
                if not vecs:
                    return self._make_result([], [], [])
                mat = np.stack(vecs, axis=0)
                # cosine similarity
                try:
                    q = emb_arr
                except NameError:
                    q = _extract_embedding(query_embedding)
                q = np.asarray(q, dtype=np.float32)
                qn = q / (np.linalg.norm(q) + 1e-12)
                matn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
                scores = matn.dot(qn)
                top_idx = np.argsort(-scores)[:top_k]
                out = []
                for ix in top_idx:
                    docid = ids[int(ix)]
                    key = f"{self._prefix}{docid}"
                    # fetch fields
                    fields = self.client.hgetall(key)
                    text = fields.get("text") or fields.get(b"text")
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", errors="replace")
                    elif isinstance(text, str):
                        # attempt to recover mojibake where UTF-8 bytes were decoded
                        # as latin-1 by re-encoding then decoding as UTF-8
                        try:
                            text = text.encode("latin-1").decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    meta = fields.get("meta") or fields.get(b"meta")
                    if isinstance(meta, bytes):
                        try:
                            meta = json.loads(meta.decode("utf-8"))
                        except Exception:
                            try:
                                meta = json.loads(meta.decode("latin-1"))
                            except Exception:
                                meta = {}
                    elif isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            try:
                                meta = json.loads(meta.encode("latin-1").decode("utf-8"))
                            except Exception:
                                meta = {}
                    out.append({"id": docid, "score": float(scores[int(ix)]), "text": text, "meta": meta})
                # convert to VectorStoreQueryResult-like object
                try:
                    from llama_index.core.vector_stores.types import VectorStoreQueryResult
                    try:
                        # construct proper TextNode instances so llama_index validators accept them
                        from llama_index.core.schema import TextNode

                        nodes = [
                            TextNode(
                                text=(o.get("text") or ""),
                                metadata=(o.get("meta") or {}),
                                id_=o["id"],
                            )
                            for o in out
                        ]
                    except Exception:
                        # fallback to lightweight fake nodes if TextNode not importable
                        nodes = [
                            SimpleNamespace(node_id=o["id"], get_content=(lambda o=o: o.get("text")), extra_info=o.get("meta"))
                            for o in out
                        ]
                    similarities = [float(o.get("score") or 0.0) for o in out]
                    ids = [o["id"] for o in out]
                    return self._make_result(nodes, similarities, ids)
                except Exception:
                    # return a result-like object even if llama_index types aren't importable
                    nodes = [SimpleNamespace(node_id=o["id"], get_content=(lambda o=o: o.get("text")), extra_info=o.get("meta")) for o in out]
                    similarities = [float(o.get("score") or 0.0) for o in out]
                    ids = [o["id"] for o in out]
                    return self._make_result(nodes, similarities, ids)
            except Exception:
                logger.exception("Local fallback search failed")
                return []

        # Parse results: res = [total, id1, [field,val,...], id2, [field,val,...], ...]
        if not res or len(res) < 2:
            return self._make_result([], [], [])
        total = res[0]
        out = []
        i = 1
        while i < len(res):
            docid = res[i].decode("utf-8") if isinstance(res[i], bytes) else str(res[i])
            fields = res[i + 1]
            d = {fields[j].decode("utf-8") if isinstance(fields[j], bytes) else fields[j]: fields[j + 1] for j in range(0, len(fields), 2)}
            text = d.get("text")
            if isinstance(text, bytes):
                text = text.decode("utf-8", errors="replace")
            elif isinstance(text, str):
                try:
                    text = text.encode("latin-1").decode("utf-8", errors="replace")
                except Exception:
                    pass
            meta = d.get("meta")
            if isinstance(meta, bytes):
                try:
                    meta = json.loads(meta.decode("utf-8"))
                except Exception:
                    try:
                        meta = json.loads(meta.decode("latin-1"))
                    except Exception:
                        meta = {}
            elif isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    try:
                        meta = json.loads(meta.encode("latin-1").decode("utf-8"))
                    except Exception:
                        meta = {}
            out.append({"id": docid, "score": None, "text": text, "meta": meta})
            i += 2
        # convert to VectorStoreQueryResult-like object for llama_index
        try:
            # prefer creating proper TextNode objects for llama_index
            from llama_index.core.schema import TextNode

            nodes = [
                TextNode(text=(o.get("text") or ""), metadata=(o.get("meta") or {}), id_=o["id"])
                for o in out
            ]
            similarities = [float(o.get("score") or 0.0) for o in out]
            ids = [o["id"] for o in out]
            return self._make_result(nodes, similarities, ids)
        except Exception:
            nodes = [
                SimpleNamespace(node_id=o["id"], get_content=(lambda o=o: o.get("text")), extra_info=o.get("meta"))
                for o in out
            ]
            similarities = [float(o.get("score") or 0.0) for o in out]
            ids = [o["id"] for o in out]
            return self._make_result(nodes, similarities, ids)

    def _make_result(self, nodes, similarities, ids):
        """Return a VectorStoreQueryResult if available, else a simple result-like object."""
        try:
            from llama_index.core.vector_stores.types import VectorStoreQueryResult

            return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
        except Exception:
            return SimpleNamespace(nodes=nodes, similarities=similarities, ids=ids)

    def delete(self, doc_id: str) -> None:
        key = self._doc_key(doc_id)
        self.client.delete(key)
