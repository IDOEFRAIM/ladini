"""
Synchronous DB helpers (migrated from `core/db.py`).

This module is the infrastructure implementation for synchronous DB
access. It intentionally mirrors the previous `agriconnect.core.db`
implementation so callers can migrate to `agriconnect.infrastructure.database`.
"""

from __future__ import annotations

import os
import time
import logging
from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from agriconnect.core.settings import settings
from agriconnect.core.cloud_settings import get_settings as get_cloud_settings

logger = logging.getLogger(__name__)

_SYNC_ENGINE: Engine | None = None
_SYNC_DB_URL: str = ""
_SYNC_SESSION_FACTORY: sessionmaker[Session] | None = None


def _app_env() -> str:
	return (os.getenv("APP_ENV") or os.getenv("ENV") or "dev").strip().lower()


def _is_non_local_env() -> bool:
	return _app_env() in {"prod", "production", "staging"}


def resolve_database_url(required: bool = True) -> str:
	"""Resolve DATABASE_URL using unified precedence.

	Order:
	1) settings.DATABASE_URL
	2) cloud_settings.resolve_database_url() (Airflow/Secrets aware)
	"""
	if settings.DATABASE_URL:
		return settings.DATABASE_URL

	try:
		resolved = (get_cloud_settings().resolve_database_url() or "").strip()
	except Exception:
		resolved = ""

	if resolved:
		return resolved

	if required:
		raise RuntimeError("DATABASE_URL not resolved from settings/env/Airflow/Secrets")
	return ""


def _resolve_ssl_connect_args(db_url: str) -> Dict[str, Any]:
	# If sslmode is already in the URL, honor it as source of truth.
	if "sslmode=" in db_url:
		return {}

	mode = (getattr(settings, "DB_SSL_MODE", "verify-full") or "verify-full").strip().lower()
	if mode == "disable":
		return {"sslmode": "disable"}

	if mode == "require":
		return {"sslmode": "require"}

	if mode != "verify-full":
		raise RuntimeError(f"Unsupported DB_SSL_MODE={mode!r}. Expected verify-full|require|disable")

	ca_path_raw = (getattr(settings, "DB_CA_PATH", "") or "").strip()
	if not ca_path_raw:
		if _is_non_local_env():
			raise RuntimeError("DB_CA_PATH missing while DB_SSL_MODE=verify-full in non-local environment")
		logger.warning("DB_CA_PATH missing in local/dev; falling back to sslmode=require")
		return {"sslmode": "require"}

	ca_path = Path(ca_path_raw)
	if not ca_path.is_absolute():
		ca_path = (Path(settings.BASE_DIR) / ca_path).resolve()

	if not ca_path.exists():
		if _is_non_local_env():
			raise RuntimeError(f"DB_CA_PATH does not exist: {ca_path}")
		logger.warning("DB_CA_PATH not found in local/dev; falling back to sslmode=require")
		return {"sslmode": "require"}

	return {
		"sslmode": "verify-full",
		"sslrootcert": str(ca_path),
	}


def get_engine(db_url: Optional[str] = None) -> Engine:
	"""Return the singleton sync SQLAlchemy engine.

	This is the only approved entrypoint for sync DB access.
	"""
	global _SYNC_ENGINE, _SYNC_DB_URL, _SYNC_SESSION_FACTORY

	resolved = (db_url or "").strip() or resolve_database_url(required=True)
	if _SYNC_ENGINE is not None and _SYNC_DB_URL == resolved:
		return _SYNC_ENGINE

	pool_size = int(os.getenv("DB_POOL_SIZE", "10") or "10")
	max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10") or "10")
	pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "1800") or "1800")
	connect_timeout = int(os.getenv("DB_CONNECT_TIMEOUT", "15") or "15")

	# Normalize legacy postgres URI alias for SQLAlchemy.
	if resolved.startswith("postgres://"):
		resolved = "postgresql://" + resolved[len("postgres://"):]

	def _is_url_format(value: str) -> bool:
		return "://" in value

	if not _is_url_format(resolved) and "=" not in resolved:
		raise RuntimeError(
			"DATABASE_URL invalid: expected a SQLAlchemy URI (postgresql://...) "
			"or a DSN string (host=... dbname=... user=... password=...)."
		)

	if _is_url_format(resolved):
		connect_args = _resolve_ssl_connect_args(resolved)
		if "connect_timeout" not in connect_args:
			connect_args["connect_timeout"] = connect_timeout

		_SYNC_ENGINE = create_engine(
			resolved,
			pool_pre_ping=True,
			pool_size=pool_size,
			max_overflow=max_overflow,
			pool_recycle=pool_recycle,
			connect_args=connect_args,
		)
	else:
		# Support DSN-style DATABASE_URL (e.g. "host=... dbname=... user=...")
		# via a creator function; this keeps compatibility with existing .env formats.
		import psycopg2

		def _creator():
			return psycopg2.connect(resolved, connect_timeout=connect_timeout)

		_SYNC_ENGINE = create_engine(
			"postgresql+psycopg2://",
			creator=_creator,
			pool_pre_ping=True,
			pool_size=pool_size,
			max_overflow=max_overflow,
			pool_recycle=pool_recycle,
		)
	_SYNC_DB_URL = resolved
	_SYNC_SESSION_FACTORY = sessionmaker(bind=_SYNC_ENGINE, autoflush=False, autocommit=False)
	return _SYNC_ENGINE


