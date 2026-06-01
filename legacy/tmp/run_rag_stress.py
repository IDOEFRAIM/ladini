from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

# Ensure local imports work from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from agriconnect.core.settings import settings
from agriconnect.rag.components import get_embedding_model
from agriconnect.rag.providers.redis_search_provider import RedisSearchProvider


def _decode(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="ignore")
    return str(v)


def _parse_meta(raw: Any) -> Dict[str, Any]:
    try:
        return json.loads(_decode(raw) or "{}")
    except Exception:
        return {}


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(np.dot(a / na, b / nb))


def main() -> int:
    targets = {"sidwaya_actualites", "insd_microdata_resources", "meteo_anam_bulletins"}
    queries = [
        "Lien entre pluviometrie ANAM et prix SONAGESS",
        "Tendances agroclimatiques Burkina et impacts sur production",
        "Microdata INSD et indicateurs utiles a la securite alimentaire",
    ]

    redis_url = os.getenv("AGRICONNECT_REDIS_URL", settings.REDIS_URL)
    provider = RedisSearchProvider(
        url=redis_url,
        dim=int(settings.RAG_EMBEDDING_DIM),
        ensure_index=False,
        allow_degraded_mode=True,
        decode_responses=False,
    )

    ids = list(provider.client.smembers(provider._docs_key))
    docs: List[Dict[str, Any]] = []
    for raw_id in ids:
        doc_id = _decode(raw_id)
        key = provider._doc_key(doc_id)
        metadata = _parse_meta(provider.client.hget(key, "metadata") or provider.client.hget(key, "meta"))
        source_id = str(metadata.get("source_id") or "")
        if source_id not in targets:
            continue
        if not bool(metadata.get("is_indexed", False)):
            continue
        vec_b = provider.client.hget(key, "vec")
        if isinstance(vec_b, str):
            vec_b = vec_b.encode("latin-1", errors="ignore")
        vec = np.frombuffer(vec_b or b"", dtype=np.float32)
        if vec.shape[0] != int(settings.RAG_EMBEDDING_DIM):
            continue
        docs.append({"id": doc_id, "source_id": source_id, "vec": vec, "metadata": metadata})

    if not docs:
        raise SystemExit("No indexed target docs found for stress test")

    embedder = get_embedding_model()
    results: List[Dict[str, Any]] = []

    for q in queries:
        q_vec = np.asarray(embedder.get_query_embedding(q) or [], dtype=np.float32)
        if q_vec.shape[0] != int(settings.RAG_EMBEDDING_DIM):
            raise SystemExit(f"Embedding dimension mismatch for query: got {q_vec.shape[0]}")

        scored = []
        for d in docs:
            sim = _cosine(q_vec, d["vec"])
            scored.append({"doc_id": d["id"], "source_id": d["source_id"], "cosine": round(sim, 6)})

        scored.sort(key=lambda x: x["cosine"], reverse=True)
        top = scored[:5]
        best_by_source: Dict[str, float] = {}
        for row in scored:
            sid = row["source_id"]
            if sid not in best_by_source:
                best_by_source[sid] = float(row["cosine"])

        results.append({
            "query": q,
            "top5": top,
            "best_by_source": {k: round(v, 6) for k, v in best_by_source.items()},
            "threshold_pass": all(float(best_by_source.get(s, 0.0)) > 0.85 for s in targets),
        })

    overall_pass = all(r["threshold_pass"] for r in results)

    payload = {
        "targets": sorted(list(targets)),
        "threshold": 0.85,
        "overall_pass": overall_pass,
        "results": results,
    }

    out_path = Path("tmp") / "rag_integrity_stress_report.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"overall_pass": overall_pass, "report": str(out_path)}, ensure_ascii=False))

    return 0 if overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
