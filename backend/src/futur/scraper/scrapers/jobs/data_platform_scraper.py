from __future__ import annotations

"""
Data Platform Scraper (discovery-only)
ladini@2026Efra
Specialized for data catalogs (CKAN, Socrata, INSD, FAO).
- Extracts structured metadata (JSON-LD) and resource download URLs.
- Converts dataset pages into Markdown for indexing.

Important: this scraper is discovery-only: it emits resource URLs and
metadata but does NOT download or validate binary resources. The ingestion
pipeline is responsible for fetching and validating files.
"""

import json
import re
import requests
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from ..shared.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class DataPlatformScraper(BaseScraper):
    """V2.1 data-platform scraper (catalog metadata + markdown, zero-IO)."""

    name = "data_platform_scraper"
    version = "2.1.0"

    # Extended mapping for file format detection
    FORMAT_MAPPING = {
        ".csv": "CSV",
        ".xlsx": "Excel",
        ".xls": "Excel",
        ".json": "JSON",
        ".xml": "XML",
        ".zip": "ZIP",
        ".geojson": "GeoJSON",
        ".dta": "Stata",
        ".sav": "SPSS",
        ".rds": "R Data",
        ".pdf": "PDF",
    }

    @staticmethod
    def identify_platform(url: str, soup: Optional[BeautifulSoup] = None) -> str:
        """Identify the data platform via HTML signatures or the URL."""
        if soup is not None:
            # Vérification via la balise generator
            generator_meta = soup.select_one('meta[name="generator" i]')
            if generator_meta:
                val = (generator_meta.get("content") or "").strip().lower()
                for platform in ["ckan", "socrata", "dkan", "junar", "arcgis"]:
                    if platform in val:
                        return platform

            # Vérification via les classes CSS communes ou meta tags spécifiques
            html_content = str(soup).lower()
            if "ckan-" in html_content or "dataset-resources" in html_content:
                return "ckan"
            if "socrata" in html_content:
                return "socrata"

        low_url = url.lower()
        if "data.gov" in low_url:
            return "data_gov"

        return "generic_catalog"

    @staticmethod
    def _dig_payload(payload: Dict[str, Any], path: str) -> Any:
        """Traverse nested dictionaries/lists using a dotted path.

        Supports simple list indices via dot notation, e.g. result.rows.0.title.
        """
        current: Any = payload
        for token in [p for p in str(path or "").split(".") if p]:
            if isinstance(current, dict):
                current = current.get(token)
                continue
            if isinstance(current, list):
                try:
                    idx = int(token)
                except Exception:
                    return None
                if idx < 0 or idx >= len(current):
                    return None
                current = current[idx]
                continue
            return None
        return current

    def _extract_structured_metadata(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """
        Extract JSON-LD blocks (Dataset, DataCatalog).
        Note: normalize into lists to avoid data overwrite when @graph is present.
        """
        results: Dict[str, List[Dict[str, Any]]] = {
            "datasets": [],
            "catalogs": [],
            "organizations": [],
            "raw_jsonld": []
        }

        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            raw = script.string or script.get_text() or ""
            # Nettoyage des commentaires JS qui cassent le parser JSON
            raw = re.sub(r"//.*?(?=\n|$)", "", raw)
            raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
            raw = raw.strip()
            
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except Exception:
                continue

            # Normalisation en liste pour traitement uniforme
            items = []
            if isinstance(payload, dict):
                if "@graph" in payload and isinstance(payload["@graph"], list):
                    items.extend(payload["@graph"])
                items.append(payload)
            elif isinstance(payload, list):
                items.extend(payload)

            for item in items:
                if not isinstance(item, dict):
                    continue
                
                t = item.get("@type", "")
                types = set(t) if isinstance(t, list) else {t}
                
                if "Dataset" in types:
                    results["datasets"].append(item)
                elif "DataCatalog" in types:
                    results["catalogs"].append(item)
                elif "Organization" in types:
                    results["organizations"].append(item)
                
            results["raw_jsonld"].append(payload)

        # Nettoyage des entrées vides
        return {k: v for k, v in results.items() if v}

    def _guess_file_format(self, url: str, anchor: Optional[Any] = None) -> str:
        """Determine file format (URL > attributes > CSS class)."""
        url_l = url.lower()
        
        # 1. Vérification par extension d'URL
        for ext, label in self.FORMAT_MAPPING.items():
            if ext in url_l:
                return label

        # 2. Vérification via attributs HTML (spécifique CKAN/DKAN)
        if anchor:
            for attr in ["data-format", "data_format", "data-type", "type"]:
                val = str(anchor.get(attr) or "").strip().lower()
                for ext, label in self.FORMAT_MAPPING.items():
                    if ext.strip(".") in val:
                        return label
            
            # 3. Vérification via les classes CSS
            classes = " ".join(anchor.get("class", [])).lower()
            for ext, label in self.FORMAT_MAPPING.items():
                if f"format-{ext.strip('.')}" in classes:
                    return label

        return "Unknown"

    def _extract_data_links(self, soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
        """Extract data download links by filtering noise."""
        # Focus on content areas to avoid menus
        content_area = soup.select_one(
            "main, #content, .dataset-resources, .resource-list, .downloads, .entry-content"
        ) or soup

        links_by_url: Dict[str, Dict[str, str]] = {}
        # Action words used to detect download links (includes English and French variants)
        action_words = {"download", "telecharger", "télécharger", "export", "resource", "données", "data", "downloadable"}

        for anchor in content_area.select("a[href]"):
            href = anchor.get("href") or ""
            if not href or href.startswith(("#", "javascript:")):
                continue
                
            absolute = urljoin(page_url, href)
            # Avoid loops when the link points to the page itself
            if absolute.rstrip("/") == page_url.rstrip("/"):
                continue

            text = (anchor.get_text(strip=True) or "").lower()
            fmt = self._guess_file_format(absolute, anchor)
            
            # Critères de pertinence
            is_data_fmt = fmt != "Unknown"
            is_action = any(word in text for word in action_words)
            has_download_attr = anchor.has_attr("download")

            if not (is_data_fmt or is_action or has_download_attr):
                continue

            # Construction du label
            label = anchor.get_text(strip=True) or anchor.get("title") or anchor.get("aria-label")
            if not label:
                img = anchor.find("img")
                label = img.get("alt") if img else ""
            
            if not label:
                label = urlparse(absolute).path.split("/")[-1] or "Resource"

            links_by_url[absolute] = {
                "url": absolute,
                "label": label[:120].strip(),
                "format": fmt,
            }

        return list(links_by_url.values())

    @staticmethod
    def _build_fallback_markdown(
        *,
        page_url: str,
        title: str,
        soup: BeautifulSoup,
        platform: str,
        data_links: List[Dict[str, str]],
        structured_metadata: Dict[str, Any],
    ) -> str:
        """Build a minimal markdown when HTML-to-markdown extraction is empty.

        Some catalogs are JS-heavy and expose little readable body text server-side.
        In that case, keep the scrape successful with concise metadata content.
        """
        lines: List[str] = [f"# {title or 'Data Platform Page'}", ""]
        lines.append(f"- URL: {page_url}")
        lines.append(f"- Platform: {platform}")

        meta_desc = soup.select_one('meta[name="description" i], meta[property="og:description" i]')
        description = (meta_desc.get("content") if meta_desc else "") or ""
        description = str(description).strip()
        if description:
            lines.extend(["", "## Description", description])

        datasets = structured_metadata.get("datasets") if isinstance(structured_metadata, dict) else None
        if isinstance(datasets, list) and datasets:
            lines.extend(["", "## Structured Datasets"])
            for idx, item in enumerate(datasets[:10], start=1):
                if not isinstance(item, dict):
                    continue
                ds_title = item.get("name") or item.get("headline") or f"Dataset {idx}"
                ds_desc = item.get("description")
                lines.append(f"- {ds_title}")
                if ds_desc and str(ds_desc).strip():
                    lines.append(f"  - {str(ds_desc).strip()[:240]}")

        if data_links:
            lines.extend(["", "## Data Links"])
            for link in data_links[:50]:
                if not isinstance(link, dict):
                    continue
                label = str(link.get("label") or "Resource").strip()
                href = str(link.get("url") or "").strip()
                fmt = str(link.get("format") or "Unknown").strip()
                if not href:
                    continue
                lines.append(f"- [{label}]({href}) ({fmt})")

        return "\n".join(lines).strip()

    @staticmethod
    def _json_to_markdown_catalog(payload: Dict[str, Any], api_config: Optional[Dict[str, Any]] = None) -> str:
        """Convert common catalog JSON payloads into concise markdown.

        Supports structures like:
        - {"result": {"rows": [...]}}
        - {"rows": [...]} or plain list payloads (normalized by caller)
        """
        cfg = api_config or {}
        rows: List[Dict[str, Any]] = []
        payload_path = str(cfg.get("payload_path") or "result.rows")
        if isinstance(payload, dict):
            selected = DataPlatformScraper._dig_payload(payload, payload_path)
            if isinstance(selected, list):
                rows = [r for r in selected if isinstance(r, dict)]
            elif isinstance(selected, dict):
                rows = [selected]
            elif isinstance(payload.get("rows"), list):
                rows = [r for r in payload.get("rows", []) if isinstance(r, dict)]

        if not rows:
            return ""

        field_mapping = cfg.get("field_mapping") if isinstance(cfg.get("field_mapping"), dict) else {}
        def pick(row: Dict[str, Any], key: str, defaults: List[str]) -> str:
            aliases = field_mapping.get(key) if isinstance(field_mapping.get(key), list) else defaults
            for alias in aliases:
                value = row.get(alias)
                if value is not None and str(value).strip():
                    return str(value).strip()
            return ""

        rows_limit = int(cfg.get("rows_limit", 25))
        lines: List[str] = [str(cfg.get("title") or "# Dataset Catalog"), ""]
        for idx, row in enumerate(rows[:rows_limit], start=1):
            title = pick(row, "title", ["title", "name"]) or f"Dataset {idx}"
            dataset_id = pick(row, "id", ["idno", "id"])
            nation = pick(row, "country", ["nation", "country"])
            author = pick(row, "author", ["authoring_entity", "organization"])
            year = pick(row, "year", ["year_start", "year"])
            abstract = pick(row, "summary", ["abstract", "subtitle", "description"])

            lines.append(f"## {idx}. {title}")
            if dataset_id:
                lines.append(f"- ID: {dataset_id}")
            if nation:
                lines.append(f"- Country: {nation}")
            if author:
                lines.append(f"- Source: {author}")
            if year:
                lines.append(f"- Year: {year}")
            if abstract:
                lines.append(f"- Summary: {abstract}")
            lines.append("")

        return "\n".join(lines).strip()

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        """Main stateless scraping method."""
        selectors = self.config.get("selectors") if isinstance(self.config.get("selectors"), dict) else {}
        api_config = self.config.get("api_config") if isinstance(self.config.get("api_config"), dict) else {}
        
        # Discovery / safety config
        self.discovery_output_path = str(self.config.get("discovery_output_path") or "data/pdf_discovery/data_platform_discovery.ndjson")

        response = self._request(url)
        html = response.text
        content_type = str(response.headers.get("Content-Type") or "").lower()
        preferred_selectors = selectors.get("content") or selectors.get("body") or "main, #content, .dataset-details, .dataset-view, .wrapper-content, article"
        json_force = bool(api_config.get("expect_json", False))
        json_payload_path = str(api_config.get("payload_path") or "result.rows")

        # JS-heavy portals often expose JSON catalog endpoints. Handle them directly.
        if json_force or "application/json" in content_type or html.lstrip().startswith("{"):
            try:
                payload = response.json()
            except Exception:
                payload = {}

            markdown = self._json_to_markdown_catalog(payload if isinstance(payload, dict) else {}, api_config=api_config)
            if markdown.strip():
                rows_obj = self._dig_payload(payload, json_payload_path) if isinstance(payload, dict) else []
                rows_count = len(rows_obj) if isinstance(rows_obj, list) else 0
                doc = self._build_document(
                    url=url,
                    title=str(api_config.get("doc_title") or "Dataset Catalog API"),
                    markdown=markdown,
                    metadata={
                        "source_type": "data_platform",
                        "platform": "api_catalog",
                        "source_domain": self._source_domain(url),
                        "publication_date": None,
                        "language": None,
                        "extraction_method": "json_catalog_to_markdown",
                        "rows_count": rows_count,
                    },
                    language=None,
                )
                return doc, {
                    "http_status": response.status_code,
                    "bytes_downloaded": len(response.content or b""),
                    "raw_payload": json.dumps(payload, ensure_ascii=False)[:10000],
                    "platform_detected": "api_catalog",
                }
            # Additionally: convert JSON rows into discovery NDJSON records (best-effort)
            try:
                from .pdf_discovery import DiscoveryWriter, is_pdf_candidate, normalize_pdf_url
                writer = DiscoveryWriter(self.discovery_output_path)
                rows_obj = self._dig_payload(payload, json_payload_path) if isinstance(payload, dict) else []
                if isinstance(rows_obj, list):
                    for row in rows_obj:
                        if not isinstance(row, dict):
                            continue
                        parent = row.get("url") or row.get("identifier") or None
                        title = row.get("title") or row.get("name") or None
                        # candidate urls in common fields
                        candidates = set()
                        u = row.get("url")
                        if u:
                            candidates.add(u)
                        # some catalog APIs include a `resources` list
                        for r in row.get("resources") or []:
                            if isinstance(r, dict):
                                for k in ("url", "download_url", "resource_url"):
                                    v = r.get(k)
                                    if v:
                                        candidates.add(v)
                        # also check fields like `file`, `link`
                        for k in ("file", "link", "href"):
                            v = row.get(k)
                            if v:
                                candidates.add(v)

                        for cand in candidates:
                            if not cand:
                                continue
                            if not is_pdf_candidate(cand) and not cand.lower().endswith('.pdf'):
                                continue
                            rec = {
                                "pdf_url": normalize_pdf_url(cand),
                                "parent_page_url": parent or None,
                                "context": {"page_title": title},
                                "metadata": {"discovery_method": "api_catalog", "dataset_row": row.get("id") or row.get("idno")},
                            }
                            try:
                                writer.write(rec)
                            except Exception as e:
                                logger.debug("Failed to write discovery record for %s: %s", cand, e)
                                continue
            except Exception as e:
                # best-effort only; don't fail the scraping if discovery write fails
                logger.debug("Discovery NDJSON conversion failed: %s", e)

        soup = BeautifulSoup(html, "html.parser")

        # Extraction du titre (priorité H1 de contenu)
        title_selectors = selectors.get("title") or ["h1", ".dataset-title", ".entry-title", "title"]
        title_elem = None
        if isinstance(title_selectors, list):
            for sel in title_selectors:
                title_elem = soup.select_one(str(sel))
                if title_elem is not None:
                    break
        else:
            title_elem = soup.select_one(str(title_selectors))
        if title_elem is None:
            title_elem = soup.select_one("title")
        title = title_elem.get_text(strip=True) if title_elem else "Data Platform Page"

        platform = self.identify_platform(url, soup=soup)
        
        # Sélecteurs optimisés pour capturer les tables de métadonnées (Data Dictionary)
        markdown_source_html = self._prepare_markdown_html(
            html,
            base_url=url,
            preferred_selectors=preferred_selectors,
        )
        
        data_links = self._extract_data_links(soup, url)
        structured_metadata = self._extract_structured_metadata(soup)
        markdown = extract_markdown_from_html(markdown_source_html, include_links=True)
        quality = self._assess_extraction_quality(markdown, html)

        if quality.get("blocked_by_waf"):
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "blocked_by_waf": True,
            }

        if not markdown.strip():
            markdown = self._build_fallback_markdown(
                page_url=url,
                title=title,
                soup=soup,
                platform=platform,
                data_links=data_links,
                structured_metadata=structured_metadata,
            )

        if not markdown.strip():
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "blocked_by_waf": False,
                "empty_content": True,
            }

        # Produce discovery NDJSON for PDF candidates
        from .pdf_discovery import DiscoveryWriter, is_pdf_candidate, normalize_pdf_url

        writer = DiscoveryWriter(self.discovery_output_path)
        discovered = 0
        domain = urlparse(url).netloc.lower()

        for link in data_links:
            link_url = str(link.get("url") or "")
            if not link_url:
                continue
            # detect PDF format by mapping or candidate heuristics
            fmt = str(link.get("format") or "").upper()
            if fmt != "PDF" and not is_pdf_candidate(link_url):
                continue
            if writer.has_seen(link_url):
                continue
            normalized_link = normalize_pdf_url(link_url)
            if not normalized_link:
                continue
            record = {
                "pdf_url": normalized_link,
                "parent_page_url": url,
                "context": {
                    "page_title": title,
                    "anchor_text": link.get("label") or None,
                    "surrounding_text": None,
                },
                "metadata": {
                    "site_name": domain,
                    "discovery_method": "data_platform",
                    "extracted_date": None,
                },
                "resolver_meta": {
                    "resolution_method": "catalog_candidate",
                    "resolved_from": url,
                },
            }
            if writer.write(record):
                discovered += 1

        doc = self._build_document(
            url=url,
            title=title,
            markdown=markdown,
            metadata={
                "source_type": "data_platform",
                "platform": platform,
                "source_domain": self._source_domain(url),
                "publication_date": None, # Ideally extracted from structured_metadata later
                "language": self._extract_language_code(soup),
                "extraction_method": "html_to_markdown",
                "data_links": data_links,
                "structured_metadata": structured_metadata,
                "partial_extraction": bool(quality.get("partial_extraction")),
            },
            language=self._extract_language_code(soup),
        )

        return doc, {
            "http_status": response.status_code,
            "bytes_downloaded": len(response.content or b""),
            "raw_payload": html,
            "platform_detected": platform,
        }


def _cli_main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run DataPlatformScraper on a single URL and emit discovery NDJSON.")
    parser.add_argument("url", help="URL to scrape")
    parser.add_argument("--discovery-output", dest="discovery_output", default=None,
                        help="Path to discovery NDJSON output (overrides scraper config)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = {}
    if args.discovery_output:
        cfg["discovery_output_path"] = args.discovery_output

    scraper = DataPlatformScraper(config=cfg)

    doc, meta = scraper.scrape(args.url)
    # Print a brief summary
    if doc is None:
        print(f"Scrape failed: meta={meta}")
        return 1
    print(f"Title: {doc.title}")
    print(f"URL: {doc.url}")
    print(f"Discovery output: {getattr(scraper, 'discovery_output_path', None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())