@contextmanager
def session_scope(db_url: Optional[str] = None) -> Generator[Session, None, None]:
	"""Transactional context manager for synchronous code paths."""
	global _SYNC_SESSION_FACTORY
	if _SYNC_SESSION_FACTORY is None:
		get_engine(db_url=db_url)
	assert _SYNC_SESSION_FACTORY is not None
	session = _SYNC_SESSION_FACTORY()
	try:
		yield session
		session.commit()
	except Exception:
		session.rollback()
		raise
	finally:
		session.close()


def wait_for_db_ready(retries: int = 3, delay_s: float = 1.5, db_url: Optional[str] = None) -> bool:
	"""Wait until DB accepts queries through the unified engine."""
	engine = get_engine(db_url=db_url)
	last_err = ""
	for attempt in range(max(0, retries) + 1):
		try:
			with engine.connect() as conn:
				conn.execute(text("SELECT 1"))
			return True
		except Exception as exc:
			last_err = str(exc)
			if attempt < retries:
				time.sleep(max(0.0, delay_s))
	logger.error("wait_for_db_ready failed after retries: %s", last_err)
	return False


def aggressive_db_healthcheck(db_url: Optional[str] = None) -> Dict[str, Any]:
	"""Deep checks: connectivity, UUID function, pgvector extension, and write temp privilege."""
	engine = get_engine(db_url=db_url)
	report: Dict[str, Any] = {
		"ok": False,
		"connectivity": False,
		"gen_random_uuid": False,
		"pgvector": False,
		"write_probe": False,
		"errors": [],
	}

	try:
		with engine.begin() as conn:
			conn.execute(text("SELECT 1"))
			report["connectivity"] = True

			has_uuid = conn.execute(text("SELECT to_regproc('gen_random_uuid') IS NOT NULL")).scalar()
			report["gen_random_uuid"] = bool(has_uuid)
			if not report["gen_random_uuid"]:
				report["errors"].append("gen_random_uuid() unavailable")

			has_vector = conn.execute(
				text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
			).scalar()
			report["pgvector"] = bool(has_vector)
			if not report["pgvector"]:
				report["errors"].append("pgvector extension unavailable")

			conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS _agri_health_probe(v INT) ON COMMIT DROP"))
			conn.execute(text("INSERT INTO _agri_health_probe(v) VALUES (1)"))
			report["write_probe"] = True
	except Exception as exc:
		report["errors"].append(str(exc))

	report["ok"] = bool(
		report["connectivity"]
		and report["gen_random_uuid"]
		and report["pgvector"]
		and report["write_probe"]
	)
	return report


def ensure_ingestion_schema(db_url: Optional[str] = None) -> None:
	"""Create ingestion operational tables if they do not exist.

	This helper centralizes ingestion DDL in the database module so workers
	can remain pure consumers/producers (SELECT/INSERT only).
	"""
	engine = get_engine(db_url=db_url)
	ddl = [
		"CREATE SCHEMA IF NOT EXISTS ingestion",
		"""
		CREATE TABLE IF NOT EXISTS ingestion.worker_processed_objects (
			marker TEXT PRIMARY KEY,
			bucket TEXT NOT NULL,
			object_key TEXT NOT NULL,
			etag TEXT,
			last_modified TIMESTAMPTZ,
			processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		""",
		"CREATE INDEX IF NOT EXISTS idx_worker_processed_bucket_key ON ingestion.worker_processed_objects (bucket, object_key)",
		"""
		CREATE TABLE IF NOT EXISTS ingestion.ingested_documents (
			s3_key TEXT PRIMARY KEY,
			file_hash TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'processed',
			chunk_count INTEGER NOT NULL DEFAULT 0,
			last_processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			metadata JSONB NOT NULL DEFAULT '{}'::jsonb
		)
		""",
		"""
		CREATE TABLE IF NOT EXISTS ingestion.document_chunks (
			id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
			s3_key TEXT NOT NULL,
			file_hash TEXT NOT NULL,
			chunk_index INTEGER NOT NULL,
			content TEXT NOT NULL,
			metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
			source_chunk_id TEXT,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			UNIQUE (s3_key, file_hash, chunk_index)
		)
		""",
		"CREATE INDEX IF NOT EXISTS idx_document_chunks_s3_key ON ingestion.document_chunks (s3_key)",
		"CREATE INDEX IF NOT EXISTS idx_document_chunks_file_hash ON ingestion.document_chunks (file_hash)",
	]
	with engine.begin() as conn:
		for statement in ddl:
			conn.execute(text(statement))


@asynccontextmanager
async def get_async_db():
	"""Unified async DB accessor for MCP/FastAPI via existing async core stack."""
	from agriconnect.core.database import init_db, get_db

	init_db()
	async with get_db() as session:
		yield session
