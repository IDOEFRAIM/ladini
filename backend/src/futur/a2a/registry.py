"""
A2A Registry 2.0 — Agent registration with optional DB persistence.
====================================================================

Agents are indexed in-memory for O(1) discovery and optionally
persisted to ``protocol_agent_registry`` for cross-instance consistency.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger("A2A.Registry")

# ═══════════════════════════════════════════════════════════
# ENUMS & MODELS
# ═══════════════════════════════════════════════════════════

class AgentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BUSY = "busy"
    ERROR = "error"

class AgentDomain(str, Enum):
    DIAGNOSIS = "diagnosis"
    MARKET = "market"
    MARKETPLACE = "marketplace"
    WEATHER = "weather"
    FORMATION = "formation"
    SOIL = "soil"
    FINANCE = "finance"
    EXTERNAL = "external"

@dataclass
class AgentCard:
    agent_id: str = ""
    name: str = ""
    description: str = ""
    domain: AgentDomain = AgentDomain.EXTERNAL
    intents: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    # Optional JSON Schema or lightweight contract describing expected payload
    input_schema: Optional[Dict[str, Any]] = None
    zones: List[str] = field(default_factory=list)
    crops: List[str] = field(default_factory=list)
    endpoint: str = ""
    protocol: str = "internal"
    version: str = "1.0"
    status: AgentStatus = AgentStatus.ACTIVE
    avg_response_ms: int = 500
    registered_at: str = ""
    last_heartbeat: str = ""

    def __post_init__(self):
        if not self.agent_id: self.agent_id = str(uuid.uuid4())[:8]
        if not self.registered_at: self.registered_at = datetime.now(timezone.utc).isoformat()
        # Normalisation automatique
        self.intents = [i.strip().upper() for i in self.intents]
        # Store zones in UPPERCASE for consistent topic matching
        self.zones = [z.strip().upper() for z in self.zones]
        self.crops = [c.strip().lower() for c in self.crops]

    def supports_zone(self, zone: str) -> bool:
        z = zone.strip().upper()
        return "ALL" in self.zones or z in self.zones

    def supports_crop(self, crop: str) -> bool:
        c = crop.strip().lower()
        return "all" in self.crops or c in self.crops

# ═══════════════════════════════════════════════════════════
# REGISTRY CORE
# ═══════════════════════════════════════════════════════════

class A2ARegistry:
    def __init__(self, session_factory=None):
        self._agents: Dict[str, AgentCard] = {}
        self._intent_index: Dict[str, Set[str]] = {}
        self._zone_index: Dict[str, Set[str]] = {}
        self._session_factory = session_factory
        logger.info("🔌 A2A Registry initialisé (db=%s)", "yes" if session_factory else "no")

    def register(self, card: AgentCard) -> str:
        """Enregistre un agent et met à jour les index de recherche rapide."""
        self._agents[card.agent_id] = card

        for intent in card.intents:
            self._intent_index.setdefault(intent.upper(), set()).add(card.agent_id)

        for zone in card.zones:
            self._zone_index.setdefault(zone.upper(), set()).add(card.agent_id)

        # Persist to DB (best-effort)
        self._persist_agent(card)

        logger.info("✅ Agent [%s] enregistré (%s)", card.name, card.agent_id)
        return card.agent_id

    def unregister(self, agent_id: str):
        """Supprime un agent et nettoie proprement tous les index."""
        if agent_id in self._agents:
            del self._agents[agent_id]
            for index in [self._intent_index, self._zone_index]:
                for ids in index.values():
                    ids.discard(agent_id)
            logger.info("❌ Agent [%s] retiré", agent_id)

    def _candidate_ids_for(self, intent: Optional[str] = None, zone: Optional[str] = None) -> Optional[Set[str]]:
        """
        Return a set of candidate agent IDs matching the given intent and/or zone,
        or None when no filtering is requested.
        """
        intent_key = self._normalize_intent(intent)
        zone_key = self._normalize_zone(zone)

        # If no filtering requested, signal with None
        no_intent = intent_key is None
        no_zone = zone_key is None
        zone_is_all = zone_key == "ALL"
        if no_intent:
            if no_zone:
                return None
            if zone_is_all:
                return None

        intent_ids = self._get_intent_ids(intent_key) if intent_key else None
        zone_ids = self._get_zone_ids(zone_key) if zone_key and zone_key != "ALL" else None

        if intent_ids is None:
            return zone_ids
        if zone_ids is None:
            return intent_ids

        return intent_ids & zone_ids

    def _normalize_intent(self, intent: Optional[str]) -> Optional[str]:
        return intent.strip().upper() if intent and intent.strip() else None

    def _normalize_zone(self, zone: Optional[str]) -> Optional[str]:
        return zone.strip().upper() if zone and zone.strip() else None

    def _get_intent_ids(self, intent_key: str) -> Set[str]:
        return set(self._intent_index.get(intent_key, set()))

    def _get_zone_ids(self, zone_key: str) -> Set[str]:
        return set(self._zone_index.get(zone_key, set())) | set(self._zone_index.get("ALL", set()))

    def _apply_filters(self, candidates: List[AgentCard], domain: Optional[AgentDomain], crop: Optional[str]) -> List[AgentCard]:
        return [
            a for a in candidates
            if a.status == AgentStatus.ACTIVE
            and (not domain or a.domain == domain)
            and (not crop or a.supports_crop(crop))
        ]

    def discover(
        self,
        intent: Optional[str] = None,
        zone: Optional[str] = None,
        crop: Optional[str] = None,
        domain: Optional[AgentDomain] = None,
    ) -> List[AgentCard]:
        """
        Moteur de recherche hybride.
        Utilise les index pour filtrer massivement, puis affine les résultats.
        """
        # 1. Intersection des index (Recherche ultra-rapide)
        candidates_ids = self._candidate_ids_for(intent, zone)

        # 2. Récupération des objets
        if candidates_ids is not None:
            candidates = [self._agents[aid] for aid in candidates_ids if aid in self._agents]
        else:
            candidates = list(self._agents.values())

        # 3. Filtrage fin (Cultures, Domaine, Status)
        results = self._apply_filters(candidates, domain, crop)

        # 4. Tri par performance
        results.sort(key=lambda a: a.avg_response_ms)
        return results

    def heartbeat(self, agent_id: str, status: AgentStatus = AgentStatus.ACTIVE):
        """Update last seen."""
        if agent_id in self._agents:
            self._agents[agent_id].status = status
            self._agents[agent_id].last_heartbeat = datetime.now(timezone.utc).isoformat()

    def stats(self) -> Dict[str, Any]:
        return {
            "total": len(self._agents),
            "active": sum(1 for a in self._agents.values() if a.status == AgentStatus.ACTIVE),
            "intents": len(self._intent_index),
            "zones": len(self._zone_index)
        }

    def discover_scored(
        self,
        intent: Optional[str] = None,
        zone: Optional[str] = None,
        crop: Optional[str] = None,
        domain: Optional[AgentDomain] = None,
    ) -> List[Dict[str, Any]]:
        """
        Like ``discover()`` but returns scored dicts for trace recording.
        Each dict: {"agent_id", "name", "score", "reason"}
        """
        candidates = self.discover(intent=intent, zone=zone, crop=crop, domain=domain)
        scored = []
        for i, agent in enumerate(candidates):
            score = max(0, 100 - agent.avg_response_ms // 10)
            reason_parts = []
            if intent and intent.upper() in agent.intents:
                reason_parts.append(f"matches intent {intent}")
            if zone and agent.supports_zone(zone):
                reason_parts.append(f"covers zone {zone}")
            if crop and agent.supports_crop(crop):
                reason_parts.append(f"handles crop {crop}")
            reason_parts.append(f"avg_ms={agent.avg_response_ms}")
            scored.append({
                "agent_id": agent.agent_id,
                "name": agent.name,
                "rank": i + 1,
                "score": score,
                "reason": ", ".join(reason_parts),
            })
        return scored

    # ── DB persistence ────────────────────────────────────

    def _persist_agent(self, card: AgentCard) -> None:
        """Best-effort persist to ``protocol_agent_registry``."""
        if not self._session_factory:
            return
        try:
            from sqlalchemy import text
            session = self._session_factory()
            try:
                session.execute(
                    text("""
                        INSERT INTO protocol_agent_registry
                            (agent_id, name, domain, intents, zones, crops,
                             endpoint, protocol, version, status, avg_response_ms)
                        VALUES
                            (:aid, :name, :domain, :intents::jsonb, :zones::jsonb,
                             :crops::jsonb, :endpoint, :protocol, :version, :status, :avg)
                        ON CONFLICT (agent_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            avg_response_ms = EXCLUDED.avg_response_ms,
                            last_heartbeat = NOW()
                    """),
                    {
                        "aid": card.agent_id, "name": card.name,
                        "domain": card.domain.value, "intents": json.dumps(card.intents),
                        "zones": json.dumps(card.zones), "crops": json.dumps(card.crops),
                        "endpoint": card.endpoint, "protocol": card.protocol,
                        "version": card.version, "status": card.status.value,
                        "avg": card.avg_response_ms,
                    },
                )
                session.commit()
            except Exception:
                session.rollback()
            finally:
                session.close()
        except Exception as exc:
            logger.debug("Agent registry persist skipped: %s", exc)