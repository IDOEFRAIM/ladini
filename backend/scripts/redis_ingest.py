"""Ingest documents and upsert into a Redis-backed vector store.

Features:
 - --ingest-all: collect textual files from backend/sources, backend/docs, src/agriconnect
 - --batch-size: number of pipeline ops per execute
 - --limit: stop after ingesting this many chunks (0 = no limit)
 - --pause: seconds to sleep between pipeline executes
 - connect via --redis-url or host/port/username/password args

This script writes to Redis (preferred) or falls back to writing JSON batches
when Redis is unavailable or an OutOfMemory/limit error occurs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List
from urllib.parse import quote_plus

try:
    import numpy as np
except Exception:
    np = None

try:
    from agriconnect.rag.redis_search_store import RedisSearchVectorStore
except Exception:
    RedisSearchVectorStore = None

try:
    from agriconnect.rag.redis_store import RedisVectorStore
except Exception:
    RedisVectorStore = None

from agriconnect.core.settings import settings

logger = logging.getLogger("redis_ingest")


def chunk_text(text: str, max_chars: int = 500, overlap: int = 50) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    L = len(text)
    while start < L:
        end = min(start + max_chars, L)
        chunks.append(text[start:end])
        if end == L:
            break
        start = max(0, end - overlap)
    return chunks


def embed_texts(texts: List[str], dim: int = 384) -> List[List[float]]:
    if np is None:
        raise RuntimeError("numpy is required for embeddings; install numpy or provide embeddings in input JSON")
    out = []
    for t in texts:
        rng = np.random.RandomState(abs(hash(t)) % (2 ** 31))
        v = rng.rand(dim).astype(np.float32)
        out.append(v.tolist())
    return out


def get_redis_url() -> str:
    return os.getenv("REDIS_URL") or getattr(settings, "REDIS_URL", "")


def choose_store(url: str, dim: int, prefer: str = "auto"):
    import ssl as _ssl

    def try_search(u: str):
        s = RedisSearchVectorStore(u, dim=dim)
        if hasattr(s, "client"):
            s.client.ping()
        return s

    def try_simple(u: str):
        s = RedisVectorStore(u, dim=dim)
        if hasattr(s, "client"):
            s.client.ping()
        return s

    if prefer == "search":
        return try_search(url)
    if prefer == "simple":
        return try_simple(url)

    # auto
    if RedisSearchVectorStore is not None:
        try:
            return try_search(url)
        except Exception as e:
            msg = str(e)
            logger.warning("RedisSearch init error: %s", msg)
            if ("WRONG_VERSION_NUMBER" in msg) or (isinstance(e, _ssl.SSLError)) or ("ssl" in msg.lower()):
                if url.startswith("rediss://"):
                    fb = url.replace("rediss://", "redis://", 1)
                    try:
                        return try_search(fb)
                    except Exception:
                        logger.warning("RedisSearch non-TLS fallback failed")
    if RedisVectorStore is not None:
        try:
            return try_simple(url)
        except Exception as e:
            msg = str(e)
            logger.warning("RedisVectorStore init error: %s", msg)
            if ("WRONG_VERSION_NUMBER" in msg) or ("ssl" in msg.lower()):
                if url.startswith("rediss://"):
                    fb = url.replace("rediss://", "redis://", 1)
                    try:
                        return try_simple(fb)
                    except Exception:
                        logger.warning("RedisVectorStore non-TLS fallback failed")
    raise RuntimeError("No Redis-backed vector store available or connection failed")


def collect_documents() -> List[Dict[str, Any]]:
    roots = [
        os.path.join(os.getcwd(), "sources"),
        os.path.join(os.getcwd(), "docs"),
        os.path.join(os.getcwd(), "src", "agriconnect"),
    ]
    exts = (".txt", ".md", ".html", ".json", ".csv")
    collected: List[Dict[str, Any]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(exts):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                except Exception:
                    continue
                if fn.lower().endswith(".json"):
                    try:
                        data = json.loads(text)
                        if isinstance(data, list):
                            for entry in data:
                                if isinstance(entry, dict) and entry.get("text"):
                                    collected.append({"text": entry.get("text"), "meta": {"source": path}})
                            continue
                    except Exception:
                        pass
                collected.append({"text": text, "meta": {"source": path}})
    return collected


def ingest(items: List[Dict[str, Any]], store, dim: int = 384, batch_size: int = 200, limit: int = 0, pause: float = 0.0) -> int:
    total = 0
    client = getattr(store, "client", None) if store is not None else None
    ns = getattr(store, "ns", "rag") if store is not None else "rag"

    if client is not None:
        docs_key = f"{ns}:docs"
        p = client.pipeline()
        ops = 0
        try:
            for item in items:
                text = item.get("text")
                meta = item.get("meta", {})
                embedding = item.get("embedding")
                if embedding is None:
                    chunks = chunk_text(text)
                    embeddings = embed_texts(chunks, dim=dim)
                    seq = zip(chunks, embeddings)
                else:
                    seq = [(text, embedding)]

                for ch, emb in seq:
                    doc_id = str(uuid.uuid4())
                    arr = np.asarray(emb, dtype=np.float32)
                    b = arr.tobytes()
                    key = f"{ns}:doc:{doc_id}"
                    # When Redis client is configured with `decode_responses=True` and
                    # encoding='latin-1' we must send text/meta as Python strings so the
                    # client performs round-trip preserving raw bytes for the vector field.
                    if client is not None and getattr(client, "decode_responses", False):
                        vec_value = b.decode("latin-1")
                        text_value = ch
                        meta_value = json.dumps(meta, ensure_ascii=False)
                    else:
                        vec_value = b
                        text_value = ch.encode("utf-8")
                        meta_value = json.dumps(meta).encode("utf-8")
                    mapping = {"text": text_value, "meta": meta_value, "vec": vec_value}
                    p.hset(key, mapping=mapping)
                    p.sadd(docs_key, doc_id)
                    ops += 1
                    total += 1

                    if limit and total >= limit:
                        if ops > 0:
                            try:
                                p.execute()
                            except Exception as e:
                                logger.exception("Final batch execute failed: %s", e)
                        return total

                    if ops >= batch_size:
                        try:
                            p.execute()
                        except Exception as e:
                            logger.exception("Batch execute failed; aborting ingest: %s", e)
                            return total
                        ops = 0
                        if pause:
                            time.sleep(pause)

            if ops > 0:
                try:
                    p.execute()
                except Exception as e:
                    logger.exception("Final batch execute failed: %s", e)
                    return total
        except KeyboardInterrupt:
            logger.warning("Ingest interrupted by user after %d items", total)
            try:
                if ops > 0:
                    p.execute()
            except Exception:
                pass
        return total

    # fallback: no redis client, do per-item adds (or collect for JSON)
    for item in items:
        text = item.get("text")
        meta = item.get("meta", {})
        embedding = item.get("embedding")
        if embedding is None:
            chunks = chunk_text(text)
            embeddings = embed_texts(chunks, dim=dim)
            seq = zip(chunks, embeddings)
        else:
            seq = [(text, embedding)]

        for ch, emb in seq:
            doc_id = str(uuid.uuid4())
            if store:
                store.add(doc_id, ch, meta, emb)
            else:
                item.setdefault("_out", []).append({"id": doc_id, "text": ch, "meta": meta, "embedding": emb})
            total += 1
            if limit and total >= limit:
                return total

    return total


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", "-i", help="JSON file with list of documents to ingest")
    p.add_argument("--sample", action="store_true", help="Ingest a small sample payload with random embeddings")
    p.add_argument("--ingest-all", action="store_true", help="Ingest all textual documents found under backend/sources and backend/docs")
    p.add_argument("--dim", type=int, default=384, help="Embedding dimension")
    p.add_argument("--prefer", choices=("auto", "search", "simple"), default="auto", help="Preferred Redis store backend")
    p.add_argument("--redis-url", help="Full Redis URL to use (overrides host/port/db)")
    p.add_argument("--redis-host", help="Redis host (e.g. redis-18836.crce218.eu-central-1-1.ec2.cloud.redislabs.com)")
    p.add_argument("--redis-port", type=int, help="Redis port (e.g. 18836)")
    p.add_argument("--redis-username", help="Redis ACL username (default: default)")
    p.add_argument("--redis-password", help="Redis password (will also read REDIS_PASSWORD env var)")
    p.add_argument("--redis-dbname", help="Optional Redis DB name/identifier (used as username if applicable)")
    p.add_argument("--batch-size", type=int, default=200, help="Number of pipeline ops per execute")
    p.add_argument("--limit", type=int, default=0, help="Maximum number of chunks to ingest (0 = no limit)")
    p.add_argument("--pause", type=float, default=0.0, help="Seconds to sleep between pipeline batch executes")
    args = p.parse_args(argv)

    # Build redis url
    url = args.redis_url or get_redis_url()
    if not url and args.redis_host:
        host = args.redis_host
        port = args.redis_port or 6379
        username = args.redis_username or os.getenv("REDIS_USERNAME") or os.getenv("REDIS_USER") or "default"
        password = args.redis_password or os.getenv("REDIS_PASSWORD") or ""
        tls_env = os.getenv("REDIS_TLS")
        if tls_env is None:
            tls = ("redislabs" in host.lower()) or ("aws" in host.lower() and ":" in host)
        else:
            tls = tls_env.lower() in ("1", "true", "yes")
        scheme = "rediss" if tls else "redis"
        auth = ""
        if password:
            if username:
                auth = f"{quote_plus(username)}:{quote_plus(password)}@"
            else:
                auth = f":{quote_plus(password)}@"
        url = f"{scheme}://{auth}{host}:{port}/0"

    store = None
    if url:
        try:
            store = choose_store(url, dim=args.dim, prefer=args.prefer)
            print("Using Redis store:", type(store).__name__)
        except Exception as e:
            print("Could not initialize Redis store, falling back to local JSON output:", e)
            store = None

    items: List[Dict[str, Any]] = []
    if args.sample:
        items = [{"text": "This is a sample document about agriculture and weather."}]
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            items = json.load(fh)
            if not isinstance(items, list):
                print("Input JSON must be a list of objects with 'text' fields")
                return 2
    elif args.ingest_all:
        print("Collecting documents from backend/sources, backend/docs, and src/agriconnect...")
        items = collect_documents()
        if not items:
            print("No documents found to ingest. Check directories or run with --input/--sample.")
            return 2
    else:
        print("Provide --input or --sample. See help for usage.")
        return 2

    try:
        count = ingest(items, store, dim=args.dim, batch_size=args.batch_size, limit=args.limit, pause=args.pause)
        print(f"Ingested {count} chunks")
        if store is None:
            out = []
            for it in items:
                out.extend(it.get("_out", []))
            out_file = "redis_ingest_output.json"
            with open(out_file, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False, indent=2)
            print("Wrote embeddings to", out_file)
        return 0
    except Exception as e:
        logger.exception("Ingest failed: %s", e)
        print("Ingest failed:", e)
        return 3


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
