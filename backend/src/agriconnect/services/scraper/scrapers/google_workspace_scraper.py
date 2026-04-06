from __future__ import annotations

"""
Google Workspace Scraper (V2)

Handles in-memory extraction from Google Docs/Forms/Drive.
Takes public URLs, exports HTML/PDF when available, and returns `RawDocument` + meta.
"""

import re
from typing import Dict, Optional, Tuple

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from .base import BaseScraper
from .pdf_downloader import PdfDownloader
from .registry import register_scraper


@register_scraper("google", "google_workspace", "gdrive", "gdoc", "gform")
class GoogleWorkspaceScraper(BaseScraper):
    """Google Workspace scraper in memory-only mode."""

    name = "google_workspace_scraper"
    version = "2.0.0"

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config=config)
        self.pdf_downloader = PdfDownloader(config=config or {})

    @staticmethod
    def identify_link_type(url: str) -> str:
        low = url.lower()
        if "docs.google.com/forms" in low:
            return "forms"
        if "docs.google.com/document" in low:
            return "docs"
        if "drive.google.com" in low or "share.google" in low:
            return "drive"
        if "docs.google.com/spreadsheets" in low:
            return "sheets"
        if "docs.google.com/presentation" in low:
            return "slides"
        return "unknown"

    @staticmethod
    def extract_file_id(url: str) -> Optional[str]:
        patterns = [
            r"/d/([a-zA-Z0-9-_]+)",
            r"id=([a-zA-Z0-9-_]+)",
            r"/([a-zA-Z0-9-_]+)/edit",
            r"/([a-zA-Z0-9-_]+)/view",
            r"docs\.google\.com/[^/]+/d/([^/]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _is_auth_or_login_page(final_url: str, html: str) -> bool:
        low_url = (final_url or "").lower()
        low_html = (html or "").lower()
        if "servicelogin" in low_url or "accounts.google.com" in low_url:
            return True
        if "identifier" in low_html and "sign in" in low_html and "accounts.google.com" in low_html:
            return True
        if "connexion" in low_html and "compte google" in low_html:
            return True
        return False

    def _scrape_google_doc(self, url: str, file_id: str) -> Tuple[Optional[RawDocument], Dict]:
        export_url = f"https://docs.google.com/document/d/{file_id}/export?format=html"
        response = self._request(export_url)
        html = response.text
        if self._is_auth_or_login_page(response.url, html):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "auth_required": True,
                "reason": "google_login_page_detected",
            }
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else f"Google Doc {file_id}"
        markdown_html = self._prepare_markdown_html(
            html,
            base_url=export_url,
            preferred_selectors="main, article, #content, .kix-page, .doc-content",
        )
        markdown = extract_markdown_from_html(markdown_html, include_links=True)
        quality = self._assess_extraction_quality(markdown, html)
        if quality.get("blocked_by_waf"):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "blocked_by_waf": True,
            }

        # Guard against protected/empty exports that still contain shell text.
        text_chars = len((markdown or "").strip())
        if text_chars < 40:
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "auth_required": self._is_auth_or_login_page(response.url, html),
                "reason": "empty_or_protected_doc_content",
            }

        doc = self._build_document(
            url=url,
            title=title,
            markdown=markdown,
            metadata={
                "source_type": "google_docs",
                "source_domain": self._source_domain(url),
                "publication_date": None,
                "language": self._extract_language_code(soup),
                "file_id": file_id,
                "export_url": export_url,
                "extraction_method": "html_to_markdown",
                "partial_extraction": bool(quality.get("partial_extraction")),
            },
            language=self._extract_language_code(soup),
        )
        return doc, {
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": html,
            "partial_extraction": bool(quality.get("partial_extraction")),
        }

    def _scrape_google_form(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        response = self._request(url)
        html = response.text
        if self._is_auth_or_login_page(response.url, html):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "auth_required": True,
                "reason": "google_login_page_detected",
            }
        soup = BeautifulSoup(html, "html.parser")

        # Prefer resilient selectors over volatile class names.
        title_elem = (
            soup.select_one("[role='heading'][aria-level='1']")
            or soup.select_one("h1")
            or soup.find("div", {"class": "freebirdFormviewerViewHeaderTitle"})
        )
        title = title_elem.get_text(strip=True) if title_elem else "Google Form"

        desc_elem = (
            soup.select_one("[data-params]")
            or soup.find("div", {"class": "freebirdFormviewerViewHeaderDescription"})
        )
        description = desc_elem.get_text(strip=True) if desc_elem else ""

        questions_md = []
        question_divs = soup.find_all("div", attrs={"role": re.compile(r"group|listitem", re.I)})
        if not question_divs:
            question_divs = soup.find_all("div", {"class": re.compile("freebirdFormviewerComponent")})
        for idx, q_div in enumerate(question_divs, start=1):
            q_text_elem = q_div.select_one("[role='heading']") or q_div.find("span")
            if q_text_elem is None:
                continue
            q_text = q_text_elem.get_text(strip=True)
            if not q_text:
                continue
            questions_md.append(f"- Q{idx}: {q_text}")

        markdown = f"# {title}\n\n{description}\n\n## Questions\n" + "\n".join(questions_md)
        doc = self._build_document(
            url=url,
            title=title,
            markdown=markdown,
            metadata={
                "source_type": "google_forms",
                "questions_count": len(questions_md),
            },
            language=None,
        )
        return doc, {
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": html,
        }

    def _scrape_google_sheet(self, url: str, file_id: str) -> Tuple[Optional[RawDocument], Dict]:
        csv_url = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"
        response = self._request(csv_url)
        csv_text = response.text
        if self._is_auth_or_login_page(response.url, csv_text):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": csv_text,
                "auth_required": True,
                "reason": "google_login_page_detected",
            }

        markdown = f"# Google Sheet {file_id}\n\n```csv\n{csv_text}\n```"
        doc = self._build_document(
            url=url,
            title=f"Google Sheet {file_id}",
            markdown=markdown,
            metadata={
                "source_type": "google_sheets",
                "source_domain": self._source_domain(url),
                "publication_date": None,
                "language": None,
                "file_id": file_id,
                "export_url": csv_url,
                "extraction_method": "csv_export",
            },
            language=None,
        )
        return doc, {
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": csv_text,
        }

    def _scrape_google_drive(self, url: str, file_id: str) -> Tuple[Optional[RawDocument], Dict]:
        export_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        response = self._request(export_url)
        if self._is_auth_or_login_page(response.url, response.text if "text" in (response.headers.get("Content-Type") or "").lower() else ""):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": response.text if isinstance(response.text, str) else "",
                "auth_required": True,
                "reason": "google_login_page_detected",
            }
        content_type = (response.headers.get("Content-Type") or "").lower()

        if "pdf" in content_type:
            doc, log = self.pdf_downloader.run(export_url)
            if doc is None:
                return None, {
                    "http_status": response.status_code,
                    "bytes_downloaded": len(response.content or b""),
                    "raw_payload": "",
                }
            wrapped = self._build_document(
                url=url,
                title=doc.title,
                markdown=doc.content_markdown,
                metadata={
                    "source_type": "google_drive",
                    "file_id": file_id,
                    "content_type": content_type,
                    "extraction_method": "pdf",
                },
                language=doc.language,
            )
            return wrapped, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b"") + int(log.bytes_downloaded),
                "raw_payload": "[binary-drive-pdf]",
            }

        html = response.text
        markdown_html = self._prepare_markdown_html(
            html,
            base_url=url,
            preferred_selectors="main, article, #content, .post-content",
        )
        markdown = extract_markdown_from_html(markdown_html, include_links=True)
        quality = self._assess_extraction_quality(markdown, html)
        if quality.get("blocked_by_waf"):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "blocked_by_waf": True,
            }
        if not markdown.strip():
            markdown = "# Google Drive Resource\n\nExtraction limitée sur ce type de contenu."

        language = None
        try:
            language = self._extract_language_code(BeautifulSoup(html, "html.parser"))
        except Exception:
            language = None

        doc = self._build_document(
            url=url,
            title=f"Google Drive {file_id}",
            markdown=markdown,
            metadata={
                "source_type": "google_drive",
                "source_domain": self._source_domain(url),
                "publication_date": None,
                "language": language,
                "file_id": file_id,
                "content_type": content_type,
                "extraction_method": "html_or_text",
                "partial_extraction": bool(quality.get("partial_extraction")),
            },
            language=language,
        )
        return doc, {
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": html if isinstance(html, str) else "",
            "partial_extraction": bool(quality.get("partial_extraction")),
        }

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        link_type = self.identify_link_type(url)
        file_id = self.extract_file_id(url)

        if link_type == "forms":
            return self._scrape_google_form(url)

        if not file_id:
            return None, {"http_status": None, "bytes_downloaded": 0, "raw_payload": ""}

        if link_type == "docs":
            return self._scrape_google_doc(url, file_id)

        if link_type == "sheets":
            return self._scrape_google_sheet(url, file_id)

        # drive/sheets/slides/unknown -> generic drive strategy.
        return self._scrape_google_drive(url, file_id)
