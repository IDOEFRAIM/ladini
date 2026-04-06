import sys
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from time import time
from pathlib import Path

# Ensure package root on path
sys.path.insert(0, "backend/src")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def dump_node(n):
    try:
        text = n.node.get_content()
    except Exception:
        text = str(getattr(n, "node", n))
    meta = getattr(n.node, "metadata", {}) if hasattr(n, "node") else {}
    score = getattr(n, "score", None)
    return {"text": text[:200], "score": score, "meta": meta}


def main():
    try:
        from agriconnect.rag.retriever import AgileRetriever
    except Exception as e:
        print("Failed to import AgileRetriever:", e)
        raise

    start = time()
    r = AgileRetriever()
    took = time() - start
    print(f"Instantiated AgileRetriever in {took:.2f}s")
    print("Attributes: index=", bool(getattr(r, "index", None)), "vector_retriever=", bool(getattr(r, "vector_retriever", None)), "vector_store=", bool(getattr(r, "vector_store", None)), "ready=", getattr(r, "ready", None))

    # Inspect memory file that search_memory uses
    base = Path(__file__).resolve().parents[1]
    mem_file = base / "backend" / "src" / "agriconnect" / "rag_db" / "memory.json"
    print("Memory file path:", mem_file)
    if mem_file.exists():
        try:
            import json

            with mem_file.open("r", encoding="utf-8") as fh:
                entries = json.load(fh)
            print("memory.json entries:", len(entries))
            if entries:
                print("first entry sample:", entries[0])
        except Exception as e:
            print("Failed reading memory.json:", e)
    else:
        print("memory.json does not exist; search_memory will return empty list")

    # Test search_memory (fast, file-based fallback)
    try:
        mem = r.search_memory(user_id="", query="maïs", top_k=5)
        print("search_memory returned count:", len(mem))
        if mem:
            print(mem)
    except Exception as e:
        print("search_memory failed:", e)

    # Optionally run heavy probes (embedding + vector search).
    full = "--full" in sys.argv
    if not full:
        print("Skipping embedding and vector search probes. Re-run with --full to allow heavy model loads.")
        return

    # Probe embedding for diagnostics (may download/load a model)
    try:
        emb = None
        if hasattr(r, "_embed_query"):
            emb = r._embed_query("maïs")
            print("_embed_query ->", None if emb is None else f"len={len(emb)}")
    except Exception as e:
        print("_embed_query raised:", e)

    # Test vector search with timeout to avoid long model download on cold start
    def do_search():
        try:
            s0 = time()
            nodes = r.search("maïs", user_level="debutant", use_hyde=False)
            elapsed = time() - s0
            out = {"ok": True, "elapsed": elapsed, "count": len(nodes)}
            # dump some node content
            sample = [dump_node(n) for n in nodes[:5]]
            out["sample"] = sample
            return out
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    print("Starting vector search with 20s timeout (may trigger heavy model loads)...")
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(do_search)
        try:
            res = fut.result(timeout=20)
            print("search result:", res)
        except TimeoutError:
            print("search timed out after 20s")
        except Exception as e:
            print("search raised:", e)


if __name__ == "__main__":
    main()
