"""
A2A Discovery 2.0 — Optimised & Streamlined
======================================================================

Every routing decision records candidates, scores, and winner
reasoning into the message's TraceEnvelope.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from functools import lru_cache

from agriconnect.core.agent_registry import internal_agents
from agriconnect.core.settings import settings

from .registry import A2ARegistry, AgentCard, AgentDomain, AgentStatus
from .messaging import A2AChannel, A2AMessage, MessageType
from agriconnect.protocols.core import (
    CorrelationCtx,
    TraceCategory,
    TraceEnvelope,
)

logger = logging.getLogger("A2A.Discovery")

class A2ADiscovery:
    """
    Service de découverte et routing inter-agents.
    Gère le cycle de vie : Enregistrement -> Découverte -> Routage.
    """

    def __init__(self, registry: Optional[A2ARegistry] = None, channel: Optional[A2AChannel] = None):
        self.registry = registry or A2ARegistry()
        self.channel = channel or A2AChannel()
        # Simple in-memory cache for manifests
        self._manifests_cache: List[Dict[str, Any]] = []
        self._manifests_cache_ts: float = 0.0
        logger.info("🔌 A2A Discovery Service initialisé")

    # Cached to avoid recreating strings for frequent keys (Performance)
    @staticmethod
    @lru_cache(maxsize=256)
    def normalize_key(val: Optional[str]) -> Optional[str]:
        return val.strip().upper() if val else None
    
    def _build_message(
        self,
        sender: str,
        intent_key: str,
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> A2AMessage:
        meta = metadata or {}
        # Ensure intent_key is a string (A2AMessage expects a string for .upper())
        intent_val = intent_key or ""
        return A2AMessage(
            sender_id=sender,
            intent=intent_val,
            payload=payload,
            zone=(self.normalize_key(meta.get("zone")) or ""),
            crop=meta.get("crop", ""),
            priority=meta.get("priority", 0),
            trace_envelope=meta.get("trace_envelope"),
            correlation=meta.get("correlation") or CorrelationCtx(),
        )

    def register_internal_agents(self):
        """Enregistre les agents internes définis dans la configuration core."""
        for card in internal_agents:
            self._register_and_subscribe(card)
        logger.info("✅ %d agents internes enregistrés et abonnés", len(internal_agents))
        # Ensure a small dev stub agent exists to help in quick-tests (returns canned responses)
        # Only register the dev stub in development/debug environments to avoid noise in production.
        try:
            # Only enable the dev stub when explicitly requested via DEBUG or
            # the environment variable ENABLE_DEV_STUB=true. Avoid relying on
            # SENTRY_ENVIRONMENT default values to prevent accidental registration.
            enable_stub = settings.DEBUG or (os.getenv("ENABLE_DEV_STUB", "false").lower() == "true")
            if enable_stub and not any(c.name == "Dev Stub" for c in self.registry._agents.values()):
                self.add_dev_stub_agent()
        except Exception:
            # don't fail startup for minor dev convenience
            pass

    def register_external_agent(self, card: AgentCard) -> str:
        """Enregistre un agent externe (ex: formation, sentinelle)."""
        card.protocol = card.protocol or "http"
        agent_id = self._register_and_subscribe(card)
        logger.info("🔗 Agent externe enregistré: %s (%s)", card.name, card.endpoint)
        return agent_id

    def _register_and_subscribe(self, card: AgentCard) -> str:
        agent_id = self.registry.register(card)
        # Subscribe to general intent channels. If the card explicitly declares
        # specific zones (not 'all'), subscribe to per-zone topics as well.
        for intent in card.intents:
            intent_key = self.normalize_key(intent)
            # Subscribe to the base intent and a GLOBAL alias
            self.channel.subscribe(agent_id, intent_key)
            self.channel.subscribe(agent_id, f"{intent_key}_GLOBAL")
            # If explicit zones are provided other than 'all', subscribe to them
            explicit_zones = [z for z in card.zones if z and z.strip().lower() != 'all']
            for zone in explicit_zones:
                zone_key = self.normalize_key(zone)
                if zone_key:
                    self.channel.subscribe(agent_id, f"{intent_key}_{zone_key}")
        return agent_id

    def find_agents(
        self,
        intent: Optional[str] = None,
        zone: Optional[str] = None,
        crop: Optional[str] = None,
        domain: Optional[AgentDomain] = None,
    ) -> List[AgentCard]:
        return self.registry.discover(
            intent=self.normalize_key(intent), 
            zone=self.normalize_key(zone), 
            crop=crop, 
            domain=domain
        )

    def get_all_capabilities(self, ttl_seconds: int = 30) -> List[Dict[str, Any]]:
        """Return a list of agent manifests (dicts). Cached for `ttl_seconds` to avoid frequent registry hits."""
        now = time.time()
        if self._manifests_cache and (now - self._manifests_cache_ts) < ttl_seconds:
            return self._manifests_cache

        manifests: List[Dict[str, Any]] = []
        try:
            # Build manifests from registry agents
            for aid, card in (self.registry._agents.items() if hasattr(self.registry, '_agents') else []):
                manifests.append({
                    "agent_id": card.agent_id,
                    "name": card.name,
                    "description": card.description,
                    "intents": list(card.intents),
                    "capabilities": list(card.capabilities),
                    "zones": list(card.zones),
                    "crops": list(card.crops),
                    "endpoint": card.endpoint,
                    "protocol": card.protocol,
                    "avg_response_ms": card.avg_response_ms,
                })
        except Exception as e:
            logger.debug("Error building manifests: %s", e)

        self._manifests_cache = manifests
        self._manifests_cache_ts = now
        return manifests

    def call_agent(self, *args, **kwargs) -> Dict[str, Any]:
        """Compatibility wrapper for older call signatures.

        Supported call patterns:
        - call_agent(agent_name, query, context)
        - call_agent(agent_name=..., query=..., context=...)
        - call_agent(sender, agent_id, intent, payload, metadata=None)
        The wrapper normalizes the arguments and delegates to the internal
        implementation `_call_agent_impl`.
        """
        # Backwards-compatible form: (agent_name, query, context)
        if "agent_name" in kwargs or (len(args) >= 1 and isinstance(args[0], str) and (len(args) >= 2)):
            agent_name = kwargs.get("agent_name") or args[0]
            query = kwargs.get("query") or (args[1] if len(args) >= 2 else "")
            context = kwargs.get("context") or (args[2] if len(args) >= 3 else {})
            sender = kwargs.get("sender") or "agri_orchestrator"
            intent = kwargs.get("intent") or "CHAT"
            payload = {"query": query, "context": context}
            timeout = float(kwargs.get("timeout_sec", kwargs.get("timeout", 2.0)))
            return self._call_agent_impl(sender, agent_name, intent, payload, metadata=None, timeout_sec=timeout)

        # New form: (sender, agent_id, intent, payload, metadata=None)
        sender = args[0] if len(args) >= 1 else kwargs.get("sender", "agri_orchestrator")
        agent_id = args[1] if len(args) >= 2 else kwargs.get("agent_id") or kwargs.get("agent_name")
        intent = args[2] if len(args) >= 3 else kwargs.get("intent", "CHAT")
        payload = args[3] if len(args) >= 4 else kwargs.get("payload", {})
        metadata = kwargs.get("metadata")
        timeout = float(kwargs.get("timeout_sec", kwargs.get("timeout", 2.0)))
        return self._call_agent_impl(sender, agent_id, intent, payload, metadata=metadata, timeout_sec=timeout)

    def _call_agent_impl(self, sender: str, agent_id: str, intent: str, payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None, timeout_sec: float = 2.0) -> Dict[str, Any]:
        """Internal implementation used by the compatibility wrapper.
        See `call_agent` for compatibility handling.
        """
        meta = metadata or {}
        try:
            message = self._build_message(sender, intent, payload, meta)
            message.receiver_id = agent_id
            # Send (returns ACK)
            ack = self.channel.send(message)
        except Exception as exc:
            logger.exception("call_agent failed building/sending message: sender=%s agent_id=%s intent=%s payload=%s meta=%s", sender, agent_id, intent, repr(payload), repr(meta))
            raise

        # If a synchronous handler is registered, try calling it and capture any return
        handler = getattr(self.channel, "_handlers", {}).get(agent_id)
        if handler:
            try:
                resp = handler(message)
                if resp is not None:
                    # Normalize A2AMessage -> text payload for orchestrator
                    if isinstance(resp, A2AMessage):
                        text = resp.payload.get("text") if isinstance(resp.payload, dict) else str(resp.payload)
                        return {"ack": ack, "response": text, "raw": resp}
                    return {"ack": ack, "response": (resp if isinstance(resp, str) else str(resp)), "raw": resp}
            except Exception as e:
                logger.debug("Dev handler raised: %s", e)

        # Poll the sender's inbound queue for a RESPONSE referencing our message
        deadline = time.time() + float(timeout_sec)
        while time.time() < deadline:
            try:
                inbox = self.channel.receive(sender, limit=10)
            except Exception:
                inbox = []
            for m in inbox:
                try:
                    if m.message_type == MessageType.RESPONSE and getattr(m, "reference_id", "") == message.message_id:
                        if isinstance(m, A2AMessage):
                            text = m.payload.get("text") if isinstance(m.payload, dict) else str(m.payload)
                            return {"ack": ack, "response": text, "raw": m}
                        return {"ack": ack, "response": (m if isinstance(m, str) else str(m)), "raw": m}
                except Exception:
                    continue
            time.sleep(0.05)

        return {"ack": ack, "response": None}

    def add_dev_stub_agent(self, agent_id: str = "dev_stub", name: str = "Dev Stub") -> str:
        """Register a small development stub agent that replies with a canned message.

        The stub registers in the local registry and sets a handler on the channel
        which immediately sends back a textual canned RESPONSE message.
        """
        try:
            card = AgentCard(
                agent_id=agent_id,
                name=name,
                description="Development stub agent returning canned responses",
                intents=["CHAT", "PING", "CHECK_WEATHER", "LEARN"],
                capabilities=["dev_stub"],
                zones=["ALL"],
                crops=["all"],
                endpoint="local://dev_stub",
                protocol="internal",
            )
            aid = self._register_and_subscribe(card)

            def _handler(msg: A2AMessage):
                try:
                    # Create a simple canned response and send it back
                    resp = msg.create_response({"text": f"Stub response from {name}", "agent_id": aid}, status="ok")
                    self.channel.send(resp)
                    return resp
                except Exception as e:
                    logger.debug("Dev stub handler error: %s", e)
                    return None

            self.channel.register_handler(aid, _handler)
            logger.info("🔧 Dev stub agent registered: %s (%s)", name, aid)
            return aid
        except Exception as e:
            logger.debug("Failed to register dev stub agent: %s", e)
            return ""

    def route_message(self, sender: str, intent: str, payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Route un message. If `receiver` is provided in metadata, do a direct route; otherwise use discovery."""
        t0 = time.monotonic()
        intent_key = self.normalize_key(intent) or ""
        meta = metadata or {}
        
        zone = meta.get("zone", "")
        crop = meta.get("crop", "")
        receiver = meta.get("receiver")
        trace_env = meta.get("trace_envelope")

        message = self._build_message(sender, intent_key, payload, meta)

        if receiver:
            return self._route_direct(message, receiver, intent_key, t0)

        ctx = self._SendContext(
            message=message,
            agents=self.find_agents(intent=intent_key, zone=zone, crop=crop),
            scored=self.registry.discover_scored(intent=intent_key, zone=zone, crop=crop),
            intent_key=intent_key,
            zone=zone,
            crop=crop,
            payload=payload,
            trace_envelope=trace_env,
            t0=t0,
        )
        return self._route_by_intent(ctx)

    def _route_direct(self, message: A2AMessage, receiver: str, intent_key: str, t0: Optional[float] = None) -> Dict[str, Any]:
        message.receiver_id = receiver
        ack = self.channel.send(message)
        
        trace_envelope = message.trace_envelope
        start = t0 if t0 is not None else time.monotonic()
        duration_ms = (time.monotonic() - start) * 1000

        self._record_trace_safe(
            trace_envelope,
            TraceCategory.ROUTING,
            "A2ADiscovery",
            "route_direct",
            input_summary={"intent": intent_key, "receiver": receiver},
            output_summary={"ack": getattr(ack, 'ack_status', None)},
            reasoning=f"Direct route to {receiver}",
            duration_ms=duration_ms,
        )
        return {"message_id": getattr(ack, 'message_id', message.message_id), "delivered_to": [receiver], "status": "ok"}

    def _route_by_intent(self, ctx: _SendContext) -> Dict[str, Any]:
        if not ctx.agents:
            logger.warning("🚫 Aucun agent trouvé pour %s dans la zone %s", ctx.intent_key, ctx.zone)
            self._record_trace_safe(
                ctx.trace_envelope,
                TraceCategory.DISCOVERY,
                "A2ADiscovery",
                "no_candidates",
                input_summary={"intent": ctx.intent_key, "zone": ctx.zone, "crop": ctx.crop},
                output_summary={"candidates": 0},
                reasoning="No agent found matching intent/zone/crop",
                duration_ms=(time.monotonic() - ctx.t0) * 1000,
            )
            return {"message_id": ctx.message.message_id, "delivered_to": [], "status": "no_agent"}

        return self._attempt_send_to_agents(ctx)

    def _validate_payload(self, agent: AgentCard, payload: Dict[str, Any]) -> Optional[str]:
        schema = getattr(agent, "input_schema", None)
        if not schema or not isinstance(schema, dict):
            return None
        
        required = schema.get("required", [])
        missing = [r for r in required if r not in payload]
        return f"missing_required_fields: {missing}" if missing else None

    @dataclass
    class _SendContext:
        message: A2AMessage
        agents: List[AgentCard]
        scored: Any
        intent_key: str
        zone: str
        crop: str
        payload: Dict[str, Any]
        trace_envelope: Optional[TraceEnvelope]
        t0: float

    def _record_trace_safe(self, trace_envelope: Optional[TraceEnvelope], *args, **kwargs):
        if trace_envelope:
            try:
                trace_envelope.record(*args, **kwargs)
            except Exception as e:
                logger.debug("Erreur silencieuse lors de l'enregistrement de la trace: %s", e)

    def _send_to_single_agent(self, ctx: _SendContext, agent: AgentCard) -> tuple[bool, Any, str]:
        attempted_id = agent.agent_id
        
        if agent.status != AgentStatus.ACTIVE:
            self._record_trace_safe(
                ctx.trace_envelope, TraceCategory.DISCOVERY, "A2ADiscovery", "skip_inactive",
                input_summary={"agent": attempted_id}, output_summary={"status": agent.status.value},
                reasoning=f"Skipped {attempted_id} due to status={agent.status.value}"
            )
            return False, None, attempted_id

        payload_err = self._validate_payload(agent, ctx.payload)
        if payload_err:
            self._record_trace_safe(
                ctx.trace_envelope, TraceCategory.DISCOVERY, "A2ADiscovery", "invalid_payload",
                input_summary={"agent": attempted_id, "required_missing": payload_err},
                output_summary={"status": "invalid_payload"},
                reasoning=f"Payload validation failed for {attempted_id}: {payload_err}"
            )
            return False, None, attempted_id

        ctx.message.receiver_id = attempted_id
        ack = self.channel.send(ctx.message)
        ack_status = getattr(ack, 'ack_status', None)

        self._record_trace_safe(
            ctx.trace_envelope, TraceCategory.DISCOVERY, "A2ADiscovery", "attempt_send",
            input_summary={"intent": ctx.intent_key, "candidate": attempted_id},
            output_summary={"ack": ack_status},
            reasoning=f"Attempted send to {attempted_id}; ack={ack_status}",
            duration_ms=(time.monotonic() - ctx.t0) * 1000,
        )

        status_val = ack_status.value if hasattr(ack_status, 'value') else str(ack_status)
        
        if status_val == 'accepted':
            self._record_trace_safe(
                ctx.trace_envelope, TraceCategory.DISCOVERY, "A2ADiscovery", "route_by_intent",
                input_summary={"intent": ctx.intent_key, "zone": ctx.zone, "crop": ctx.crop},
                output_summary={"candidates": ctx.scored, "winner": attempted_id, "winner_name": agent.name},
                reasoning=f"Selected {agent.name} ({attempted_id}) from {len(ctx.scored)} candidates",
                duration_ms=(time.monotonic() - ctx.t0) * 1000,
            )
            return True, ack, attempted_id

        self._record_trace_safe(
            ctx.trace_envelope, TraceCategory.DISCOVERY, "A2ADiscovery", "candidate_failed",
            input_summary={"agent": attempted_id}, output_summary={"ack": ack_status},
            reasoning=f"Candidate {attempted_id} failed to accept message; trying next"
        )
        return False, ack, attempted_id

    def _attempt_send_to_agents(self, ctx: _SendContext) -> Dict[str, Any]:
        last_ack = None
        attempted: List[str] = []

        for agent in ctx.agents:
            attempted.append(agent.agent_id)
            accepted, ack, attempted_id = self._send_to_single_agent(ctx, agent)
            
            if ack: last_ack = ack
            
            if accepted:
                return {
                    "message_id": getattr(ack, 'message_id', ctx.message.message_id),
                    "delivered_to": [attempted_id],
                    "agent_name": agent.name,
                    "status": "ok"
                }

        self._record_trace_safe(
            ctx.trace_envelope, TraceCategory.DISCOVERY, "A2ADiscovery", "undeliverable",
            input_summary={"intent": ctx.intent_key, "attempted": attempted},
            output_summary={"last_ack": getattr(last_ack, 'ack_status', None) if last_ack else None},
            reasoning="No candidate accepted the message",
            duration_ms=(time.monotonic() - ctx.t0) * 1000,
        )

        return {"message_id": ctx.message.message_id, "delivered_to": [], "status": "undeliverable"}

    def broadcast_offer(self, sender: str, intent: str, payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Diffuse une offre à tous les agents abonnés au topic."""
        meta = metadata or {}
        intent_key = self.normalize_key(intent)
        zone_key = self.normalize_key(meta.get("zone"))

        message = A2AMessage(
            sender_id=sender,
            intent=intent_key,
            payload=payload,
            zone=zone_key,
            crop=meta.get("crop", ""),
            trace_envelope=meta.get("trace_envelope"),
        )

        topic = f"{intent_key}_{zone_key}" if zone_key and zone_key != "ALL" else intent_key
        delivered = self.channel.broadcast(message, topic=topic)

        return {
            "message_id": message.message_id,
            "topic": topic,
            "delivered_to": delivered,
            "count": len(delivered),
            "status": "ok",
        }

    # ═══════════════════════════════════════════════════════════
    # TRADING & MONITORING
    # ═══════════════════════════════════════════════════════════

    def initiate_trade(self, seller_id: str, buyer_id: str, offer: Dict[str, Any]) -> str:
        """Déclenche un protocole de négociation sécurisé entre deux agents."""
        message = A2AMessage(
            sender_id=seller_id,
            receiver_id=buyer_id,
            intent="TRADE_INITIATE",
            payload=offer,
        )
        return self.channel.initiate_handshake(message)

    def status(self) -> Dict[str, Any]:
        """Donne une vue globale de la santé du réseau A2A."""
        return {
            "registry_stats": self.registry.stats(),
            "channel_stats": self.channel.stats(),
            "uptime_status": "healthy"
        }