from __future__ import annotations

import json
import math
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import io

import numpy as np
from pypdf import PdfReader

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from agriconnect.rag.components import get_embedding_model


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


def read_document(uri: str) -> str:
    """Read a document from a local path, http(s) URL or s3 URI and return text.

    Supports: local files, http(s) (via requests), s3:// (via boto3).
    For PDFs, uses pypdf to extract text from bytes.
    """
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()

    # Helper to extract text from bytes for PDF
    def _text_from_pdf_bytes(b: bytes) -> str:
        reader = PdfReader(io.BytesIO(b))
        pages = [pg.extract_text() or "" for pg in reader.pages]
        return "\n".join(pages)

    # HTTP/HTTPS
    if scheme in ("http", "https"):
        import requests

        resp = requests.get(uri, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "application/pdf" in content_type or uri.lower().endswith(".pdf"):
            return _text_from_pdf_bytes(resp.content)
        if "application/json" in content_type or uri.lower().endswith(".json"):
            data = resp.json()
            if isinstance(data, dict):
                for k in ("text", "content", "body", "markdown"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        return v
            return json.dumps(data, ensure_ascii=False)
        return resp.text

    # S3
    if scheme == "s3":
        try:
            import boto3
        except Exception as e:  # pragma: no cover - optional dependency
            raise RuntimeError("boto3 is required to read s3:// URIs") from e
        s3 = boto3.client("s3")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        with io.BytesIO() as buf:
            s3.download_fileobj(bucket, key, buf)
            b = buf.getvalue()
        if key.lower().endswith(".pdf"):
            return _text_from_pdf_bytes(b)
        if key.lower().endswith(".json"):
            data = json.loads(b.decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                for k in ("text", "content", "body", "markdown"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        return v
            return json.dumps(data, ensure_ascii=False)
        return b.decode("utf-8", errors="ignore")

    # Fallback: local filesystem path
    p = Path(uri)
    suffix = p.suffix.lower()
    if suffix == ".txt":
        return p.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in ("text", "content", "body", "markdown"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        return json.dumps(data, ensure_ascii=False)
    if suffix == ".pdf":
        reader = PdfReader(str(p))
        pages = [pg.extract_text() or "" for pg in reader.pages]
        return "\n".join(pages)
    return p.read_text(encoding="utf-8", errors="ignore")


def chunk_text(text: str, chunk_size: int = 850, overlap: int = 150) -> list[str]:
    txt = " ".join((text or "").split())
    if not txt:
        return []
    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    for i in range(0, len(txt), step):
        chunk = txt[i:i + chunk_size]
        if chunk:
            chunks.append(chunk)
        if i + chunk_size >= len(txt):
            break
    return chunks


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(np.dot(a / na, b / nb))


def main() -> int:
    eval_items: list[dict[str, Any]] = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

    doc_map: dict[str, dict[str, Any]] = {}
    for item in eval_items:
        source = str(item["source"])
        uri = str(item["doc_path"])
        if source in doc_map:
            continue
        text = read_document(uri)
        doc_map[source] = {"path": uri, "text": text}

    chunks: list[dict[str, Any]] = []
    for source, payload in doc_map.items():
        c = chunk_text(payload["text"])
        for idx, chunk in enumerate(c):
            chunks.append({"source": source, "chunk_id": idx, "text": chunk})

    if not chunks:
        raise SystemExit("No chunks available for retrieval benchmark")

    model = get_embedding_model()
    chunk_texts = [c["text"] for c in chunks]
    chunk_vecs = np.asarray(model.get_text_embedding_batch(chunk_texts), dtype=np.float32)

    results: list[dict[str, Any]] = []
    hits_top5 = 0
    hits_top1 = 0
    semantic_alerts = []

    for item in eval_items:
        q = str(item["question"])
        expected_context = str(item["expected_context"])
        expected_norm = normalize_text(expected_context)
        expected_source = str(item["source"])

        q_vec = np.asarray(model.get_query_embedding(q), dtype=np.float32)
        sims = np.array([cosine(q_vec, vec) for vec in chunk_vecs], dtype=np.float32)
        top_idx = np.argsort(-sims)[:5]

        top_rows = []
        expected_positions = []
        best_expected_score = -1.0
        best_wrong_score = -1.0

        for rank, idx in enumerate(top_idx, start=1):
            row = chunks[int(idx)]
            score = float(sims[int(idx)])
            row_norm = normalize_text(row["text"])
            contains_expected = (expected_norm in row_norm) or (overlap_ratio(expected_context, row["text"]) >= 0.72)
            if contains_expected:
                expected_positions.append(rank)
                best_expected_score = max(best_expected_score, score)
            else:
                best_wrong_score = max(best_wrong_score, score)
            top_rows.append(
                {
                    "rank": rank,
                    "source": row["source"],
                    "chunk_id": row["chunk_id"],
                    "score": round(score, 6),
                    "contains_expected_context": contains_expected,
                    "preview": row["text"][:180],
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
        md.append(f"| {r['question']} | {'YES' if r['top5_hit'] else 'NO'} | {'YES' if r['top1_hit'] else 'NO'} | {best_rank} |")

    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps({
        "report_json": str(OUT_JSON),
        "report_md": str(OUT_MD),
        "hit_rate_top5_percent": payload["hit_rate_top5_percent"],
        "precision_at_1_percent": payload["precision_at_1_percent"],
        "semantic_alert_count": payload["semantic_alert_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
