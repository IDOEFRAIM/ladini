from __future__ import annotations

import json
from pathlib import Path


REPORT_PATH = Path("tmp/global_scraper_validation_full.json")
OUT_PATH = Path("tmp/closure_semantic_report.md")


def note_for(source_id: str, err: str, status: str) -> str:
    if status == "SUCCESS":
        if source_id == "lefaso_actualites":
            return "Flux RSS configure et extraction validee."
        if source_id == "meteo_agromet_mensuel":
            return "Backoff reset connexion + timeout etendus, collecte validee."
        return "Collecte conforme."

    low = (err or "").lower()
    if "robots.txt forbids scraping" in low:
        if source_id == "lefaso_actualites":
            return "Flux RSS active, mais acces bloque par robots.txt (mode production conforme)."
        if source_id == "meteo_agromet_mensuel":
            return "Backoff configure, mais blocage robots.txt intervient avant la phase reseau."
        return "Blocage robots.txt (compliance active)."
    if "connection reset" in low or "10054" in low:
        return "Instabilite reseau (ConnectionReset) malgre retries/backoff."
    if "scrape_failed" in low:
        return "Extraction vide: ajustement de selecteurs requis."
    return "Echec technique a investiguer."


def main() -> int:
    data = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    rows = data.get("results", [])

    lines = [
        "# Rapport de Cloture Semantique",
        "",
        f"- Total: {summary.get('total', 0)}",
        f"- Success: {summary.get('success', 0)}",
        f"- Failed: {summary.get('failed', 0)}",
        f"- Success rate: {summary.get('success_rate', 0)}%",
        "",
        "| Source | Volume final (Ko) | Status | Note technique |",
        "|---|---:|---|---|",
    ]

    for row in rows:
        sid = str(row.get("source_id") or "")
        status = "SUCCESS" if row.get("status") == "SUCCESS" else "FAILED"
        chars = int(row.get("content_size") or 0)
        ko = round(chars / 1024.0, 2)
        err = str(row.get("error") or "")
        note = note_for(sid, err, status)
        lines.append(f"| {sid} | {ko} | {status} | {note} |")

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
