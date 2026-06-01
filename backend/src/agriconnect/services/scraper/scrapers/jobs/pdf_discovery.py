from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, IO, Union
from urllib.parse import urljoin, urlparse, urlunparse


PDF_DISCOVERY_PATTERNS = (
    ".pdf",
    "pdf",
    "document",
    "bitstream",
    "download",
    "/download/",
    "/getfile/",
    "/uploads/",
    "wp-content/uploads/",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_pdf_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path or ""
    if path != "/":
        path = path.rstrip("/")
    # Normalize network location and path to lowercase to avoid
    # case-sensitivity issues (e.g., .PDF vs .pdf) when deduping.
    normalized = parsed._replace(query="", fragment="", path=path.lower())
    # netloc (host:port) should be compared case-insensitively
    netloc = (normalized.netloc or "").lower()
    normalized = normalized._replace(netloc=netloc)
    return urlunparse(normalized)


def is_pdf_candidate(url: str) -> bool:
    low = (url or "").lower()
    if not low:
        return False
    return any(pat in low for pat in PDF_DISCOVERY_PATTERNS)


def is_wordpress_pdf(url: str) -> bool:
    low = (url or "").lower()
    return "wp-content/uploads/" in low and ".pdf" in low


def surrounding_text_for_anchor(anchor: Any, max_chars: int = 300) -> str:
    parent = getattr(anchor, "parent", None)
    text = ""
    if parent is not None:
        text = (parent.get_text(" ", strip=True) or "")
    if not text and hasattr(anchor, "get_text"):
        text = (anchor.get_text(" ", strip=True) or "")
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def extract_candidate_links(soup: Any, base_url: str) -> list[str]:
    """Extract and normalize crawl candidates from common HTML containers."""
    candidates: list[str] = []

    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href:
            candidates.append(urljoin(base_url, href))

    for iframe in soup.select("iframe[src], embed[src]"):
        src = (iframe.get("src") or "").strip()
        if src:
            candidates.append(urljoin(base_url, src))

    for obj in soup.select("object[data]"):
        data_url = (obj.get("data") or "").strip()
        if data_url:
            candidates.append(urljoin(base_url, data_url))

    for form in soup.select("form[action]"):
        action = (form.get("action") or "").strip()
        if action:
            candidates.append(urljoin(base_url, action))

    meta_refresh = soup.select_one("meta[http-equiv='refresh']")
    if meta_refresh is not None:
        content = str(meta_refresh.get("content") or "")
        marker = "url="
        idx = content.lower().find(marker)
        if idx >= 0:
            redirected = content[idx + len(marker):].strip(" '\";")
            if redirected:
                candidates.append(urljoin(base_url, redirected))

    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        n = normalize_pdf_url(c)
        if not n or n in seen:
            continue
        seen.add(n)
        unique.append(n)
    return unique


class DiscoveryWriter:
    """Append-only NDJSON writer with session dedup and resume support.

    Accepts either a filesystem path (string) for backward compatibility or a
    file-like object (stream) to decouple storage (useful in cloud mode).
    """

    def __init__(self, output: Union[str, Path, IO]):
        self._stream: Optional[IO] = None
        self._seen = set()
        if hasattr(output, "write") and callable(getattr(output, "write")):
            # file-like stream provided — do not attempt to read existing file
            self._stream = output
            self.output_path = None
        else:
            self.output_path = Path(str(output))
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._load_existing()

    def _load_existing(self) -> None:
        if not self.output_path or not self.output_path.exists():
            return
        try:
            with self.output_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    key = normalize_pdf_url(str(obj.get("pdf_url") or ""))
                    if key:
                        self._seen.add(key)
        except Exception:
            return

    def has_seen(self, pdf_url: str) -> bool:
        return normalize_pdf_url(pdf_url) in self._seen

    def write(self, record: Dict[str, Any]) -> bool:
        key = normalize_pdf_url(str(record.get("pdf_url") or ""))
        if not key:
            return False
        if key in self._seen:
            return False
        record = dict(record)
        record["pdf_url"] = key
        if "discovered_at" not in record:
            record["discovered_at"] = utc_now_iso()
        if self._stream is not None:
            try:
                self._stream.write(json.dumps(record, ensure_ascii=False) + os.linesep)
            except Exception:
                return False
        else:
            with self.output_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + os.linesep)
        self._seen.add(key)
        return True

    @property
    def seen_count(self) -> int:
        return len(self._seen)
