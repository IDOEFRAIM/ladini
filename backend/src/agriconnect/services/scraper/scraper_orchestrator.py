from __future__ import annotations

"""YAML-driven scraper orchestrator (zero-IO).

This orchestrator centralizes source strategy in a YAML file while reusing
registered scraper engines from `SCRAPER_REGISTRY`.
"""

from pathlib import Path
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Literal
import logging

from pydantic import BaseModel, Field

from agriconnect.core.schemas import RawDocument, ScraperLog
from .scrapers.registry import ScraperRegistry

logger = logging.getLogger(__name__)


class SourceRunResult(BaseModel):
    """Typed contract for a single source execution result."""

    status: Literal["SUCCESS", "ERROR"]
    source_id: str
    error: Optional[str] = None
    document: Optional[RawDocument] = None
    log: Optional[ScraperLog | Dict[str, Any]] = None


class OrchestratorRunResult(BaseModel):
    """Typed contract for a batch execution summary."""

    status: Literal["SUCCESS", "PARTIAL_SUCCESS"]
    total_sources: int
    success_count: int
    failure_count: int
    results: List[SourceRunResult] = Field(default_factory=list)


class ScraperOrchestrator:
    """Central orchestrator that executes sources declared in YAML."""

    ENGINE_ALIASES = {
        "institutional_pdf": "institutional_pdf",
        "news_article": "news_article",
        "technical_crawler": "technical_crawler",
        "technical_site": "technical_site",
        "news": "news",
        "technical": "technical",
    }

    def __init__(
        self,
        sources_file: Optional[str] = None,
        auto_discover: bool = True,
        headless: bool = True,
        strict_init: bool = False,
    ):
        _ = headless  # kept for backward compatibility with previous orchestrator signature
        self.sources_file = Path(sources_file) if sources_file else self._default_sources_file()
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.init_error: Optional[str] = None

        if auto_discover:
            # Ensure decorators are loaded before create() calls.
            try:
                ScraperRegistry.discover("agriconnect.services.scraper.scrapers")
            except Exception as exc:
                msg = f"Failed to discover scrapers: {exc}"
                logger.exception(msg)
                if strict_init:
                    raise RuntimeError(msg) from exc
                self.init_error = msg

        try:
            self.sources = self._load_sources(self.sources_file)
        except Exception as exc:
            msg = f"Failed to initialize sources from '{self.sources_file}': {exc}"
            logger.exception(msg)
            if strict_init:
                raise RuntimeError(msg) from exc
            self.init_error = msg
            self.sources = {}

        # Disabled sources can be configured via env var `SCRAPER_DISABLED_SOURCES`
        # as a comma-separated list of source ids. These sources will be skipped
        # during `run_all()` without changing the YAML config.
        raw_disabled = os.getenv("SCRAPER_DISABLED_SOURCES", "") or ""
        self.disabled_sources = {s.strip() for s in raw_disabled.split(',') if s.strip()}

    @staticmethod
    def _default_sources_file() -> Path:
        # backend/src/agriconnect/services/scraper/scraper_orchestrator.py -> backend/sources/sources.yaml
        return Path(__file__).resolve().parents[4] / "sources" / "sources.yaml"

    @staticmethod
    def _read_yaml(path: Path) -> Dict[str, Any]:
        try:
            import yaml  # type: ignore
        except Exception as exc:
            raise RuntimeError("PyYAML is required to load sources.yaml") from exc

        with path.open("r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh) or {}
        if not isinstance(payload, dict):
            raise ValueError("sources.yaml root must be a mapping")
        return payload

    @staticmethod
    def _normalize_config_url(source_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize legacy URL keys into one explicit config.url key."""
        cfg = dict(config or {})
        if cfg.get("url"):
            return cfg

        legacy_candidates: List[str] = []

        start_url = cfg.get("start_url")
        if start_url:
            legacy_candidates.append(str(start_url))

        search_urls = cfg.get("search_urls")
        if isinstance(search_urls, list) and search_urls:
            legacy_candidates.append(str(search_urls[0]))

        base_url = cfg.get("base_url")
        if base_url:
            legacy_candidates.append(str(base_url))

        if legacy_candidates:
            cfg["url"] = legacy_candidates[0]
            logger.warning(
                "Source '%s' uses legacy URL keys; normalized to config.url",
                source_id,
            )
        return cfg

    def _load_sources(self, path: Path) -> Dict[str, Dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"Sources config not found: {path}")

        payload = self._read_yaml(path)
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            raise ValueError("sources.yaml must define a top-level 'sources' list")

        loaded: Dict[str, Dict[str, Any]] = {}
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or "").strip()
            engine = str(item.get("engine") or "").strip()
            if not source_id or not engine:
                continue

            raw_config = dict(item.get("config") or {})
            config = self._normalize_config_url(source_id, raw_config)
            if not config.get("url"):
                logger.warning("Skipping source '%s': missing required config.url", source_id)
                continue

            loaded[source_id] = {
                "id": source_id,
                "engine": engine,
                "priority": int(item.get("priority", 3)),
                "frequency": str(item.get("frequency", "manual")),
                "tags": list(item.get("tags") or []),
                "config": config,
            }

        if not loaded:
            raise ValueError("No valid sources found in sources.yaml")

        return loaded

    @classmethod
    def _registry_engine_key(cls, engine_name: str) -> str:
        normalized = (engine_name or "").strip().lower()
        return cls.ENGINE_ALIASES.get(normalized, normalized)

    @staticmethod
    def _enrich_document(doc: RawDocument, source: Dict[str, Any]) -> RawDocument:
        meta = dict(doc.metadata or {})
        meta["source_id"] = source["id"]
        meta["source_engine"] = source["engine"]
        meta["source_priority"] = int(source.get("priority", 3))
        meta["source_frequency"] = source.get("frequency")
        meta["source_tags"] = list(source.get("tags") or [])
        meta["collected_at"] = datetime.now(timezone.utc).isoformat()
        doc.metadata = meta
        return doc

    def list_sources(self) -> List[str]:
        return sorted(self.sources.keys())

    def run_source(self, source_id: str) -> SourceRunResult:
        source = self.sources.get(source_id)
        if source is None:
            return SourceRunResult(status="ERROR", source_id=source_id, error="source_not_found")

        try:
            engine_key = self._registry_engine_key(source["engine"])
            scraper = ScraperRegistry.create(engine_key, config=source.get("config") or {})
            target_url = str((source.get("config") or {}).get("url") or "").strip()
            if not target_url:
                raise ValueError(f"Source '{source_id}' is missing required config.url")

            doc, log = scraper.run(target_url)
            if doc is None:
                error_trace = log.error_trace if hasattr(log, "error_trace") else None
                return SourceRunResult(
                    status="ERROR",
                    source_id=source_id,
                    error=error_trace or "scrape_failed",
                    log=log,
                )

            doc = self._enrich_document(doc, source)
            return SourceRunResult(
                status="SUCCESS",
                source_id=source_id,
                error=None,
                document=doc,
                log=log,
            )
        except Exception as exc:
            logger.exception("Source '%s' failed", source_id)
            return SourceRunResult(status="ERROR", source_id=source_id, error=str(exc))

    def run_all(self) -> Iterator[SourceRunResult]:
        """Stream source execution results one-by-one to minimize memory usage."""
        for source_id in self.list_sources():
            # Skip explicitly disabled sources (operator control)
            if source_id in getattr(self, 'disabled_sources', set()):
                logger.info("Skipping disabled source: %s", source_id)
                continue
            # Do not fail-fast: one failing source must not block the rest.
            yield self.run_source(source_id)

    def run_all_summary(self) -> OrchestratorRunResult:
        """Optional helper for callers that still need a full batch summary."""
        results: List[SourceRunResult] = list(self.run_all())
        success_count = sum(1 for r in results if r.status == "SUCCESS")
        failure_count = len(results) - success_count
        return OrchestratorRunResult(
            status="SUCCESS" if failure_count == 0 else "PARTIAL_SUCCESS",
            total_sources=len(results),
            success_count=success_count,
            failure_count=failure_count,
            results=results,
        )
