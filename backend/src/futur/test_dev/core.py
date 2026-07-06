"""
Protocol Core — Shared primitives for A2A, MCP, and AG-UI protocols.
=====================================================================

Defines:
  - TraceEnvelope   : structured decision trace attached to every message
  - TraceStep       : single reasoning step within a trace
  - CorrelationCtx  : propagates correlation_id across protocol boundaries
  - AsyncResult     : immediate ACK returned by async channel operations
  - HandshakeFSM    : finite-state-machine for handshake lifecycle
  - CacheFreshness  : semantic cache invalidation policy

These primitives are **import-safe** (no heavy dependencies) so every
protocol sub-package can import them at module level.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Protocol.Core")


# ═══════════════════════════════════════════════════════════════
# CORRELATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class CorrelationCtx:
    """Propagated across every protocol call for end-to-end tracing."""

    correlation_id: str = ""
    parent_id: str = ""          # parent span / message id
    session_id: str = ""         # user conversation session
    user_id: str = ""
    originated_at: str = ""      # ISO timestamp of first creation

    def __post_init__(self):
        if not self.correlation_id:
            self.correlation_id = uuid.uuid4().hex[:16]
        if not self.originated_at:
            self.originated_at = datetime.now(timezone.utc).isoformat()

    def child(self, parent_id: str = "") -> "CorrelationCtx":
        """Create a child context preserving the correlation chain."""
        return CorrelationCtx(
            correlation_id=self.correlation_id,
            parent_id=parent_id or self.correlation_id,
            session_id=self.session_id,
            user_id=self.user_id,
            originated_at=self.originated_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════
# TRACE ENVELOPE
# ═══════════════════════════════════════════════════════════════

class TraceCategory(str, Enum):
    DISCOVERY = "discovery"
    ROUTING = "routing"
    MCP_CONTEXT = "mcp_context"
    MCP_RAG = "mcp_rag"
    MCP_WEATHER = "mcp_weather"
    AGENT_REASONING = "agent_reasoning"
    HANDSHAKE = "handshake"
    RENDERING = "rendering"
    CACHE = "cache"
    SECURITY = "security"


@dataclass
class TraceStep:
    """Single reasoning / decision step inside a trace."""

    category: TraceCategory
    module: str                        # e.g. "A2ADiscovery", "MCPRagServer"
    action: str                        # e.g. "discover_candidates", "hyde_generate"
    input_summary: Dict[str, Any] = field(default_factory=dict)
    output_summary: Dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""                # human-readable explanation
    duration_ms: float = 0.0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d


@dataclass
class TraceEnvelope:
    """
    Structured decision trace attached to every A2AMessage.

    Every module (Discovery, MCP, Agent) **appends** its reasoning here.
    After the full pipeline, the envelope is persisted to
    ``protocol_trace_log`` for monitoring / audit.
    """

    trace_id: str = ""
    correlation: CorrelationCtx = field(default_factory=CorrelationCtx)
    steps: List[TraceStep] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""
    status: str = "in_progress"      # in_progress | completed | error

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    # ── Mutation helpers ─────────────────────────────────────
    def add_step(self, step: TraceStep) -> None:
        self.steps.append(step)

    def record(
        self,
        category: TraceCategory,
        module: str,
        action: str,
        *,
        input_summary: Dict[str, Any] | None = None,
        output_summary: Dict[str, Any] | None = None,
        reasoning: str = "",
        duration_ms: float = 0.0,
    ) -> TraceStep:
        """Convenience: build + append a TraceStep in one call."""
        step = TraceStep(
            category=category,
            module=module,
            action=action,
            input_summary=input_summary or {},
            output_summary=output_summary or {},
            reasoning=reasoning,
            duration_ms=duration_ms,
        )
        self.steps.append(step)
        return step

    def complete(self, status: str = "completed") -> None:
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.status = status

    # ── Serialization ────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "correlation": self.correlation.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraceEnvelope":
        corr = CorrelationCtx(**data.get("correlation", {}))
        steps = [
            TraceStep(
                category=TraceCategory(s["category"]),
                module=s["module"],
                action=s["action"],
                input_summary=s.get("input_summary", {}),
                output_summary=s.get("output_summary", {}),
                reasoning=s.get("reasoning", ""),
                duration_ms=s.get("duration_ms", 0.0),
                timestamp=s.get("timestamp", ""),
            )
            for s in data.get("steps", [])
        ]
        return cls(
            trace_id=data.get("trace_id", ""),
            correlation=corr,
            steps=steps,
            created_at=data.get("created_at", ""),
            completed_at=data.get("completed_at", ""),
            status=data.get("status", "in_progress"),
        )


# ═══════════════════════════════════════════════════════════════
# ASYNC RESULT (ACK pattern)
# ═══════════════════════════════════════════════════════════════

class AckStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    QUEUED = "queued"


@dataclass
class AsyncResult:
    """
    Immediately returned by the async channel upon ``send()``.
    The actual processing happens in a background worker.
    """

    correlation_id: str
    message_id: str
    ack_status: AckStatus = AckStatus.ACCEPTED
    queue_position: int = 0
    estimated_ms: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ack_status"] = self.ack_status.value
        return d


# ═══════════════════════════════════════════════════════════════
# HANDSHAKE FSM
# ═══════════════════════════════════════════════════════════════

class HSState(str, Enum):
    """Handshake Finite-State-Machine states."""
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COUNTER = "counter_offer"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


# Valid transitions: (from_state) → set of allowed to_states
_HS_TRANSITIONS: Dict[HSState, set] = {
    HSState.PROPOSED:  {HSState.ACCEPTED, HSState.REJECTED, HSState.COUNTER, HSState.TIMED_OUT, HSState.CANCELLED},
    HSState.COUNTER:   {HSState.ACCEPTED, HSState.REJECTED, HSState.COUNTER, HSState.TIMED_OUT, HSState.CANCELLED},
    HSState.ACCEPTED:  {HSState.COMPLETED, HSState.CANCELLED},
    HSState.REJECTED:  set(),   # terminal
    HSState.COMPLETED: set(),   # terminal
    HSState.TIMED_OUT: set(),   # terminal
    HSState.CANCELLED: set(),   # terminal
}


class HandshakeFSMError(Exception):
    """Raised on invalid FSM transitions."""


@dataclass
class HandshakeRecord:
    """Persistent handshake state (maps to ``protocol_handshake_states`` table)."""

    handshake_id: str
    initiator_id: str
    responder_id: str
    intent: str
    current_state: HSState = HSState.PROPOSED
    turns: int = 0
    max_turns: int = 5
    timeout_at: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = datetime.now(timezone.utc)
        if not self.handshake_id:
            self.handshake_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = now.isoformat()
        if not self.updated_at:
            self.updated_at = now.isoformat()
        if not self.timeout_at:
            self.timeout_at = (now + timedelta(seconds=300)).isoformat()

    @property
    def is_terminal(self) -> bool:
        return self.current_state in (HSState.REJECTED, HSState.COMPLETED, HSState.TIMED_OUT, HSState.CANCELLED)

    def transition(self, to_state: HSState, actor_id: str, payload: Dict[str, Any] | None = None) -> None:
        """
        Advance the FSM. Raises HandshakeFSMError on illegal transitions.
        """
        # Check timeout first
        now = datetime.now(timezone.utc)
        if self.timeout_at and now > datetime.fromisoformat(self.timeout_at):
            self.current_state = HSState.TIMED_OUT
            self.updated_at = now.isoformat()
            raise HandshakeFSMError(
                f"Handshake {self.handshake_id} timed out."
            )

        allowed = _HS_TRANSITIONS.get(self.current_state, set())
        if to_state not in allowed:
            raise HandshakeFSMError(
                f"Invalid transition {self.current_state.value} → {to_state.value} "
                f"for handshake {self.handshake_id}"
            )

        if self.turns >= self.max_turns and to_state == HSState.COUNTER:
            raise HandshakeFSMError(
                f"Max turns ({self.max_turns}) reached for handshake {self.handshake_id}"
            )

        self.history.append({
            "from": self.current_state.value,
            "to": to_state.value,
            "actor": actor_id,
            "payload": payload or {},
            "timestamp": now.isoformat(),
        })
        self.current_state = to_state
        self.turns += 1
        self.updated_at = now.isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["current_state"] = self.current_state.value
        return d


# ═══════════════════════════════════════════════════════════════
# CACHE FRESHNESS / SEMANTIC INVALIDATION
# ═══════════════════════════════════════════════════════════════

# Keywords that force cache bypass (emergency, disease, urgent…)
CACHE_BYPASS_KEYWORDS: set[str] = {
    "urgence", "urgent", "emergency", "maladie", "disease",
    "inondation", "flood", "criquet", "locust", "invasion",
    "famine", "sécheresse", "drought", "alerte", "alert",
    "danger", "mort", "dead", "dying", "mourir",
    "épidémie", "epidemic", "contamination",
}


@dataclass
class CachePolicy:
    """Per-entry cache metadata for semantic invalidation."""
    key: str
    ttl_seconds: int = 300          # 5 min default
    created_at: str = ""
    bypass_keywords: set[str] = field(default_factory=lambda: CACHE_BYPASS_KEYWORDS)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_expired(self) -> bool:
        created = datetime.fromisoformat(self.created_at)
        return datetime.now(timezone.utc) > created + timedelta(seconds=self.ttl_seconds)

    def should_bypass(self, text: str) -> bool:
        """Return True if *text* contains high-priority keywords → skip cache."""
        lower = text.lower()
        return any(kw in lower for kw in self.bypass_keywords)

    def should_bypass_payload(self, payload: Dict[str, Any]) -> bool:
        """Check payload dict values (recursive shallow) for bypass keywords."""
        for v in payload.values():
            if isinstance(v, str) and self.should_bypass(v):
                return True
            if isinstance(v, dict):
                if self.should_bypass_payload(v):
                    return True
        # Also check if payload has image/attachment (force rebuild)
        if payload.get("image") or payload.get("attachment") or payload.get("photo"):
            return True
        return False


# ═══════════════════════════════════════════════════════════════
# CLIENT CAPABILITIES (AG-UI negotiation)
# ═══════════════════════════════════════════════════════════════

@dataclass
class ClientCapabilities:
    """
    Constraints manifest provided by the UI channel **before** the agent
    generates a response. The agent MUST use these to prune its output.
    """

    channel: str = "web"                    # whatsapp | web | sms | ussd | mobile
    max_chars: int = 0                      # 0 = unlimited
    max_buttons: int = 10                   # WhatsApp limit = 3
    max_list_items: int = 50                # WhatsApp = 10
    supports_images: bool = True
    supports_cards: bool = True
    supports_charts: bool = True
    supports_voice: bool = False
    supports_markdown: bool = True
    supports_interactive: bool = True       # buttons / list pickers
    locale: str = "fr"
    text_format: str = "markdown"           # plain | markdown | html

    @classmethod
    def whatsapp(cls) -> "ClientCapabilities":
        return cls(
            channel="whatsapp", max_chars=4096, max_buttons=3,
            max_list_items=10, supports_charts=False,
            supports_markdown=False, text_format="plain",
            supports_voice=True,
        )

    @classmethod
    def sms(cls) -> "ClientCapabilities":
        return cls(
            channel="sms", max_chars=160, max_buttons=0,
            max_list_items=0, supports_images=False,
            supports_cards=False, supports_charts=False,
            supports_interactive=False, supports_markdown=False,
            text_format="plain",
        )

    @classmethod
    def ussd(cls) -> "ClientCapabilities":
        return cls(
            channel="ussd", max_chars=182, max_buttons=0,
            max_list_items=9, supports_images=False,
            supports_cards=False, supports_charts=False,
            supports_markdown=False, text_format="plain",
        )

    @classmethod
    def web(cls) -> "ClientCapabilities":
        return cls(channel="web")

    @classmethod
    def mobile(cls) -> "ClientCapabilities":
        return cls(channel="mobile", supports_voice=True)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
