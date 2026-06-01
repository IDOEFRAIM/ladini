from __future__ import annotations

"""YAML-driven scraper orchestrator (zero-IO).

This orchestrator centralizes source strategy in a YAML file while reusing
registered scraper engines from `SCRAPER_REGISTRY`.
"""

from pathlib import Path
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Literal, Set, Callable, IO
import logging
import json
from collections import Counter

from pydantic import BaseModel, Field
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import JSONB

from agriconnect.core.schemas import RawDocument, ScraperLog
from agriconnect.core.scraper_utils import canonicalize_url
from agriconnect.core.db import get_engine, resolve_database_url
from .scrapers.pdf_discovery import is_pdf_candidate
from .scrapers.registry import ScraperRegistry

logger = logging.getLogger(__name__)

class SourceRunResult(BaseModel):
    """Typed contract for a single source execution result."""

    status: Literal["SUCCESS", "ERROR"]
    source_id: str
    error: Optional[str] = None
    document: Optional[RawDocument] = None
    log: Optional[ScraperLog | Dict[str, Any]] = None
    discovered_count: int = 0


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
        discovery_dir: Optional[str] = None,
        s3_bucket: Optional[str] = None,
        s3_prefix: Optional[str] = None,
        discovery_stream: Optional[IO[str]] = None,
        discovery_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
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

        # Discovery centralization (local folder + optional S3 prefix)
        if discovery_dir:
            self.discovery_dir = Path(discovery_dir)
        else:
            # default: project_root/data/discovery
            self.discovery_dir = Path(__file__).resolve().parents[4] / "data" / "discovery"
        self.discovery_dir.mkdir(parents=True, exist_ok=True)
        batch_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.discovery_pending_file = self.discovery_dir / f"discovery_{batch_ts}.ndjson"
        self.discovery_stream = discovery_stream
        self.discovery_callback = discovery_callback
        # Optional S3 configuration
        self.s3_bucket = s3_bucket or os.getenv("DISCOVERY_S3_BUCKET")
        self.s3_prefix = s3_prefix or os.getenv("DISCOVERY_S3_PREFIX", "discovery/pending/")

        self.state_engine = None
        resolved_db_url = resolve_database_url(required=False)
        if resolved_db_url:
            try:
                self.state_engine = get_engine(resolved_db_url)
            except Exception:
                logger.exception("Failed to initialize DB engine for discovery queue mode")

        # In-memory dedupe set (loaded from existing discovery files)
        self._seen_urls: Set[str] = set()
        try:
            self._load_existing_discoveries()
        except Exception:
            # Non-fatal: continue with empty seen set
            logger.debug("No existing discovery lines loaded (or failed to load)")

        # Disabled sources can be configured via env var `SCRAPER_DISABLED_SOURCES`
        # as a comma-separated list of source ids. These sources will be skipped
        # during `run_all()` without changing the YAML config.
        raw_disabled = os.getenv("SCRAPER_DISABLED_SOURCES", "") or ""
        self.disabled_sources = {s.strip() for s in raw_disabled.split(',') if s.strip()}

        # Internal telemetry used for pipeline/audit mode.
        self.stats: Dict[str, Dict[str, Any]] = {}

    def _reset_stats(self) -> None:
        self.stats = {}

    def _ensure_stats_bucket(self, scraper_type: str) -> Dict[str, Any]:
        key = (scraper_type or "unknown").strip() or "unknown"
        bucket = self.stats.get(key)
        if bucket is None:
            bucket = {
                "discovery_found": 0,
                "ingestion_success": 0,
                "ingestion_failure": 0,
                "failure_reasons": Counter(),
                "latency_sum_ms": 0.0,
                "latency_count": 0,
            }
            self.stats[key] = bucket
        return bucket

    def _record_event(
        self,
        *,
        scraper_type: str,
        event: str,
        reason: Optional[str] = None,
        latency_ms: Optional[float] = None,
    ) -> None:
        bucket = self._ensure_stats_bucket(scraper_type)
        if event == "discovery_found":
            bucket["discovery_found"] += 1
        elif event == "ingestion_success":
            bucket["ingestion_success"] += 1
        elif event == "ingestion_failure":
            bucket["ingestion_failure"] += 1
            if reason:
                bucket["failure_reasons"][reason] += 1

        if latency_ms is not None:
            bucket["latency_sum_ms"] += float(latency_ms)
            bucket["latency_count"] += 1

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
    def _merge_source_config(item: Dict[str, Any]) -> Dict[str, Any]:
        """Merge legacy config with extended YAML sections.

        Runtime scrapers receive a single config dict that can include:
        - url (required)
        - engine_config
        - selectors
        - network_params
        - api_config
        - processing
        """
        merged = dict(item.get("config") or {})
        for section in ("engine_config", "selectors", "network_params", "api_config", "processing"):
            value = item.get(section)
            if isinstance(value, dict):
                merged[section] = value

        # Promote common networking keys when only declared inside network_params.
        net = merged.get("network_params") if isinstance(merged.get("network_params"), dict) else {}
        if isinstance(net, dict):
            for key in ("timeout", "min_delay_s", "max_delay_s", "max_attempts", "respect_robots", "user_agents", "user_agent"):
                if key in net and key not in merged:
                    merged[key] = net[key]

        # Allow API-driven sources to omit config.url and rely on api endpoint.
        api_cfg = merged.get("api_config") if isinstance(merged.get("api_config"), dict) else {}
        if not merged.get("url") and isinstance(api_cfg, dict):
            endpoint = api_cfg.get("endpoint")
            if endpoint:
                merged["url"] = str(endpoint)

        return merged

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

            raw_config = self._merge_source_config(item)
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

    def _normalize_discovery_url(self, url: str) -> str:
        # Canonical normalization for strict dedup across sources and runs
        return canonicalize_url(url)

    def _load_existing_discoveries(self) -> None:
        """Scan existing NDJSON discovery files to populate the seen-URLs set.

        This helps deduplicate across orchestrator runs and restarts.
        """
        if not self.discovery_dir.exists():
            return
        for p in self.discovery_dir.glob("discovery_*.ndjson"):
            try:
                with p.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            j = json.loads(line)
                            url = j.get("url") or j.get("document", {}).get("url")
                            if url:
                                self._seen_urls.add(self._normalize_discovery_url(str(url)))
                        except Exception:
                            continue
            except Exception:
                continue

    def _append_to_pending(self, payload: Dict[str, Any]) -> None:
        """Append a single JSON object as a line to the pending NDJSON file."""
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        if self.discovery_stream is not None:
            self.discovery_stream.write(line)
            return
        with self.discovery_pending_file.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _enqueue_discovery_payload(self, payload: Dict[str, Any]) -> bool:
        """Insert one discovered URL into SQL queue (deduplicated by url+source)."""
        if self.state_engine is None:
            self._append_to_pending(payload)
            return True

        q = text(
            """
            INSERT INTO ingestion.discovery_queue (url_pdf, source_id, discovered_at, status, metadata, updated_at)
            VALUES (:url_pdf, :source_id, :discovered_at, 'pending', :metadata, NOW())
            ON CONFLICT (url_pdf, source_id) DO NOTHING
            """
        ).bindparams(bindparam("metadata", type_=JSONB))
        params = {
            "url_pdf": str(payload.get("url") or "").strip(),
            "source_id": str(payload.get("source_id") or "unknown"),
            "discovered_at": payload.get("discovered_at"),
            "metadata": dict(payload.get("metadata") or {}),
        }
        with self.state_engine.begin() as conn:
            res = conn.execute(q, params)
        return bool(res.rowcount and res.rowcount > 0)

    def _resolve_discovery_mapping_path(self, mapping_path: str) -> Path:
        p = Path(mapping_path)
        if p.is_absolute():
            return p
        repo_root = Path(__file__).resolve().parents[5]
        return (repo_root / p).resolve()

    def _iter_mapping_pdf_records(self, doc: RawDocument) -> Iterator[Dict[str, Any]]:
        """Yield discovered PDF records from a scraper mapping file when available.

        Expected NDJSON shape in mapping files:
        {
          "pdf_url": "https://...pdf",
          "parent_page_url": "https://...",
          "context": {...},
          "metadata": {...}
        }
        """
        meta = dict(doc.metadata or {})
        mapping_path = str(meta.get("discovery_output_path") or "").strip()
        if not mapping_path:
            return
        path = self._resolve_discovery_mapping_path(mapping_path)
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    pdf_url = str(rec.get("pdf_url") or "").strip()
                    if not pdf_url:
                        continue
                    yield rec
        except Exception:
            logger.exception("Failed to read discovery mapping file: %s", path)

    def _emit_discovery_url(
        self,
        source: Dict[str, Any],
        url: str,
        title: Optional[str],
        doc_id: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
        parent_page_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not url:
            return None

        emitted_url = str(url)
        norm = self._normalize_discovery_url(emitted_url)
        if norm in self._seen_urls:
            logger.debug("Deduplicated discovery URL: %s", url)
            return None

        payload = {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "source_id": source.get("id"),
            "source_engine": source.get("engine"),
            "url": emitted_url,
            "title": title,
            "id": doc_id,
            "parent_page_url": parent_page_url,
            "metadata": {
                **dict(metadata or {}),
                "discovery_contract": "candidate_pdf_url",
                "validation_delegated_to": "ingestion.pdf_downloader",
            },
        }

        # Optional correlation id for supervised runs (audit-friendly).
        scout_run_id = (os.getenv("SCOUT_RUN_ID") or os.getenv("SCRAPER_RUN_ID") or "").strip()
        if scout_run_id:
            try:
                payload["metadata"]["scout_run_id"] = scout_run_id
            except Exception:
                pass
        try:
            inserted = self._enqueue_discovery_payload(payload)
            self._seen_urls.add(norm)
            if inserted:
                self._record_event(scraper_type=str(source.get("engine") or "unknown"), event="discovery_found")
            else:
                logger.debug("Discovery already queued (deduplicated at DB level): %s", emitted_url)
        except Exception:
            logger.exception("Failed to write discovery payload for %s", url)
            return None
        return payload

    def get_audit_report(self) -> Dict[str, Any]:
        per_scraper: Dict[str, Any] = {}
        for scraper_type, bucket in self.stats.items():
            discovered = int(bucket.get("discovery_found", 0))
            success = int(bucket.get("ingestion_success", 0))
            failure = int(bucket.get("ingestion_failure", 0))
            total = success + failure
            success_rate = (float(success) / float(discovered) * 100.0) if discovered > 0 else 0.0
            latency_count = int(bucket.get("latency_count", 0))
            avg_latency_ms = (float(bucket.get("latency_sum_ms", 0.0)) / float(latency_count)) if latency_count > 0 else 0.0

            reasons_counter = bucket.get("failure_reasons")
            if isinstance(reasons_counter, Counter):
                top_failures = [{"reason": r, "count": c} for r, c in reasons_counter.most_common()]
            else:
                top_failures = []

            per_scraper[scraper_type] = {
                "urls_discovered": discovered,
                "pdfs_ingested_success": success,
                "ingestion_failures": failure,
                "success_rate_percent": round(success_rate, 2),
                "top_failure_reasons": top_failures,
                "avg_latency_ms_per_document": round(avg_latency_ms, 2),
                "processed_documents": total,
            }

        # Build global aggregation
        total_discovered = 0
        total_success = 0
        total_failures = 0
        total_latency_ms = 0.0
        total_latency_count = 0
        aggregated_reasons: Counter = Counter()

        for bucket in self.stats.values():
            total_discovered += int(bucket.get("discovery_found", 0))
            total_success += int(bucket.get("ingestion_success", 0))
            total_failures += int(bucket.get("ingestion_failure", 0))
            total_latency_ms += float(bucket.get("latency_sum_ms", 0.0))
            total_latency_count += int(bucket.get("latency_count", 0))
            br = bucket.get("failure_reasons")
            if isinstance(br, Counter):
                aggregated_reasons.update(br)

        global_success_rate = (float(total_success) / float(total_discovered) * 100.0) if total_discovered > 0 else 0.0
        avg_latency_ms_global = (total_latency_ms / float(total_latency_count)) if total_latency_count > 0 else 0.0

        top_global_failures = [{"reason": r, "count": c} for r, c in aggregated_reasons.most_common()]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scrapers": per_scraper,
            "_global_summary": {
                "total_urls_discovered": total_discovered,
                "total_successes": total_success,
                "total_failures": total_failures,
                "global_success_rate_percent": round(global_success_rate, 2),
                "total_latency_ms": round(total_latency_ms, 2),
                "avg_latency_ms_per_document": round(avg_latency_ms_global, 2),
                "total_processed_documents": total_success + total_failures,
                "top_failure_reasons": top_global_failures,
            },
        }

    def _maybe_emit_discovery(self, doc: RawDocument, source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Emit a discovery NDJSON line for a RawDocument if not already seen.

        Returns the payload written or None if deduplicated.
        """
        url = str(getattr(doc, "url", "") or doc.metadata.get("url", ""))
        if not is_pdf_candidate(url):
            meta = dict(doc.metadata or {})
            meta["parent_page"] = url
            meta["parent_page_reason"] = "non_pdf_candidate_url"
            doc.metadata = meta
            logger.info(
                "Discovery fallback skipped non-candidate URL for source '%s': %s",
                source.get("id"),
                url,
            )
            return None

        return self._emit_discovery_url(
            source=source,
            url=url,
            title=getattr(doc, "title", None),
            doc_id=getattr(doc, "id", None),
            metadata=dict(doc.metadata or {}),
            parent_page_url=None,
        )

    def _upload_pending_to_s3(self) -> None:
        """Upload the current pending NDJSON file to S3 under the configured prefix.

        This is best-effort and non-atomic; intended for infra that will consume the file.
        """
        if not self.s3_bucket:
            return
        try:
            import boto3
        except Exception:
            logger.debug("boto3 not installed; skipping S3 upload")
            return

        s3 = boto3.client("s3")
        key = f"{self.s3_prefix.rstrip('/')}/{self.discovery_pending_file.name}"
        with self.discovery_pending_file.open("rb") as fh:
            s3.upload_fileobj(fh, self.s3_bucket, key)

    def get_discovery_output_file(self) -> Path:
        """Return the active discovery batch NDJSON path for this run."""
        return self.discovery_pending_file

    def list_sources(self) -> List[str]:
        return sorted(self.sources.keys())

    def run_source(
        self,
        source_id: str,
        ingestion_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> SourceRunResult:
        _ = ingestion_callback
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
            # Emit granular discovery lines (one per discovered PDF when mapping exists),
            # otherwise fallback to one line for the document URL.
            try:
                emitted = 0
                for rec in self._iter_mapping_pdf_records(doc):
                    pdf_url = str(rec.get("pdf_url") or "").strip()
                    if not pdf_url:
                        continue
                    context = rec.get("context") if isinstance(rec.get("context"), dict) else {}
                    rec_meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
                    payload = self._emit_discovery_url(
                        source=source,
                        url=pdf_url,
                        title=(context.get("anchor_text") or context.get("page_title") or getattr(doc, "title", None)),
                        doc_id=getattr(doc, "id", None),
                        metadata={
                            **dict(doc.metadata or {}),
                            **rec_meta,
                            "surrounding_text": context.get("surrounding_text"),
                            "anchor_text": context.get("anchor_text"),
                        },
                        parent_page_url=rec.get("parent_page_url"),
                    )
                    if payload is not None:
                        emitted += 1

                if emitted == 0:
                    payload = self._maybe_emit_discovery(doc, source)
                    emitted = 1 if payload is not None else 0

                doc.metadata = dict(doc.metadata or {})
                doc.metadata["discovery_emitted_count"] = emitted
            except Exception:
                logger.exception("Error while emitting discovery payload (continuing)")

            return SourceRunResult(
                status="SUCCESS",
                source_id=source_id,
                error=None,
                document=doc,
                log=log,
                discovered_count=emitted,
            )
        except Exception as exc:
            logger.exception("Source '%s' failed", source_id)
            return SourceRunResult(status="ERROR", source_id=source_id, error=str(exc))

    def run_all(
        self,
        ingestion_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        audit_output_path: Optional[str] = None,
        log_audit_summary: bool = True,
    ) -> Iterator[SourceRunResult]:
        """Stream source execution results one-by-one to minimize memory usage.

        If `audit_output_path` is provided, the final audit (from
        `get_audit_report()`) will be written as formatted JSON to that file.
        If `audit_output_path` is None and `log_audit_summary` is True, a
        single-line structured JSON audit will be emitted to the standard
        logger at INFO level when the run completes.
        """
        self._reset_stats()
        run_start = time.perf_counter()
        try:
            for source_id in self.list_sources():
                # Skip explicitly disabled sources (operator control)
                if source_id in getattr(self, 'disabled_sources', set()):
                    logger.info("Skipping disabled source: %s", source_id)
                    continue
                # Do not fail-fast: one failing source must not block the rest.
                yield self.run_source(source_id, ingestion_callback=ingestion_callback)
        finally:
            # Export audit after the generator is fully consumed (or on error)
            run_end = time.perf_counter()
            run_duration_s = run_end - run_start
            report = self.get_audit_report()
            # Attach run timing to global summary if present
            if isinstance(report, dict):
                gs = report.setdefault("_global_summary", {})
                gs.setdefault("run_duration_seconds", round(run_duration_s, 3))

            # Persist to file when requested
            if audit_output_path:
                try:
                    p = Path(audit_output_path)
                    if not p.parent.exists():
                        p.parent.mkdir(parents=True, exist_ok=True)
                    with p.open("w", encoding="utf-8") as fh:
                        json.dump(report, fh, ensure_ascii=False, indent=2)
                    logger.info("Audit written to %s", str(p))
                except Exception:
                    logger.exception("Failed to write audit to %s", audit_output_path)
            elif log_audit_summary:
                try:
                    # Single-line structured JSON for easy ingestion by log collectors
                    logger.info("audit_report: %s", json.dumps(report, ensure_ascii=False, separators=(",",":")))
                except Exception:
                    logger.exception("Failed to emit audit summary to logs")

    def run_all_summary(
        self,
        ingestion_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> OrchestratorRunResult:
        """Optional helper for callers that still need a full batch summary."""
        results: List[SourceRunResult] = list(self.run_all(ingestion_callback=ingestion_callback))
        success_count = sum(1 for r in results if r.status == "SUCCESS")
        failure_count = len(results) - success_count
        return OrchestratorRunResult(
            status="SUCCESS" if failure_count == 0 else "PARTIAL_SUCCESS",
            total_sources=len(results),
            success_count=success_count,
            failure_count=failure_count,
            results=results,
        )
