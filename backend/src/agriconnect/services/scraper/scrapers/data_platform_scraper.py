from __future__ import annotations

"""
Data Platform Scraper (V2.1)

Scraper specialized for data catalogs (CKAN, Socrata, INSD, FAO).
Extracts structured metadata (JSON-LD), resource download links,
and converts dataset description pages into Markdown.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from agriconnect.core.schemas import RawDocument
from agriconnect.core.scraper_utils import extract_markdown_from_html

from .base import BaseScraper
from .registry import register_scraper


@register_scraper("data_platform", "dataset_catalog", "statistics")
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
        if "microdata.insd.bf" in low_url:
            return "insd_microdata"
        if "fews.net" in low_url and "data" in low_url:
            return "fews_data"
        if "fao.org" in low_url and ("data" in low_url or "survey" in low_url):
            return "fao_data"
        if "data.gov" in low_url:
            return "data_gov"
        
        return "generic_catalog"

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

    def scrape(self, url: str) -> Tuple[Optional[RawDocument], Dict]:
        """Main stateless scraping method."""
        response = self._request(url)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # Extraction du titre (priorité H1 de contenu)
        title_elem = soup.select_one("h1, .dataset-title, .entry-title") or soup.select_one("title")
        title = title_elem.get_text(strip=True) if title_elem else "Data Platform Page"

        platform = self.identify_platform(url, soup=soup)
        
        # Sélecteurs optimisés pour capturer les tables de métadonnées (Data Dictionary)
        markdown_source_html = self._prepare_markdown_html(
            html,
            base_url=url,
            preferred_selectors="main, #content, .dataset-details, .dataset-view, .wrapper-content, article",
        )
        
        markdown = extract_markdown_from_html(markdown_source_html, include_links=True)
        quality = self._assess_extraction_quality(markdown, html)

        if quality.get("blocked_by_waf") or not markdown.strip():
            return None, {
                "http_status": response.status_code,
                "bytes_downloaded": len(response.content or b""),
                "raw_payload": html,
                "blocked_by_waf": bool(quality.get("blocked_by_waf")),
            }

        data_links = self._extract_data_links(soup, url)
        structured_metadata = self._extract_structured_metadata(soup)

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