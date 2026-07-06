from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests

from futur.scraper.scrapers.shared.message_models import ScraperQueueMessage


def _build_relative_pdf_path(source_id: str, url: str, digest: str) -> str:
    now = datetime.now(timezone.utc)
    filename = os.path.basename(urlparse(url).path) or "document.pdf"
    return f"raws_data/pdf/{source_id}/{now.year:04d}/{now.month:02d}/{now.day:02d}/{digest[:16]}_{filename}"


def _resolve_storage_root() -> Path:
    storage_root = Path(os.getenv("PDF_STORAGE_PATH", "/app/data/pdfs"))
    storage_root.mkdir(parents=True, exist_ok=True)
    return storage_root


def _download_pdf_bytes(url: str, timeout: int) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.content
    if not payload.startswith(b"%PDF"):
        raise ValueError("Downloaded payload is not a PDF")
    return payload


def _process_record(record: Dict[str, Any], storage_root: Path, timeout: int) -> Dict[str, Any]:
    body = json.loads(record.get("body") or "{}")
    message = ScraperQueueMessage.model_validate(body)

    payload = _download_pdf_bytes(str(message.url), timeout=timeout)
    digest = sha256(payload).hexdigest()
    relative_path = _build_relative_pdf_path(message.source_id, str(message.url), digest)
    local_path = storage_root / relative_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(payload)

    return {
        "trace_id": str(message.trace_id),
        "source_id": message.source_id,
        "url": str(message.url),
        "status": "SUCCESS",
        "storage_path": str(local_path),
        "relative_path": relative_path,
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    storage_root = _resolve_storage_root()
    timeout = int(os.getenv("PDF_DOWNLOAD_TIMEOUT", "45"))

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for record in event.get("Records", []):
        try:
            results.append(_process_record(record=record, storage_root=storage_root, timeout=timeout))
        except Exception as exc:  # pylint: disable=broad-except
            failures.append(
                {
                    "record": record,
                    "error": str(exc),
                    "status": "ERROR",
                }
            )

    return {
        "processed": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
