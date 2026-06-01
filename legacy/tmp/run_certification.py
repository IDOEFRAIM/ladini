from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure local imports work from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from agriconnect.core.settings import settings
from agriconnect.domain.ingestion.worker import IngestionWorker
from agriconnect.rag.config import RAW_DATA_DIR
from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator


def _log_density(log_obj: Any) -> float:
    if log_obj is None:
        return 0.0
    if isinstance(log_obj, dict):
        try:
            return float(log_obj.get("content_density") or 0.0)
        except Exception:
            return 0.0
    try:
        return float(getattr(log_obj, "content_density", 0.0) or 0.0)
    except Exception:
        return 0.0


def _strategy_label(source: Dict[str, Any]) -> str:
    engine = str(source.get("engine") or "").strip().lower()
    cfg = source.get("config") if isinstance(source.get("config"), dict) else {}
    selectors = cfg.get("selectors") if isinstance(cfg.get("selectors"), dict) else {}
    engine_cfg = cfg.get("engine_config") if isinstance(cfg.get("engine_config"), dict) else {}
    api_cfg = cfg.get("api_config") if isinstance(cfg.get("api_config"), dict) else {}

    body = selectors.get("body")
    parser_type = str(engine_cfg.get("parser_type") or "").lower()

    if engine == "data_platform" or bool(api_cfg.get("expect_json")):
        return "API"
    if isinstance(body, list) and any(str(x).lower() in {"channel", "item"} for x in body):
        return "RSS"
    if "rss" in parser_type:
        return "RSS"
    if engine in {"institutional_pdf", "pdf", "pdf_document"} or engine_cfg.get("pdf_fallback_order"):
        return "PDF Fallback"
    return "Generic DOM"


def _semantic_score(density: float) -> int:
    density = max(0.0, min(1.0, float(density or 0.0)))
    # 1..5 score based on extracted useful text density.
    return max(1, min(5, int(round(density * 5))))


def main() -> int:
    orchestrator = ScraperOrchestrator(auto_discover=True, strict_init=True)
    worker = IngestionWorker(raw_data_dir=str(RAW_DATA_DIR), max_files=0)

    rows: List[Dict[str, Any]] = []
    for source_id in orchestrator.list_sources():
        source = orchestrator.sources.get(source_id) or {}
        strategy = _strategy_label(source)
        run_result = orchestrator.run_source(source_id)

        row: Dict[str, Any] = {
            "source_id": source_id,
            "strategy": strategy,
            "status": "FAILED",
            "content_chars": 0,
            "quality_score": 1,
            "density": 0.0,
            "error": None,
        }

        if run_result.status != "SUCCESS" or run_result.document is None:
            row["status"] = "FAILED"
            row["error"] = run_result.error or "scrape_failed"
            rows.append(row)
            continue

        document = run_result.document
        density = _log_density(run_result.log)
        row["content_chars"] = len(document.content_markdown or "")
        row["density"] = round(density, 4)
        row["quality_score"] = _semantic_score(density)

        try:
            s3_key = worker.s3_manager.save_raw(document)
            worker.process_raw_document(document=document, s3_key=s3_key)
            row["status"] = "SUCCESS"
        except Exception as exc:
            row["status"] = "FAILED"
            row["error"] = str(exc)

        rows.append(row)

    success = sum(1 for r in rows if r["status"] == "SUCCESS")
    total = len(rows)
    failed = total - success

    out_json = {
        "summary": {
            "total": total,
            "success": success,
            "failed": failed,
            "embedding_dim": int(getattr(settings, "RAG_EMBEDDING_DIM", 0) or 0),
        },
        "rows": rows,
    }

    out_dir = Path("tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "master_impact_report.json"
    md_path = out_dir / "master_impact_report.md"

    json_path.write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# Master Impact Report",
        "",
        f"- Total sources: {total}",
        f"- Success: {success}",
        f"- Failed: {failed}",
        "",
        "| Source ID | Strategie Finale | Statut | Contenu Utile (Chars) | Qualite Semantique (1-5) |",
        "|---|---|---:|---:|---:|",
    ]

    for r in rows:
        status_icon = "✅ SUCCESS" if r["status"] == "SUCCESS" else "❌ FAILED"
        md_lines.append(
            f"| {r['source_id']} | {r['strategy']} | {status_icon} | {int(r['content_chars'])} | {int(r['quality_score'])} |"
        )

    failures = [r for r in rows if r["status"] != "SUCCESS"]
    if failures:
        md_lines.extend(["", "## Failed Sources", ""])
        for item in failures:
            md_lines.append(f"- {item['source_id']}: {item.get('error') or 'unknown_error'}")

    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(json.dumps(out_json["summary"], ensure_ascii=False))
    print(f"JSON: {json_path}")
    print(f"MD: {md_path}")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
