"""Resume-upload embeddings from redis_ingest_output.json into Redis (hotset loader).

Purpose: load a small working set into your Redis instance for testing `mcp_rag_server` when
the full DB is out of capacity.

Usage example:
  python scripts/redis_resume_upload.py --input ../redis_ingest_output.json \
    --redis-url "redis://default:***@host:18836/0" --hotset-size 400 --batch-size 50 --pause 1 --prefer simple
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

logger = logging.getLogger("redis_resume_upload")


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

    if RedisSearchVectorStore is not None:
        try:
            return try_search(url)
        except Exception:
            pass
    if RedisVectorStore is not None:
        return try_simple(url)
    raise RuntimeError("No Redis-backed vector store available or connection failed")


def bytes_from_embedding(emb: List[float]):
    if np is None:
        # fallback: pack floats to bytes using float32
        import struct

        return struct.pack(f"{len(emb)}f", *emb)
    arr = np.asarray(emb, dtype=np.float32)
    return arr.tobytes()


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", "-i", required=True, help="Path to redis_ingest_output.json")
    p.add_argument("--redis-url", help="Redis URL to upload into")
    p.add_argument("--hotset-size", type=int, default=400, help="Number of items to upload")
    p.add_argument("--start", type=int, default=0, help="Start index in the JSON list")
    p.add_argument("--dim", type=int, default=384, help="Embedding dimension")
    p.add_argument("--prefer", choices=("auto", "search", "simple"), default="simple")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--pause", type=float, default=1.0)
    args = p.parse_args(argv)

    if not os.path.exists(args.input):
        print("Input file not found:", args.input)
        return 2

    with open(args.input, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, list):
        print("Input JSON must be a list")
        return 2

    sub = data[args.start : args.start + args.hotset_size]
    print(f"Uploading {len(sub)} items starting at {args.start}")

    url = args.redis_url or os.getenv("REDIS_URL")
    store = None
    if url:
        try:
            store = choose_store(url, dim=args.dim, prefer=args.prefer)
            print("Connected to Redis store:", type(store).__name__)
        except Exception as e:
            print("Could not connect to Redis, aborting:", e)
            return 3
    else:
        print("No Redis URL provided; aborting")
        return 2

    client = getattr(store, "client", None)
    ns = getattr(store, "ns", "rag")
    docs_key = f"{ns}:docs"

    p_pipe = client.pipeline()
    ops = 0
    uploaded = 0
    batch = args.batch_size
    i = args.start

    from redis.exceptions import OutOfMemoryError

    try:
        for item in sub:
            emb = item.get("embedding")
            if emb is None:
                continue
            doc_id = item.get("id") or str(uuid.uuid4())
            key = f"{ns}:doc:{doc_id}"
            b = bytes_from_embedding(emb)
            mapping = {b"text": item.get("text", "").encode("utf-8"), b"meta": json.dumps(item.get("meta", {})).encode("utf-8"), b"vec": b}
            p_pipe.hset(key, mapping=mapping)
            p_pipe.sadd(docs_key, doc_id)
            ops += 1
            uploaded += 1
            i += 1

            if ops >= batch:
                try:
                    p_pipe.execute()
                except OutOfMemoryError as oom:
                    print("Redis OOM encountered; reducing batch and backing off")
                    batch = max(1, batch // 2)
                    time.sleep(5)
                    # reset pipeline and continue
                    p_pipe = client.pipeline()
                    ops = 0
                    continue
                except Exception as e:
                    logger.exception("Pipeline execute failed: %s", e)
                    return 4
                ops = 0
                if args.pause:
                    time.sleep(args.pause)

        if ops > 0:
            try:
                p_pipe.execute()
            except OutOfMemoryError:
                print("Final batch caused OOM; try smaller batch-size")
            except Exception as e:
                logger.exception("Final pipeline failed: %s", e)

    except KeyboardInterrupt:
        print("Interrupted by user")

    print(f"Uploaded {uploaded} items to Redis (namespace={ns})")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
