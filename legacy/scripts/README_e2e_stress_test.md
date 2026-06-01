# E2E Stress Test: Discovery -> Ingestion

This script validates a robust offline pipeline for scraper discovery and PDF ingestion.

## What it validates

- Discovery records are written through `DiscoveryWriter` using an in-memory stream (`io.StringIO`).
- Each discovered record is ingested immediately via `PdfDownloader.handle(record)`.
- Domain quarantine behavior is simulated (`403/429` style) and asserted.
- Invalid binary payloads are rejected using `%PDF` magic-bytes validation.
- `confidence_score` from discovery metadata is propagated into final `RawDocument` metadata.
- At least one successful extraction uses `pdfplumber`.
- An audit report is printed with:
  - `Scraper_Type`
  - `URLs_Discovered`
  - `PDFs_Downloaded`
  - `Extraction_Success_Rate`
  - `Avg_Time_Per_Doc_ms`

## Run

```powershell
& ".venv\Scripts\python.exe" scripts\run_e2e_stress_test.py
```
