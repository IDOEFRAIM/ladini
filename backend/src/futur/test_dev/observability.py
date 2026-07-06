"""
Protocol Observability — Structured Decision Tracing & Persistence.
===================================================================

Provides ``ProtocolTracer``, the single entry-point every protocol
module uses to record *why* an agent made a decision.

Storage backends
----------------
- **Database** (default): persists to ``protocol_trace_log`` table
  via the same session_factory used by the rest of AgriConnect.
- **In-Memory**: lightweight fallback for tests / offline mode.

Usage::

    tracer = ProtocolTracer(session_factory=get_session)

    # Start a trace (usually at channel.send time)
    envelope = tracer.start_trace(correlation)

    # Any module appends steps
    envelope.record(
        TraceCategory.DISCOVERY,
        "A2ADiscovery",
        "find_agents",
        input_summary={"intent": "CHECK_PRICE", "zone": "BOBO"},
        output_summary={"candidates": 3, "winner": "market_agent"},
        reasoning="market_agent selected: lowest avg_response_ms (210ms)",
    )

    # At end of pipeline
    tracer.complete_trace(envelope)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

from .core import (
    CorrelationCtx,
    TraceEnvelope,
    TraceStep,
    TraceCategory,
)

logger = logging.getLogger("Protocol.Observability")


class ProtocolTracer:
    """
    Central tracing service for all protocol modules.

    Responsibilities:
      1. Create/complete trace envelopes
      2. Persist completed traces to the DB (``protocol_trace_log``)
      3. Query past traces for monitoring dashboards
    """

    def __init__(self, session_factory: Optional[Callable] = None):
        self._session_factory = session_factory
        # In-memory buffer for offline / test mode
        self._buffer: List[Dict[str, Any]] = []

    # ── Lifecycle ────────────────────────────────────────────

    def start_trace(self, correlation: Optional[CorrelationCtx] = None) -> TraceEnvelope:
        """Create a fresh TraceEnvelope linked to a correlation context."""
        return TraceEnvelope(correlation=correlation or CorrelationCtx())

    def complete_trace(self, envelope: TraceEnvelope) -> None:
        """Mark envelope as completed and persist."""
        envelope.complete()
        self._persist(envelope)

    def fail_trace(self, envelope: TraceEnvelope, error: str = "") -> None:
        """Mark envelope as error and persist."""
        envelope.complete(status="error")
        if error:
            envelope.record(
                TraceCategory.AGENT_REASONING,
                "ProtocolTracer",
                "trace_error",
                reasoning=error,
            )
        self._persist(envelope)

    # ── Persistence ──────────────────────────────────────────

    def _persist(self, envelope: TraceEnvelope) -> None:
        """Write the trace to DB; fall back to in-memory buffer."""
        data = envelope.to_dict()
        if self._session_factory:
            try:
                self._persist_to_db(data)
                return
            except Exception as exc:
                logger.warning("DB trace persist failed, buffering: %s", exc)
        # Fallback: in-memory
        self._buffer.append(data)
        logger.debug("Trace %s buffered in-memory (%d total)", envelope.trace_id, len(self._buffer))

    def _persist_to_db(self, data: Dict[str, Any]) -> None:
        """Insert into ``protocol_trace_log`` table."""
        session = self._session_factory()
        try:
            session.execute(
                _INSERT_TRACE_SQL,
                {
                    "trace_id": data["trace_id"],
                    "correlation_id": data["correlation"]["correlation_id"],
                    "session_id": data["correlation"].get("session_id", ""),
                    "user_id": data["correlation"].get("user_id", ""),
                    "steps_json": json.dumps(data["steps"], ensure_ascii=False, default=str),
                    "status": data["status"],
                    "created_at": data["created_at"],
                    "completed_at": data.get("completed_at", ""),
                },
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── Query API ────────────────────────────────────────────

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single trace by ID (DB first, then buffer)."""
        # Try DB
        if self._session_factory:
            try:
                return self._query_trace_db(trace_id)
            except Exception:
                pass
        # Fallback buffer
        for item in self._buffer:
            if item.get("trace_id") == trace_id:
                return item
        return None

    def list_traces(
        self,
        correlation_id: str = "",
        user_id: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List recent traces, optionally filtered."""
        if self._session_factory:
            try:
                return self._list_traces_db(correlation_id, user_id, limit)
            except Exception:
                pass
        # Fallback: filter buffer
        results = self._buffer
        if correlation_id:
            results = [t for t in results if t.get("correlation", {}).get("correlation_id") == correlation_id]
        if user_id:
            results = [t for t in results if t.get("correlation", {}).get("user_id") == user_id]
        return results[-limit:]

    def get_buffered_traces(self) -> List[Dict[str, Any]]:
        """Return all in-memory buffered traces (for tests)."""
        return list(self._buffer)

    def flush_buffer(self) -> int:
        """Attempt to persist buffered traces to DB. Returns count flushed."""
        if not self._session_factory:
            return 0
        flushed = 0
        remaining = []
        for data in self._buffer:
            try:
                self._persist_to_db(data)
                flushed += 1
            except Exception:
                remaining.append(data)
        self._buffer = remaining
        if flushed:
            logger.info("Flushed %d buffered traces to DB", flushed)
        return flushed

    # ── DB helpers ───────────────────────────────────────────

    def _query_trace_db(self, trace_id: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text
        session = self._session_factory()
        try:
            row = session.execute(
                text("SELECT * FROM protocol_trace_log WHERE trace_id = :tid"),
                {"tid": trace_id},
            ).mappings().first()
            if row:
                return self._row_to_dict(row)
            return None
        finally:
            session.close()

    def _list_traces_db(self, correlation_id: str, user_id: str, limit: int) -> List[Dict[str, Any]]:
        from sqlalchemy import text
        session = self._session_factory()
        try:
            clauses = ["1=1"]
            params: Dict[str, Any] = {"lim": limit}
            if correlation_id:
                clauses.append("correlation_id = :cid")
                params["cid"] = correlation_id
            if user_id:
                clauses.append("user_id = :uid")
                params["uid"] = user_id

            where = " AND ".join(clauses)
            rows = session.execute(
                text(f"SELECT * FROM protocol_trace_log WHERE {where} ORDER BY created_at DESC LIMIT :lim"),
                params,
            ).mappings().all()
            return [self._row_to_dict(r) for r in rows]
        finally:
            session.close()

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        return {
            "trace_id": row["trace_id"],
            "correlation_id": row["correlation_id"],
            "session_id": row.get("session_id", ""),
            "user_id": row.get("user_id", ""),
            "steps": json.loads(row["steps_json"]) if row.get("steps_json") else [],
            "status": row["status"],
            "created_at": str(row.get("created_at", "")),
            "completed_at": str(row.get("completed_at", "")),
        }


# ── Raw SQL (works with any SQLAlchemy session) ─────────────
from sqlalchemy import text as _sa_text

_INSERT_TRACE_SQL = _sa_text("""
    INSERT INTO protocol_trace_log
        (trace_id, correlation_id, session_id, user_id, steps_json, status, created_at, completed_at)
    VALUES
        (:trace_id, :correlation_id, :session_id, :user_id, :steps_json::jsonb, :status,
         :created_at::timestamptz, NULLIF(:completed_at, '')::timestamptz)
    ON CONFLICT (trace_id) DO UPDATE SET
        steps_json = EXCLUDED.steps_json,
        status     = EXCLUDED.status,
        completed_at = EXCLUDED.completed_at
""")
