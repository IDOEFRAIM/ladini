"""
A2A Messaging 2.0 — Async-ready, Observable, Stateless.
=========================================================

Breaking changes from v1:
  - ``A2AMessage`` carries a ``trace_envelope`` + ``correlation``
  - ``A2AChannel.send()`` returns ``AsyncResult`` (ACK)
  - Idempotency checked against DB (cross-instance)
  - Handshakes use ``HandshakeRecord`` FSM persisted to DB
  - All in-memory state is optional fallback when DB is unavailable
"""

from __future__ import annotations

import json
import logging
import uuid
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from agriconnect.protocols.core import (
    AsyncResult,
    AckStatus,
    CorrelationCtx,
    TraceEnvelope,
    TraceCategory,
    HandshakeRecord,
    HandshakeFSMError,
    HSState,
)
from agriconnect.core.settings import settings
import pickle
try:
    import redis
except Exception:
    redis = None

logger = logging.getLogger("A2A.Messaging")

# Maximum agent-to-agent hops per message chain before the message is dead-lettered.
MAX_HOPS: int = 3


class MessageType(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    HANDSHAKE = "handshake"
    HEARTBEAT = "heartbeat"
    ACK = "ack"

class HandshakeStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COUNTER = "counter_offer"
    COMPLETED = "completed"

@dataclass
class A2AMessage:
    message_id: str = ""
    message_type: MessageType = MessageType.REQUEST
    sender_id: str = ""
    receiver_id: str = ""
    intent: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    handshake_status: Optional[HandshakeStatus] = None
    reference_id: str = ""
    zone: str = ""
    crop: str = ""
    priority: int = 0
    ttl: int = 3600
    created_at: str = ""
    expires_at: str = ""
    idempotency_key: str = ""
    schema_version: str = "2.0"

    # ── v2: Routing metadata ──────────────────────────────────
    # topic: semantic channel for pub/sub routing (e.g. "market.price_check")
    topic: str = ""
    # hop_count: incremented at each A2AChannel.send(); rejected when >= MAX_HOPS
    hop_count: int = 0

    # ── v2: Observability ─────────────────────────────────────
    trace_envelope: Optional[TraceEnvelope] = field(default=None, repr=False)
    correlation: CorrelationCtx = field(default_factory=CorrelationCtx)

    def __post_init__(self):
        now = datetime.now(timezone.utc)
        if not self.message_id:
            self.message_id = str(uuid.uuid4())[:12]
        if not self.created_at:
            self.created_at = now.isoformat()
        if not self.expires_at:
            self.expires_at = (now + timedelta(seconds=self.ttl)).isoformat()
        if not self.idempotency_key:
            raw = f"{self.sender_id}:{self.intent}:{self.created_at}:{json.dumps(self.payload, sort_keys=True, default=str)}"
            self.idempotency_key = hashlib.sha256(raw.encode()).hexdigest()[:16]
        
        self.intent = self.intent.upper()
        self.zone = self.zone.upper()

        # Auto-create trace envelope if absent
        if self.trace_envelope is None:
            self.trace_envelope = TraceEnvelope(correlation=self.correlation)

    def validate(self) -> Dict[str, Any]:
        errors = []
        if not self.sender_id: errors.append("sender_id requis")
        if not self.intent: errors.append("intent requis")
        if self.message_type == MessageType.HANDSHAKE and not self.handshake_status:
            errors.append("handshake_status requis pour HANDSHAKE")
        if self.message_type == MessageType.RESPONSE and not self.reference_id:
            errors.append("reference_id requis pour RESPONSE")
        
        return {"status": "ok"} if not errors else {"error": "INVALID_MESSAGE", "details": errors}

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["message_type"] = self.message_type.value
        if self.handshake_status:
            d["handshake_status"] = self.handshake_status.value
        d["trace_envelope"] = self.trace_envelope.to_dict() if self.trace_envelope else None
        d["correlation"] = self.correlation.to_dict()
        return d

    def copy_for_receiver(self, receiver_id: str, override_type: Optional[MessageType] = None) -> "A2AMessage":
        """
        Create a safe copy of this message targeted to `receiver_id`.

        Preserves Enum types and avoids round-tripping through dict/asdict.
        Optionally override the message_type (e.g. BROADCAST).
        """
        return A2AMessage(
            message_id=str(uuid.uuid4())[:12],
            message_type=override_type or self.message_type,
            sender_id=self.sender_id,
            receiver_id=receiver_id,
            intent=self.intent,
            payload=self.payload.copy() if isinstance(self.payload, dict) else self.payload,
            handshake_status=self.handshake_status,
            reference_id=self.reference_id,
            zone=self.zone,
            crop=self.crop,
            priority=self.priority,
            ttl=self.ttl,
            created_at=self.created_at,
            expires_at=self.expires_at,
            idempotency_key=self.idempotency_key,
            schema_version=self.schema_version,
            trace_envelope=self.trace_envelope,
            correlation=self.correlation,
        )

    def create_response(self, payload: Dict[str, Any], status: str = "ok") -> "A2AMessage":
        return A2AMessage(
            message_type=MessageType.RESPONSE,
            sender_id=self.receiver_id,
            receiver_id=self.sender_id,
            intent=self.intent,
            payload={**payload, "status": status},
            reference_id=self.message_id,
            zone=self.zone,
            crop=self.crop,
            trace_envelope=self.trace_envelope,
            correlation=self.correlation.child(parent_id=self.message_id),
        )



# ═══════════════════════════════════════════════════════════════
# BROKER ABSTRACTION
# ═══════════════════════════════════════════════════════════════

class MessageBroker:
    """
    Abstract interface for message transport.
    Default: in-memory queues (dev/test).
    Production: subclass with Redis Streams or RabbitMQ.
    """
    def enqueue(self, queue_name: str, message: A2AMessage) -> int:
        raise NotImplementedError

    def dequeue(self, queue_name: str, limit: int = 10) -> List[A2AMessage]:
        raise NotImplementedError

    def queue_length(self, queue_name: str) -> int:
        raise NotImplementedError


class InMemoryBroker(MessageBroker):
    """Dev/test broker using plain dicts."""

    def __init__(self):
        self._queues: Dict[str, List[A2AMessage]] = defaultdict(list)

    def enqueue(self, queue_name: str, message: A2AMessage) -> int:
        self._queues[queue_name].append(message)
        return len(self._queues[queue_name])

    def dequeue(self, queue_name: str, limit: int = 10) -> List[A2AMessage]:
        q = self._queues[queue_name]
        batch, self._queues[queue_name] = q[:limit], q[limit:]
        return batch

    def queue_length(self, queue_name: str) -> int:
        return len(self._queues.get(queue_name, []))


class RedisBroker(MessageBroker):
    """Redis-backed broker using lists for durability.

    Stores pickled A2AMessage objects in Redis lists (RPUSH/LPOP).
    Requires `redis` Python package and a reachable Redis URL.
    """

    def __init__(self, url: str):
        if redis is None:
            raise RuntimeError("redis package is required for RedisBroker")
        self._client = redis.from_url(url, decode_responses=False)

    def _key(self, queue_name: str) -> str:
        return f"a2a:queue:{queue_name}"

    def enqueue(self, queue_name: str, message: A2AMessage) -> int:
        key = self._key(queue_name)
        data = pickle.dumps(message)
        self._client.rpush(key, data)
        return self._client.llen(key)

    def dequeue(self, queue_name: str, limit: int = 10) -> List[A2AMessage]:
        key = self._key(queue_name)
        msgs: List[A2AMessage] = []
        for _ in range(limit):
            data = self._client.lpop(key)
            if not data:
                break
            try:
                msg = pickle.loads(data)
                msgs.append(msg)
            except Exception as e:
                logger.warning("Failed to unpickle A2AMessage from Redis: %s", e)
        return msgs

    def queue_length(self, queue_name: str) -> int:
        return int(self._client.llen(self._key(queue_name)))


# ═══════════════════════════════════════════════════════════════
# IDEMPOTENCY STORE
# ═══════════════════════════════════════════════════════════════

class IdempotencyStore:
    """
    Cross-instance idempotency check.
    Default: in-memory set.  Production: backed by ``protocol_idempotency_keys`` table.
    """
    def __init__(self, session_factory=None):
        self._session_factory = session_factory
        self._local: Set[str] = set()

    def check_and_store(self, key: str, message_id: str) -> bool:
        """Return True if key is NEW (not seen). False if duplicate."""
        if self._session_factory:
            return self._db_check(key, message_id)
        if key in self._local:
            return False
        self._local.add(key)
        return True

    def _db_check(self, key: str, message_id: str) -> bool:
        from sqlalchemy import text
        session = self._session_factory()
        try:
            row = session.execute(
                text("SELECT 1 FROM protocol_idempotency_keys WHERE idempotency_key = :k"),
                {"k": key},
            ).first()
            if row:
                return False
            session.execute(
                text(
                    "INSERT INTO protocol_idempotency_keys (idempotency_key, message_id) "
                    "VALUES (:k, :mid) ON CONFLICT DO NOTHING"
                ),
                {"k": key, "mid": message_id},
            )
            session.commit()
            return True
        except Exception as exc:
            session.rollback()
            logger.warning("Idempotency DB check failed, using local: %s", exc)
            if key in self._local:
                return False
            self._local.add(key)
            return True
        finally:
            session.close()


# ═══════════════════════════════════════════════════════════════
# HANDSHAKE STORE (FSM persistence)
# ═══════════════════════════════════════════════════════════════

class HandshakeStore:
    """
    Persistent handshake state using ``protocol_handshake_states``.
    Fallback: in-memory dict.
    """
    def __init__(self, session_factory=None):
        self._session_factory = session_factory
        self._local: Dict[str, HandshakeRecord] = {}

    def save(self, record: HandshakeRecord) -> None:
        if self._session_factory:
            try:
                self._db_save(record)
                return
            except Exception as exc:
                logger.warning("Handshake DB save failed, using local: %s", exc)
        self._local[record.handshake_id] = record

    def load(self, handshake_id: str) -> Optional[HandshakeRecord]:
        if self._session_factory:
            try:
                return self._db_load(handshake_id)
            except Exception:
                pass
        return self._local.get(handshake_id)

    def _db_save(self, r: HandshakeRecord) -> None:
        from sqlalchemy import text
        session = self._session_factory()
        try:
            session.execute(
                text("""
                    INSERT INTO protocol_handshake_states
                        (handshake_id, initiator_id, responder_id, intent, current_state,
                         turns, max_turns, payload, history, timeout_at, created_at, updated_at)
                    VALUES
                        (:hid, :init, :resp, :intent, :state, :turns, :mt,
                         :payload::jsonb, :history::jsonb, :timeout::timestamptz,
                         :created::timestamptz, :updated::timestamptz)
                    ON CONFLICT (handshake_id) DO UPDATE SET
                        current_state = EXCLUDED.current_state,
                        turns = EXCLUDED.turns,
                        payload = EXCLUDED.payload,
                        history = EXCLUDED.history,
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "hid": r.handshake_id, "init": r.initiator_id, "resp": r.responder_id,
                    "intent": r.intent, "state": r.current_state.value, "turns": r.turns,
                    "mt": r.max_turns, "payload": json.dumps(r.payload, default=str),
                    "history": json.dumps(r.history, default=str),
                    "timeout": r.timeout_at, "created": r.created_at, "updated": r.updated_at,
                },
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _db_load(self, hid: str) -> Optional[HandshakeRecord]:
        from sqlalchemy import text
        session = self._session_factory()
        try:
            row = session.execute(
                text("SELECT * FROM protocol_handshake_states WHERE handshake_id = :hid"),
                {"hid": hid},
            ).mappings().first()
            if not row:
                return None
            return HandshakeRecord(
                handshake_id=row["handshake_id"],
                initiator_id=row["initiator_id"],
                responder_id=row["responder_id"],
                intent=row["intent"],
                current_state=HSState(row["current_state"]),
                turns=row["turns"],
                max_turns=row["max_turns"],
                payload=row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}"),
                history=row["history"] if isinstance(row["history"], list) else json.loads(row["history"] or "[]"),
                timeout_at=str(row.get("timeout_at", "")),
                created_at=str(row.get("created_at", "")),
                updated_at=str(row.get("updated_at", "")),
            )
        finally:
            session.close()


# ═══════════════════════════════════════════════════════════════
# A2A CHANNEL v2 (Async ACK + Broker + FSM + Observability)
# ═══════════════════════════════════════════════════════════════

class A2AChannel:
    """
    Asynchronous, observable message channel.

    Key differences from v1:
      - ``send()`` returns ``AsyncResult`` immediately (ACK pattern)
      - Idempotency is checked via DB (cross-pod)
      - Handshakes use ``HandshakeRecord`` FSM
      - Every operation records a ``TraceStep``
    """

    def __init__(
        self,
        broker: Optional[MessageBroker] = None,
        session_factory=None,
        tracer=None,
    ):
        # Default broker selection: explicit broker > Redis if configured > in-memory
        if broker:
            self.broker = broker
        else:
            redis_url = getattr(settings, "REDIS_URL", None) or getattr(settings, "CELERY_BROKER_URL", None)
            if redis_url:
                try:
                    self.broker = RedisBroker(redis_url)
                except Exception as e:
                    logger.warning("RedisBroker init failed (%s), falling back to InMemoryBroker", e)
                    self.broker = InMemoryBroker()
            else:
                self.broker = InMemoryBroker()
        self.idempotency = IdempotencyStore(session_factory)
        self.handshake_store = HandshakeStore(session_factory)
        self._session_factory = session_factory
        self._tracer = tracer

        self._subscriptions: Dict[str, Set[str]] = defaultdict(set)
        self._handlers: Dict[str, Callable] = {}
        self._message_count: int = 0
        logger.info("🔌 A2A Channel v2 initialisé (broker=%s)", type(self.broker).__name__)

    # ── SEND (returns ACK immediately) ───────────────────────

    def send(self, message: A2AMessage) -> AsyncResult:
        """Validate, deduplicate, enqueue. Returns an ACK immediately."""
        t0 = time.monotonic()

        # 0. Hop-count guard — prevent infinite A2A relay loops
        if message.hop_count >= MAX_HOPS:
            logger.error(
                "A2A hop limit reached (hop_count=%d >= MAX_HOPS=%d) — "
                "dead-lettering message %s (intent=%s, sender=%s → receiver=%s)",
                message.hop_count, MAX_HOPS,
                message.message_id, message.intent,
                message.sender_id, message.receiver_id,
            )
            return AsyncResult(
                correlation_id=message.correlation.correlation_id,
                message_id=message.message_id,
                ack_status=AckStatus.REJECTED,
                error=f"HOP_LIMIT_EXCEEDED (hop_count={message.hop_count})",
            )
        # Stamp the current hop before enqueueing so the receiver sees it.
        message.hop_count += 1

        # 1. Validate
        validation = message.validate()
        if "error" in validation:
            logger.warning("A2A message rejected: %s", validation)
            return AsyncResult(
                correlation_id=message.correlation.correlation_id,
                message_id=message.message_id,
                ack_status=AckStatus.REJECTED,
                error=str(validation.get("details", "")),
            )

        if not message.receiver_id:
            raise ValueError("receiver_id requis pour send()")

        # 2. Idempotency (cross-instance via DB)
        is_new = self.idempotency.check_and_store(message.idempotency_key, message.message_id)
        if not is_new:
            return AsyncResult(
                correlation_id=message.correlation.correlation_id,
                message_id=message.message_id,
                ack_status=AckStatus.DUPLICATE,
            )

        # 3. Enqueue via broker
        pos = self.broker.enqueue(message.receiver_id, message)
        self._message_count += 1

        # 4. Persist to message log (best-effort)
        self._persist_message(message)

        # 5. Record trace step
        duration = (time.monotonic() - t0) * 1000
        if message.trace_envelope:
            message.trace_envelope.record(
                TraceCategory.ROUTING,
                "A2AChannel",
                "send",
                input_summary={"intent": message.intent, "receiver": message.receiver_id},
                output_summary={"queue_position": pos, "status": "queued"},
                reasoning=f"Message enqueued for {message.receiver_id} (pos={pos})",
                duration_ms=duration,
            )

        # 6. Notify handler (fire-and-forget for sync compat)
        handler = self._handlers.get(message.receiver_id)
        if handler:
            try:
                handler(message)
            except Exception as e:
                logger.error("Handler error for %s: %s", message.receiver_id, e)

        return AsyncResult(
            correlation_id=message.correlation.correlation_id,
            message_id=message.message_id,
            ack_status=AckStatus.ACCEPTED,
            queue_position=pos,
        )

    # ── BROADCAST ────────────────────────────────────────────

    def broadcast(self, message: A2AMessage, topic: Optional[str] = None) -> List[str]:
        """Broadcast to all subscribers; returns list of agent_ids delivered to."""
        targets: Set[str] = set()

        if topic:
            targets.update(self._subscriptions.get(topic.upper(), set()))
        if message.intent:
            if message.zone:
                auto_topic = f"{message.intent}_{message.zone}".upper()
                targets.update(self._subscriptions.get(auto_topic, set()))
            targets.update(self._subscriptions.get(message.intent.upper(), set()))

        delivered_to: List[str] = []
        for agent_id in targets:
            if agent_id == message.sender_id:
                continue
            msg_copy = message.copy_for_receiver(agent_id, override_type=MessageType.BROADCAST)
            self.broker.enqueue(agent_id, msg_copy)
            delivered_to.append(agent_id)

        self._message_count += 1

        if message.trace_envelope:
            message.trace_envelope.record(
                TraceCategory.ROUTING,
                "A2AChannel",
                "broadcast",
                input_summary={"topic": topic, "intent": message.intent},
                output_summary={"delivered_count": len(delivered_to), "agents": delivered_to},
                reasoning=f"Broadcast to {len(delivered_to)} agents on topic {topic}",
            )

        return delivered_to

    # ── RECEIVE ──────────────────────────────────────────────

    def receive(self, agent_id: str, limit: int = 10) -> List[A2AMessage]:
        """Dequeue messages for a given agent (worker pulls)."""
        return self.broker.dequeue(agent_id, limit)

    # ── SUBSCRIBE / UNSUBSCRIBE ──────────────────────────────

    def subscribe(self, agent_id: str, topic: str) -> None:
        self._subscriptions[topic.upper()].add(agent_id)
        logger.info("📌 %s subscribed to %s", agent_id, topic.upper())

    def unsubscribe(self, agent_id: str, topic: str) -> None:
        self._subscriptions[topic.upper()].discard(agent_id)

    def register_handler(self, agent_id: str, handler: Callable) -> None:
        self._handlers[agent_id] = handler

    # ── HANDSHAKE (FSM-based) ────────────────────────────────

    def initiate_handshake(
        self,
        message: A2AMessage,
        max_turns: int = 5,
        timeout_sec: int = 300,
    ) -> str:
        """Start a handshake negotiation. Returns handshake_id."""
        if not message.receiver_id:
            raise ValueError("receiver_id requis pour initiate_handshake()")

        record = HandshakeRecord(
            handshake_id=message.message_id,
            initiator_id=message.sender_id,
            responder_id=message.receiver_id,
            intent=message.intent,
            max_turns=max_turns,
            timeout_at=(datetime.now(timezone.utc) + timedelta(seconds=timeout_sec)).isoformat(),
            payload=message.payload,
        )
        self.handshake_store.save(record)

        message.message_type = MessageType.HANDSHAKE
        message.handshake_status = HandshakeStatus.PROPOSED
        self.send(message)

        if message.trace_envelope:
            message.trace_envelope.record(
                TraceCategory.HANDSHAKE,
                "A2AChannel",
                "initiate_handshake",
                input_summary={"responder": message.receiver_id, "intent": message.intent},
                output_summary={"handshake_id": record.handshake_id, "state": "proposed"},
                reasoning="Handshake initiated — FSM state: PROPOSED",
            )

        return record.handshake_id

    def respond_handshake(
        self,
        handshake_id: str,
        responder_id: str,
        status: HandshakeStatus,
        payload: Optional[Dict[str, Any]] = None,
    ) -> A2AMessage:
        """Advance handshake FSM. Validates transition legality."""
        record = self.handshake_store.load(handshake_id)
        if not record:
            raise ValueError(f"Handshake {handshake_id} inconnu")

        hs_map = {
            HandshakeStatus.ACCEPTED: HSState.ACCEPTED,
            HandshakeStatus.REJECTED: HSState.REJECTED,
            HandshakeStatus.COUNTER: HSState.COUNTER,
            HandshakeStatus.COMPLETED: HSState.COMPLETED,
        }
        target_state = hs_map.get(status)
        if not target_state:
            raise ValueError(f"Invalid handshake status: {status}")

        record.transition(target_state, responder_id, payload)
        self.handshake_store.save(record)

        last_initiator = record.initiator_id if responder_id != record.initiator_id else record.responder_id
        response = A2AMessage(
            message_type=MessageType.HANDSHAKE,
            sender_id=responder_id,
            receiver_id=last_initiator,
            intent=record.intent,
            payload=payload or {},
            handshake_status=status,
            reference_id=handshake_id,
        )
        self.send(response)
        return response

    # ── STATS ────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "total_sent": self._message_count,
            "subs": sum(len(s) for s in self._subscriptions.values()),
            "handlers": len(self._handlers),
            "broker_type": type(self.broker).__name__,
        }

    # ── INTERNAL ─────────────────────────────────────────────

    def _persist_message(self, message: A2AMessage) -> None:
        """Best-effort persist to ``protocol_message_log``."""
        if not self._session_factory:
            return
        try:
            from sqlalchemy import text
            session = self._session_factory()
            try:
                session.execute(
                    text("""
                        INSERT INTO protocol_message_log
                            (message_id, correlation_id, message_type, sender_id, receiver_id,
                             intent, zone, crop, priority, payload, trace_id, status)
                        VALUES
                            (:mid, :cid, :mtype, :sid, :rid, :intent, :zone, :crop,
                             :priority, :payload::jsonb, :tid, 'queued')
                        ON CONFLICT (message_id) DO NOTHING
                    """),
                    {
                        "mid": message.message_id,
                        "cid": message.correlation.correlation_id,
                        "mtype": message.message_type.value,
                        "sid": message.sender_id,
                        "rid": message.receiver_id,
                        "intent": message.intent,
                        "zone": message.zone,
                        "crop": message.crop,
                        "priority": message.priority,
                        "payload": json.dumps(message.payload, default=str),
                        "tid": message.trace_envelope.trace_id if message.trace_envelope else None,
                    },
                )
                session.commit()
            except Exception:
                session.rollback()
            finally:
                session.close()
        except Exception as exc:
            logger.debug("Message log persist skipped: %s", exc)