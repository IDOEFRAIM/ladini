"""Protocol Core — Primitives partagées des protocoles (AG-UI, MCP).
====================================================================

Version minimale restaurée (Phase 0) : seuls les types réellement
consommés par le codebase actuel sont conservés.

  - CorrelationCtx     : propagation du correlation_id inter-protocoles
  - TraceCategory      : catégories de trace de décision
  - TraceStep          : étape de raisonnement unitaire d'une trace
  - TraceEnvelope      : trace de décision structurée (audit/monitoring)
  - CachePolicy        : invalidation sémantique de cache (mots-clés urgence)
  - ClientCapabilities : manifeste de contraintes du canal UI (AG-UI)

Consommateurs :
  - protocols/ag_ui/renderer.py   (ClientCapabilities, TraceCategory, TraceEnvelope)
  - infrastructure/mcp/context.py (CachePolicy)

Ces primitives sont import-safe (aucune dépendance lourde).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List

# ═══════════════════════════════════════════════════════════════
# CORRELATION
# ═══════════════════════════════════════════════════════════════


@dataclass
class CorrelationCtx:
    """Propagée à chaque appel protocolaire pour le traçage bout-en-bout."""

    correlation_id: str = ""
    parent_id: str = ""  # parent span / message id
    session_id: str = ""  # session de conversation utilisateur
    user_id: str = ""
    originated_at: str = ""  # timestamp ISO de première création

    def __post_init__(self):
        if not self.correlation_id:
            self.correlation_id = uuid.uuid4().hex[:16]
        if not self.originated_at:
            self.originated_at = datetime.now(timezone.utc).isoformat()

    def child(self, parent_id: str = "") -> "CorrelationCtx":
        """Crée un contexte enfant en préservant la chaîne de corrélation."""
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
    AGENT_REASONING = "agent_reasoning"
    RENDERING = "rendering"
    CACHE = "cache"
    SECURITY = "security"


@dataclass
class TraceStep:
    """Étape unitaire de raisonnement / décision dans une trace."""

    category: TraceCategory
    module: str  # ex: "WhatsAppRenderer"
    action: str  # ex: "render"
    input_summary: Dict[str, Any] = field(default_factory=dict)
    output_summary: Dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
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
    """Trace de décision structurée, appendable par chaque module du pipeline."""

    trace_id: str = ""
    correlation: CorrelationCtx = field(default_factory=CorrelationCtx)
    steps: List[TraceStep] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""
    status: str = "in_progress"  # in_progress | completed | error

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    # ── Mutation ─────────────────────────────────────────────
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
        """Construit + ajoute un TraceStep en un seul appel."""
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

    # ── Sérialisation ────────────────────────────────────────
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
# CACHE FRESHNESS / INVALIDATION SÉMANTIQUE
# ═══════════════════════════════════════════════════════════════

# Mots-clés qui forcent le contournement du cache (urgence, maladie…)
CACHE_BYPASS_KEYWORDS: set[str] = {
    "urgence",
    "urgent",
    "emergency",
    "maladie",
    "disease",
    "inondation",
    "flood",
    "criquet",
    "locust",
    "invasion",
    "famine",
    "sécheresse",
    "drought",
    "alerte",
    "alert",
    "danger",
    "mort",
    "dead",
    "dying",
    "mourir",
    "épidémie",
    "epidemic",
    "contamination",
}


@dataclass
class CachePolicy:
    """Métadonnées de cache par entrée, pour invalidation sémantique."""

    key: str
    ttl_seconds: int = 300  # 5 min par défaut
    created_at: str = ""
    bypass_keywords: set[str] = field(default_factory=lambda: CACHE_BYPASS_KEYWORDS)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_expired(self) -> bool:
        created = datetime.fromisoformat(self.created_at)
        return datetime.now(timezone.utc) > created + timedelta(
            seconds=self.ttl_seconds
        )

    def should_bypass(self, text: str) -> bool:
        """True si *text* contient un mot-clé prioritaire → ignorer le cache."""
        lower = text.lower()
        return any(kw in lower for kw in self.bypass_keywords)

    def should_bypass_payload(self, payload: Dict[str, Any]) -> bool:
        """Vérifie les valeurs du payload (récursif superficiel)."""
        for v in payload.values():
            if isinstance(v, str) and self.should_bypass(v):
                return True
            if isinstance(v, dict):
                if self.should_bypass_payload(v):
                    return True
        # Une image/pièce jointe force également la reconstruction.
        if payload.get("image") or payload.get("attachment") or payload.get("photo"):
            return True
        return False


# ═══════════════════════════════════════════════════════════════
# CLIENT CAPABILITIES (négociation AG-UI)
# ═══════════════════════════════════════════════════════════════


@dataclass
class ClientCapabilities:
    """Manifeste de contraintes fourni par le canal UI AVANT génération.

    L'agent DOIT utiliser ces contraintes pour élaguer sa sortie.
    """

    channel: str = "web"  # whatsapp | web | sms | ussd | mobile
    max_chars: int = 0  # 0 = illimité
    max_buttons: int = 10  # WhatsApp = 3
    max_list_items: int = 50  # WhatsApp = 10
    supports_images: bool = True
    supports_cards: bool = True
    supports_charts: bool = True
    supports_voice: bool = False
    supports_markdown: bool = True
    supports_interactive: bool = True  # boutons / list pickers
    locale: str = "fr"
    text_format: str = "markdown"  # plain | markdown | html

    @classmethod
    def whatsapp(cls) -> "ClientCapabilities":
        return cls(
            channel="whatsapp",
            max_chars=4096,
            max_buttons=3,
            max_list_items=10,
            supports_charts=False,
            supports_markdown=False,
            text_format="plain",
            supports_voice=True,
        )

    @classmethod
    def sms(cls) -> "ClientCapabilities":
        return cls(
            channel="sms",
            max_chars=160,
            max_buttons=0,
            max_list_items=0,
            supports_images=False,
            supports_cards=False,
            supports_charts=False,
            supports_interactive=False,
            supports_markdown=False,
            text_format="plain",
        )

    @classmethod
    def ussd(cls) -> "ClientCapabilities":
        return cls(
            channel="ussd",
            max_chars=182,
            max_buttons=0,
            max_list_items=9,
            supports_images=False,
            supports_cards=False,
            supports_charts=False,
            supports_markdown=False,
            text_format="plain",
        )

    @classmethod
    def web(cls) -> "ClientCapabilities":
        return cls(channel="web")

    @classmethod
    def mobile(cls) -> "ClientCapabilities":
        return cls(channel="mobile", supports_voice=True)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


__all__ = [
    "CorrelationCtx",
    "TraceCategory",
    "TraceStep",
    "TraceEnvelope",
    "CACHE_BYPASS_KEYWORDS",
    "CachePolicy",
    "ClientCapabilities",
]
