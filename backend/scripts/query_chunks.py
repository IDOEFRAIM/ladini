#!/usr/bin/env python3
"""Query the vector store for a question and print top chunk scores.

Usage:
  python backend/scripts/query_chunks.py --question "Quel est le risque de sécheresse cette décade?" --k 44
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agriconnect.scripts.query_chunks")

# Allow running this script from workspace root without installing the package.
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "backend" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def embed_query_text(text: str):
    """Compute an embedding for `text` using the configured embedder or fallback.

    Returns a list[float].
    """
    # Prefer a local sentence-transformers model to avoid remote/API embedder
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        emb = model.encode([text])[0].tolist()
        return emb
    except Exception:
        logger.debug("Local sentence-transformers not available or failed; trying Settings embedder.")

    try:
        # Fallback: use configured llama_index Settings embed_model when available
        from llama_index.core import Settings

        embed_model = getattr(Settings, "embed_model", None)
        if embed_model is not None and hasattr(embed_model, "get_query_embedding"):
            vec = embed_model.get_query_embedding(text)
            if vec is not None:
                return list(vec)
    except Exception:
        logger.exception("Embedding computation failed via Settings embedder.")
        raise


def extract_node_content(node: Any) -> Dict[str, Any]:
    # node can be llama_index TextNode, or a simple wrapper created by RedisVectorStore
    text = ""
    meta = {}
    node_id = None
    try:
        # llama_index TextNode has get_content and metadata
        if hasattr(node, "get_content"):
            text = node.get_content()
            meta = getattr(node, "metadata", {}) or {}
        # some wrappers have node.get_content
        elif hasattr(node, "node") and hasattr(node.node, "get_content"):
            text = node.node.get_content()
            meta = getattr(node.node, "metadata", {}) or {}
        # SimpleNamespace created in redis_store uses get_content callable
        elif hasattr(node, "get_content"):
            try:
                text = node.get_content()
            except Exception:
                text = str(node)
            meta = getattr(node, "extra_info", {}) or {}
        else:
            text = str(node)
    except Exception:
        text = str(node)

    node_id = getattr(node, "node_id", None) or getattr(node, "id", None) or getattr(node, "node_id", None)
    return {"id": node_id, "text": text, "meta": meta}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--question", "-q", default="Quel est le risque de sécheresse cette décade?", help="Question to ask")
    p.add_argument("--k", type=int, default=5, help="How many top chunks to return")
    p.add_argument("--redis-url", default="", help="Optional Redis URL override (ex: rediss://127.0.0.1:6379)")
    p.add_argument("--local-tunnel", action="store_true", help="Shortcut for --redis-url rediss://127.0.0.1:6379")
    p.add_argument("--out", default="", help="Optional output JSON file path")
    args = p.parse_args(argv)

    # Important: components.get_vector_store() prefers VALKEY_ENDPOINT over REDIS_URL.
    # For tunnel usage we must clear VALKEY_ENDPOINT and force REDIS_URL.
    if args.local_tunnel:
        os.environ["VALKEY_ENDPOINT"] = ""
        os.environ["REDIS_URL"] = "rediss://127.0.0.1:6379"
        os.environ["VALKEY_USE_TLS"] = "true"
        logger.info("Using local SSH tunnel at rediss://127.0.0.1:6379")
    elif args.redis_url:
        os.environ["VALKEY_ENDPOINT"] = ""
        os.environ["REDIS_URL"] = args.redis_url
        os.environ["VALKEY_USE_TLS"] = "true" if args.redis_url.startswith("rediss://") else "false"
        logger.info("Using explicit Redis URL override: %s", args.redis_url)

    # Lazy import of vector store components to avoid heavy imports at module load
    try:
        from agriconnect.rag.components import get_vector_store
    except Exception as e:
        logger.exception("Failed to import vector store components: %s", e)
        raise

    store = get_vector_store()
    if store is None or not hasattr(store, "query"):
        logger.error("Vector store unavailable or does not implement query().")
        sys.exit(2)

    logger.info("Computing embedding for question...")
    q_emb = embed_query_text(args.question)

    logger.info("Querying vector store (k=%d)...", args.k)
    try:
        res = store.query(q_emb, k=args.k)
    except Exception as e:
        logger.exception("Vector store query failed: %s", e)
        raise

    # Normalize result object
    nodes = []
    scores = []
    ids = []
    if hasattr(res, "nodes") and hasattr(res, "similarities"):
        raw_nodes = getattr(res, "nodes") or []
        raw_scores = getattr(res, "similarities") or []
        for i, n in enumerate(raw_nodes):
            info = extract_node_content(n)
            info["score"] = float(raw_scores[i]) if i < len(raw_scores) else 0.0
            # try to attach id from res.ids if missing
            try:
                info_id = getattr(res, "ids", [None] * len(raw_nodes))[i]
                if info_id and not info.get("id"):
                    info["id"] = info_id
            except Exception:
                pass
            nodes.append(info)
    else:
        # If res is a simple iterable
        try:
            for r in res:
                info = extract_node_content(r)
                info["score"] = float(getattr(r, "score", 0.0) or 0.0)
                nodes.append(info)
        except Exception:
            logger.exception("Could not coerce vector store result format")

    out = {"question": args.question, "count": len(nodes), "results": nodes}

    pretty = json.dumps(out, ensure_ascii=False, indent=2)
    print(pretty)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(pretty)
        logger.info("Saved results to %s", args.out)


if __name__ == "__main__":
    main()
