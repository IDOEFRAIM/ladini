"""Local retrieval benchmark (numpy-based) to measure query latency and throughput.

This avoids external dependencies (Redis/FAISS) and gives a reproducible baseline
for vector search performance on the current machine.

Outputs JSON report to `tmp/local_retrieval_benchmark.json`.
"""
import json
import time
from pathlib import Path
import numpy as np


def run_benchmark(num_docs=100000, dim=384, top_k=10, iterations=5, seed=42):
    rng = np.random.default_rng(seed)
    docs = rng.normal(size=(num_docs, dim)).astype(np.float32)
    query = rng.normal(size=(dim,)).astype(np.float32)

    # Pre-normalize for cosine similarity via dot of normalized vectors
    def normalize(v):
        n = np.linalg.norm(v, axis=-1, keepdims=True)
        n[n == 0] = 1.0
        return v / n

    docs_n = normalize(docs)
    q_n = normalize(query)

    # Warmup
    _ = docs_n @ q_n

    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        sims = docs_n @ q_n
        # get top-k (argpartition is faster)
        idx = np.argpartition(-sims, top_k)[:top_k]
        topk = idx[np.argsort(-sims[idx])]
        t1 = time.perf_counter()
        times.append(t1 - t0)

    avg = sum(times) / len(times)
    docs_per_sec = num_docs / avg

    out = {
        "num_docs": num_docs,
        "dim": dim,
        "top_k": top_k,
        "iterations": iterations,
        "times_s": times,
        "avg_time_s": avg,
        "docs_per_sec": docs_per_sec,
        "topk_sample": topk.tolist(),
    }

    out_path = Path("tmp") / "local_retrieval_benchmark.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": "ok", "report": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    # default params reasonably large but safe for developer machines
    run_benchmark(num_docs=50000, dim=384, top_k=10, iterations=5, seed=42)
