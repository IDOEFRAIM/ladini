from __future__ import annotations

import json
from pathlib import Path

import yaml

report_path = Path("tmp/global_scraper_validation_force.json")
sources_path = Path("backend/sources/sources.yaml")
out_path = Path("tmp/technical_truth_table.md")

report = json.loads(report_path.read_text(encoding="utf-8"))
sources_doc = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
source_map = {s.get("id"): s for s in (sources_doc.get("sources") or []) if isinstance(s, dict)}


def strategy_label(src: dict | None) -> str:
    if not isinstance(src, dict):
        return "Unknown"
    engine = str(src.get("engine") or "").lower()
    api_cfg = src.get("api_config") if isinstance(src.get("api_config"), dict) else {}
    selectors = src.get("selectors") if isinstance(src.get("selectors"), dict) else {}
    body = selectors.get("body") if isinstance(selectors.get("body"), list) else []
    if engine == "data_platform" or api_cfg.get("expect_json"):
        return "API"
    if any(str(x).lower() in {"channel", "item"} for x in body):
        return "RSS"
    if engine in {"institutional_pdf", "pdf", "pdf_document"}:
        return "PDF Fallback"
    return "Generic DOM"

rows = report.get("results") or []
lines = [
    "# Tableau de Verite Technique (Mode Force)",
    "",
    f"- Total: {report.get('summary', {}).get('total', 0)}",
    f"- Success: {report.get('summary', {}).get('success', 0)}",
    f"- Failed: {report.get('summary', {}).get('failed', 0)}",
    f"- Success rate: {report.get('summary', {}).get('success_rate', 0)}%",
    "",
    "| Source ID | Strategie Finale | Statut | Contenu Utile (Chars) |",
    "|---|---|---|---:|",
]

for row in rows:
    sid = row.get("source_id")
    src = source_map.get(sid)
    strat = strategy_label(src)
    status = "SUCCESS" if row.get("status") == "SUCCESS" else "FAILED"
    chars = int(row.get("content_size") or 0)
    lines.append(f"| {sid} | {strat} | {status} | {chars} |")

lines.extend(["", "## Focus Sources", ""])
focus = ["agri_sonagess_prices", "lefaso_actualites", "meteo_agromet_decadaire", "meteo_agromet_mensuel", "meteo_anam_bulletins"]
index = {r.get("source_id"): r for r in rows}
for sid in focus:
    r = index.get(sid, {})
    st = "SUCCESS" if r.get("status") == "SUCCESS" else "FAILED"
    lines.append(f"- {sid}: {st} (chars={int(r.get('content_size') or 0)})")

out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Saved: {out_path}")
