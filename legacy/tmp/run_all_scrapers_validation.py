from __future__ import annotations

import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator


def main() -> int:
    started = time.time()
    orchestrator = ScraperOrchestrator(auto_discover=True, strict_init=False)

    rows = []
    for item in orchestrator.run_all():
        doc = getattr(item, "document", None)
        log = getattr(item, "log", None)

        duration_ms = None
        if log is not None:
            if isinstance(log, dict):
                duration_ms = log.get("duration_ms")
            else:
                duration_ms = getattr(log, "duration_ms", None)

        rows.append(
            {
                "source_id": item.source_id,
                "status": item.status,
                "error": item.error,
                "content_size": len(getattr(doc, "content_markdown", "") or "") if doc is not None else 0,
                "title": getattr(doc, "title", None) if doc is not None else None,
                "duration_ms": duration_ms,
            }
        )

    success = sum(1 for row in rows if row["status"] == "SUCCESS")
    failed = len(rows) - success

    report = {
        "status": "SUCCESS" if failed == 0 else "PARTIAL_SUCCESS",
        "elapsed_seconds": round(time.time() - started, 2),
        "summary": {
            "total": len(rows),
            "success": success,
            "failed": failed,
            "success_rate": round((success / len(rows) * 100.0), 2) if rows else 0.0,
        },
        "results": rows,
    }

    out = Path("tmp") / "global_scraper_validation_full.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if failed:
        print("FAILED_SOURCES:", ",".join(row["source_id"] for row in rows if row["status"] != "SUCCESS"))
    print(f"Report saved to: {out}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
