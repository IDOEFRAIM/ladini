from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from agriconnect.rag.retriever import AgileRetriever


EVAL_PATH = Path("tmp/eval_retrieval.json")
OUT_JSON = Path("tmp/retrieval_sniper_report.json")
OUT_MD = Path("tmp/retrieval_sniper_report.md")


def normalize_text(s: str) -> str:
    s = s or ""
    s = " ".join(s.split())
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.casefold()


def overlap_ratio(expected: str, observed: str) -> float:
    exp_tokens = set(t for t in normalize_text(expected).split() if len(t) > 2)
    obs_tokens = set(t for t in normalize_text(observed).split() if len(t) > 2)
    if not exp_tokens:
        return 0.0
    return len(exp_tokens & obs_tokens) / float(len(exp_tokens))


def contains_expected_context(expected_context: str, observed_text: str) -> bool:
    expected_norm = normalize_text(expected_context)
    observed_norm = normalize_text(observed_text)
    return (expected_norm in observed_norm) or (overlap_ratio(expected_context, observed_text) >= 0.72)


def extract_node_fields(node: Any) -> tuple[str, float, str]:
    text = ""
    score = 0.0
    source = "unknown"

    try:
        score = float(getattr(node, "score", 0.0) or 0.0)
    except Exception:
        score = 0.0

    try:
        inner = getattr(node, "node", None)
        if inner is not None and hasattr(inner, "get_content"):
            text = inner.get_content() or ""
            metadata = getattr(inner, "metadata", {}) or {}
        elif hasattr(node, "get_content"):
            text = node.get_content() or ""
            metadata = getattr(node, "metadata", {}) or {}
        else:
            text = str(node)
            metadata = {}

        source = (
            metadata.get("source_id")
            or metadata.get("source")
            or metadata.get("doc_id")
            or metadata.get("document_id")
            or metadata.get("file_path")
            or metadata.get("path")
            or "unknown"
        )
    except Exception:
        text = ""
        source = "unknown"

    return text, score, str(source)


def main() -> int:
    eval_items: list[dict[str, Any]] = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

    retriever = AgileRetriever()
    retriever_ready = bool(getattr(retriever, "ready", False))

    results: list[dict[str, Any]] = []
    hits_top5 = 0
    hits_top1 = 0
    semantic_alerts: list[dict[str, Any]] = []

    for item in eval_items:
        q = str(item.get("question", ""))
        expected_context = str(item.get("expected_context", ""))
        expected_source = str(item.get("source", ""))

        nodes = retriever.search(q, user_level="expert")
        top_nodes = list(nodes[:5]) if nodes else []

        top_rows: list[dict[str, Any]] = []
        expected_positions: list[int] = []
        best_expected_score = -1.0
        best_wrong_score = -1.0

        for rank, node in enumerate(top_nodes, start=1):
            text, score, source = extract_node_fields(node)
            contains_expected = contains_expected_context(expected_context, text)

            if contains_expected:
                expected_positions.append(rank)
                best_expected_score = max(best_expected_score, score)
            else:
                best_wrong_score = max(best_wrong_score, score)

            top_rows.append(
                {
                    "rank": rank,
                    "source": source,
                    "score": round(score, 6),
                    "contains_expected_context": contains_expected,
                    "preview": text[:180],
                }
            )

        top5_hit = len(expected_positions) > 0
        top1_hit = 1 in expected_positions
        if top5_hit:
            hits_top5 += 1
        if top1_hit:
            hits_top1 += 1

        alert = None
        if best_expected_score >= 0 and best_wrong_score > best_expected_score:
            alert = {
                "question": q,
                "expected_source": expected_source,
                "best_expected_score": round(best_expected_score, 6),
                "best_wrong_score": round(best_wrong_score, 6),
                "message": "Un chunk hors contexte score plus haut que le contexte attendu.",
            }
            semantic_alerts.append(alert)

        results.append(
            {
                "question": q,
                "expected_source": expected_source,
                "expected_context": expected_context,
                "top5_hit": top5_hit,
                "top1_hit": top1_hit,
                "expected_positions": expected_positions,
                "top5": top_rows,
                "semantic_alert": alert,
            }
        )

    total = len(eval_items)
    hit_rate = (hits_top5 / total * 100.0) if total else 0.0
    p_at_1 = (hits_top1 / total * 100.0) if total else 0.0

    payload = {
        "retriever": "AgileRetriever",
        "retriever_ready": retriever_ready,
        "total_questions": total,
        "top5_hits": hits_top5,
        "top1_hits": hits_top1,
        "hit_rate_top5_percent": round(hit_rate, 2),
        "precision_at_1_percent": round(p_at_1, 2),
        "semantic_alert_count": len(semantic_alerts),
        "semantic_alerts": semantic_alerts,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# Retrieval Sniper Report",
        "",
        "- Driver: AgileRetriever.search(user_level=\"expert\")",
        f"- Retriever ready: {retriever_ready}",
        f"- Questions: {total}",
        f"- Hit Rate Top-5: {payload['hit_rate_top5_percent']}%",
        f"- Precision@1: {payload['precision_at_1_percent']}%",
        f"- Semantic Alerts: {payload['semantic_alert_count']}",
        "",
        "| Question | Hit Top-5 | Hit Top-1 | Best Rank |",
        "|---|---|---|---:|",
    ]
    for r in results:
        best_rank = min(r["expected_positions"]) if r["expected_positions"] else "-"
        md.append(
            f"| {r['question']} | {'YES' if r['top5_hit'] else 'NO'} | {'YES' if r['top1_hit'] else 'NO'} | {best_rank} |"
        )

    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "report_json": str(OUT_JSON),
                "report_md": str(OUT_MD),
                "retriever": "AgileRetriever",
                "retriever_ready": retriever_ready,
                "hit_rate_top5_percent": payload["hit_rate_top5_percent"],
                "precision_at_1_percent": payload["precision_at_1_percent"],
                "semantic_alert_count": payload["semantic_alert_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
