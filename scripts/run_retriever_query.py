#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agriconnect.rag.components import EMBEDDING_DIM, get_embedding_model  # noqa: E402
from agriconnect.rag.core.models import QueryBundle  # noqa: E402
from agriconnect.rag.providers.redis_search_provider import RedisSearchProvider  # noqa: E402


def _vector_preview(vec: np.ndarray, n: int = 12):
    return [float(x) for x in vec[:n]]


def _decode_vec(raw):
    if raw is None:
        return np.asarray([], dtype=np.float32)
    if isinstance(raw, str):
        raw = raw.encode("latin-1", errors="ignore")
    return np.frombuffer(raw, dtype=np.float32)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default=os.getenv("AGRICONNECT_REDIS_URL", "rediss://127.0.0.1:6380/0"))
    parser.add_argument("--query", default="agriculture")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--preview-dims", type=int, default=12)
    args = parser.parse_args()

    provider = RedisSearchProvider(
        url=args.redis_url,
        dim=int(EMBEDDING_DIM),
        ensure_index=False,
        allow_degraded_mode=True,
        decode_responses=False,
    )
    health = provider.health()

    emb_model = get_embedding_model()
    q_vec = np.asarray(emb_model.get_query_embedding(args.query) or [], dtype=np.float32)

    bundle = QueryBundle(vector=q_vec.tolist(), top_k=int(args.top_k), filters={}, text_query=args.query)
    nodes = provider.query(bundle)

    retrieved = []
    for node in nodes:
        key = provider._doc_key(str(node.id))
        raw_vec = provider.client.hget(key, "vec")
        vec = _decode_vec(raw_vec)
        retrieved.append(
            {
                "id": node.id,
                "score": float(node.score or 0.0),
                "vector_dim": int(vec.shape[0]),
                "vector_norm": float(np.linalg.norm(vec)) if vec.size else 0.0,
                "vector_preview": _vector_preview(vec, int(args.preview_dims)),
                "text_preview": (node.text or "")[:180],
            }
        )

    # If query returned nothing, still show that vectors are present in Redis.
    fallback_samples = []
    if not retrieved:
        keys = provider.client.scan_iter(match=f"{provider._prefix}*", count=10)
        for key in keys:
            k = key.decode("utf-8", errors="ignore") if isinstance(key, (bytes, bytearray)) else str(key)
            raw_vec = provider.client.hget(k, "vec")
            vec = _decode_vec(raw_vec)
            fallback_samples.append(
                {
                    "redis_key": k,
                    "vector_dim": int(vec.shape[0]),
                    "vector_norm": float(np.linalg.norm(vec)) if vec.size else 0.0,
                    "vector_preview": _vector_preview(vec, int(args.preview_dims)),
                }
            )
            if len(fallback_samples) >= 3:
                break

    output = {
        "query": args.query,
        "health": health,
        "query_vector": {
            "dim": int(q_vec.shape[0]),
            "norm": float(np.linalg.norm(q_vec)) if q_vec.size else 0.0,
            "preview": _vector_preview(q_vec, int(args.preview_dims)),
        },
        "retrieved_count": len(retrieved),
        "retrieved": retrieved,
        "fallback_vector_samples": fallback_samples,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
