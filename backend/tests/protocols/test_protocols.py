"""
Tests unitaires — Protocoles A2A (messaging, registry, discovery).
"""

import pytest
from unittest.mock import MagicMock, patch


class TestA2ARegistry:
    """Tests du registre d'agents A2A."""

    def test_register_agent(self):
        from agriconnect.protocols.a2a.registry import A2ARegistry, AgentCard, AgentDomain

        reg = A2ARegistry()
        card = AgentCard(
            name="TestAgent",
            domain=AgentDomain.FORMATION,
            capabilities=["training"],
            endpoint="http://localhost:8000",
            zones=["BOBO", "OUAGA"],
        )
        agent_id = reg.register(card)
        assert agent_id is not None
        assert len(agent_id) > 0

    def test_discover_by_domain(self):
        from agriconnect.protocols.a2a.registry import A2ARegistry, AgentCard, AgentDomain

        reg = A2ARegistry()
        card = AgentCard(
            name="FormationAgent",
            domain=AgentDomain.FORMATION,
            capabilities=["training"],
            endpoint="http://localhost",
            zones=["all"],
        )
        reg.register(card)
        found = reg.discover(domain=AgentDomain.FORMATION)
        assert len(found) >= 1
        assert any(c.name == "FormationAgent" for c in found)

    def test_discover_by_intent(self):
        from agriconnect.protocols.a2a.registry import A2ARegistry, AgentCard, AgentDomain

        reg = A2ARegistry()
        card = AgentCard(
            name="PriceAgent",
            domain=AgentDomain.MARKET,
            intents=["CHECK_PRICE", "SELL_OFFER"],
            zones=["all"],
        )
        reg.register(card)
        found = reg.discover(intent="CHECK_PRICE")
        assert len(found) >= 1
        assert found[0].name == "PriceAgent"

    def test_register_and_unregister(self):
        from agriconnect.protocols.a2a.registry import A2ARegistry, AgentCard, AgentDomain

        reg = A2ARegistry()
        card = AgentCard(name="TempAgent", domain=AgentDomain.SOIL)
        agent_id = reg.register(card)
        assert len(reg.discover()) == 1
        reg.unregister(agent_id)
        assert len(reg.discover()) == 0

    def test_heartbeat_updates_status(self):
        from agriconnect.protocols.a2a.registry import (
            A2ARegistry, AgentCard, AgentDomain, AgentStatus,
        )

        reg = A2ARegistry()
        card = AgentCard(name="HeartbeatAgent", domain=AgentDomain.WEATHER)
        agent_id = reg.register(card)
        reg.heartbeat(agent_id, AgentStatus.BUSY)
        assert reg._agents[agent_id].status == AgentStatus.BUSY

    def test_stats(self):
        from agriconnect.protocols.a2a.registry import A2ARegistry, AgentCard, AgentDomain

        reg = A2ARegistry()
        reg.register(AgentCard(name="A", domain=AgentDomain.FORMATION, intents=["LEARN"]))
        reg.register(AgentCard(name="B", domain=AgentDomain.MARKET, intents=["SELL"]))
        stats = reg.stats()
        assert stats["total"] == 2
        assert stats["active"] == 2


class TestA2AMessaging:
    """Tests du canal de messagerie A2A."""

    def test_create_message(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage, MessageType

        msg = A2AMessage(
            sender_id="agent_1",
            receiver_id="agent_2",
            message_type=MessageType.REQUEST,
            intent="CHECK_PRICE",
            payload={"question": "Quel est le prix du maïs ?"},
        )
        assert msg.sender_id == "agent_1"
        assert msg.receiver_id == "agent_2"
        assert msg.message_type == MessageType.REQUEST
        assert msg.intent == "CHECK_PRICE"

    def test_message_validation_ok(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage, MessageType

        msg = A2AMessage(
            sender_id="a1",
            receiver_id="a2",
            intent="CHECK_PRICE",
        )
        result = msg.validate()
        assert result == {"status": "ok"}

    def test_message_validation_missing_sender(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage

        msg = A2AMessage(receiver_id="a2", intent="SELL")
        result = msg.validate()
        assert "error" in result

    def test_copy_for_receiver(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage, MessageType

        msg = A2AMessage(
            sender_id="broadcaster",
            receiver_id="all",
            message_type=MessageType.BROADCAST,
            intent="ALERT",
            payload={"alert": "Sécheresse"},
        )
        copy = msg.copy_for_receiver("agent_42")
        assert copy.receiver_id == "agent_42"
        assert copy.sender_id == "broadcaster"
        assert copy.payload == msg.payload
        assert copy is not msg
        assert copy.message_id != msg.message_id

    def test_channel_send_receive(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage, MessageType
        from agriconnect.protocols.core import AckStatus

        channel = A2AChannel()

        msg = A2AMessage(
            sender_id="sentinel",
            receiver_id="agent_1",
            intent="FLOOD_ALERT",
            message_type=MessageType.REQUEST,
            payload={"type": "FLOOD"},
        )
        ack = channel.send(msg)
        assert ack.ack_status == AckStatus.ACCEPTED

        inbox = channel.receive("agent_1")
        assert len(inbox) == 1
        assert inbox[0].payload["type"] == "FLOOD"

    def test_send_requires_receiver_id(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage

        channel = A2AChannel()
        msg = A2AMessage(sender_id="a1", intent="SELL")  # no receiver_id
        with pytest.raises(ValueError, match="receiver_id"):
            channel.send(msg)

    def test_broadcast_creates_copies(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage, MessageType

        channel = A2AChannel()
        channel.subscribe("a1", "ALERTS")
        channel.subscribe("a2", "ALERTS")

        msg = A2AMessage(
            sender_id="broadcaster",
            intent="ALERT_WEATHER",
            payload={"data": "test"},
        )
        delivered = channel.broadcast(msg, topic="ALERTS")

        assert "a1" in delivered
        assert "a2" in delivered

        inbox_a1 = channel.receive("a1")
        inbox_a2 = channel.receive("a2")
        assert len(inbox_a1) == 1
        assert len(inbox_a2) == 1
        assert inbox_a1[0].receiver_id == "a1"
        assert inbox_a2[0].receiver_id == "a2"
        assert inbox_a1[0].message_type == MessageType.BROADCAST

    def test_broadcast_excludes_sender(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage

        channel = A2AChannel()
        channel.subscribe("broadcaster", "NEWS")
        channel.subscribe("listener", "NEWS")

        msg = A2AMessage(sender_id="broadcaster", intent="NEWS")
        delivered = channel.broadcast(msg, topic="NEWS")
        assert "broadcaster" not in delivered
        assert "listener" in delivered

    def test_handshake_requires_receiver_id(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage

        channel = A2AChannel()
        msg = A2AMessage(sender_id="a1", intent="NEGOTIATE")
        with pytest.raises(ValueError, match="receiver_id"):
            channel.initiate_handshake(msg)

    def test_handshake_full_cycle(self):
        from agriconnect.protocols.a2a.messaging import (
            A2AChannel, A2AMessage, HandshakeStatus, MessageType,
        )

        channel = A2AChannel()
        msg = A2AMessage(
            sender_id="buyer",
            receiver_id="seller",
            intent="NEGOTIATE_PRICE",
            payload={"price": 250},
        )
        hs_id = channel.initiate_handshake(msg)
        assert hs_id == msg.message_id

        # Seller accepts
        resp = channel.respond_handshake(hs_id, "seller", HandshakeStatus.ACCEPTED, {"price": 240})
        assert resp.handshake_status == HandshakeStatus.ACCEPTED
        assert resp.sender_id == "seller"

    def test_idempotency_dedup(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage
        from agriconnect.protocols.core import AckStatus

        channel = A2AChannel()
        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="SELL")
        ack1 = channel.send(msg)
        ack2 = channel.send(msg)  # same idempotency key → deduplicated
        assert ack1.ack_status == AckStatus.ACCEPTED
        assert ack2.ack_status == AckStatus.DUPLICATE
        inbox = channel.receive("a2")
        assert len(inbox) == 1

    def test_channel_stats(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage

        channel = A2AChannel()
        channel.send(A2AMessage(sender_id="a", receiver_id="b", intent="X"))
        stats = channel.stats()
        assert stats["total_sent"] == 1
        assert stats["broker_type"] == "InMemoryBroker"


class TestA2ADiscovery:
    """Tests du service de découverte A2A."""

    def test_discovery_init(self):
        from agriconnect.protocols.a2a import A2ADiscovery

        discovery = A2ADiscovery()
        assert discovery.registry is not None
        assert discovery.channel is not None

    def test_register_internal_agents(self):
        from agriconnect.protocols.a2a import A2ADiscovery

        discovery = A2ADiscovery()
        discovery.register_internal_agents()
        stats = discovery.registry.stats()
        # At least the 5 agents from agent_registry.py
        assert stats["total"] >= 5
        assert stats["active"] >= 5

    def test_discover_agent_by_intent(self):
        from agriconnect.protocols.a2a import A2ADiscovery

        discovery = A2ADiscovery()
        discovery.register_internal_agents()
        found = discovery.registry.discover(intent="CHECK_PRICE")
        assert len(found) >= 1
        assert any(c.name == "MarketCoach" for c in found)


# ═════════════════════════════════════════════════════════════════════=
# v2 TESTS — Protocol Hardening & Observability
# ═════════════════════════════════════════════════════════════════════=


class TestTraceEnvelope:
    """Tests for structured decision tracing."""

    def test_create_envelope(self):
        from agriconnect.protocols.core import TraceEnvelope, CorrelationCtx

        corr = CorrelationCtx(user_id="farmer_42", session_id="sess_1")
        env = TraceEnvelope(correlation=corr)
        assert env.trace_id
        assert env.status == "in_progress"
        assert env.correlation.user_id == "farmer_42"

    def test_record_steps(self):
        from agriconnect.protocols.core import TraceEnvelope, TraceCategory

        env = TraceEnvelope()
        env.record(
            TraceCategory.DISCOVERY,
            "TestModule",
            "test_action",
            input_summary={"intent": "CHECK_PRICE"},
            output_summary={"candidates": 3},
            reasoning="Found 3 candidates",
            duration_ms=12.5,
        )
        assert len(env.steps) == 1
        assert env.steps[0].category == TraceCategory.DISCOVERY
        assert env.steps[0].reasoning == "Found 3 candidates"
        assert env.steps[0].duration_ms == 12.5

    def test_complete_envelope(self):
        from agriconnect.protocols.core import TraceEnvelope

        env = TraceEnvelope()
        env.complete()
        assert env.status == "completed"
        assert env.completed_at

    def test_serialize_deserialize(self):
        from agriconnect.protocols.core import TraceEnvelope, TraceCategory

        env = TraceEnvelope()
        env.record(TraceCategory.ROUTING, "Channel", "send", reasoning="test")
        env.complete()

        data = env.to_dict()
        restored = TraceEnvelope.from_dict(data)
        assert restored.trace_id == env.trace_id
        assert len(restored.steps) == 1
        assert restored.steps[0].reasoning == "test"
        assert restored.status == "completed"

    def test_correlation_child(self):
        from agriconnect.protocols.core import CorrelationCtx

        parent = CorrelationCtx(user_id="u1", session_id="s1")
        child = parent.child(parent_id="msg_42")
        assert child.correlation_id == parent.correlation_id
        assert child.parent_id == "msg_42"
        assert child.user_id == "u1"


class TestHandshakeFSM:
    """Tests for finite-state-machine handshake transitions."""

    def test_valid_proposed_to_accepted(self):
        from agriconnect.protocols.core import HandshakeRecord, HSState

        rec = HandshakeRecord(
            handshake_id="hs1", initiator_id="buyer", responder_id="seller", intent="TRADE"
        )
        assert rec.current_state == HSState.PROPOSED
        rec.transition(HSState.ACCEPTED, "seller")
        assert rec.current_state == HSState.ACCEPTED
        assert rec.turns == 1
        assert len(rec.history) == 1

    def test_valid_proposed_to_counter(self):
        from agriconnect.protocols.core import HandshakeRecord, HSState

        rec = HandshakeRecord(
            handshake_id="hs2", initiator_id="a", responder_id="b", intent="NEGOTIATE"
        )
        rec.transition(HSState.COUNTER, "b", {"price": 200})
        assert rec.current_state == HSState.COUNTER

    def test_counter_chain(self):
        from agriconnect.protocols.core import HandshakeRecord, HSState

        rec = HandshakeRecord(
            handshake_id="hs3", initiator_id="a", responder_id="b",
            intent="TRADE", max_turns=10,
        )
        rec.transition(HSState.COUNTER, "b", {"price": 200})
        rec.transition(HSState.COUNTER, "a", {"price": 180})
        rec.transition(HSState.ACCEPTED, "b")
        assert rec.current_state == HSState.ACCEPTED
        assert rec.turns == 3

    def test_invalid_transition_raises(self):
        from agriconnect.protocols.core import HandshakeRecord, HSState, HandshakeFSMError

        rec = HandshakeRecord(
            handshake_id="hs4", initiator_id="a", responder_id="b", intent="X"
        )
        rec.transition(HSState.REJECTED, "b")
        assert rec.current_state == HSState.REJECTED
        assert rec.is_terminal

        with pytest.raises(HandshakeFSMError):
            rec.transition(HSState.ACCEPTED, "a")

    def test_max_turns_enforced(self):
        from agriconnect.protocols.core import HandshakeRecord, HSState, HandshakeFSMError

        rec = HandshakeRecord(
            handshake_id="hs5", initiator_id="a", responder_id="b",
            intent="NEGOTIATE", max_turns=2,
        )
        rec.transition(HSState.COUNTER, "b")
        rec.transition(HSState.COUNTER, "a")
        with pytest.raises(HandshakeFSMError, match="Max turns"):
            rec.transition(HSState.COUNTER, "b")

    def test_accepted_to_completed(self):
        from agriconnect.protocols.core import HandshakeRecord, HSState

        rec = HandshakeRecord(
            handshake_id="hs6", initiator_id="a", responder_id="b", intent="TRADE"
        )
        rec.transition(HSState.ACCEPTED, "b")
        rec.transition(HSState.COMPLETED, "a")
        assert rec.is_terminal
        assert rec.current_state == HSState.COMPLETED


class TestAsyncACK:
    """Tests for async ACK pattern on A2AChannel."""

    def test_send_returns_async_result(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage
        from agriconnect.protocols.core import AckStatus

        channel = A2AChannel()
        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="CHECK")
        ack = channel.send(msg)
        assert ack.ack_status == AckStatus.ACCEPTED
        assert ack.message_id == msg.message_id
        assert ack.queue_position >= 1

    def test_send_rejected_invalid_message(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage
        from agriconnect.protocols.core import AckStatus

        channel = A2AChannel()
        msg = A2AMessage(receiver_id="a2", intent="")  # missing sender + intent
        ack = channel.send(msg)
        assert ack.ack_status == AckStatus.REJECTED
        assert ack.error

    def test_send_duplicate_returns_duplicate(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage
        from agriconnect.protocols.core import AckStatus

        channel = A2AChannel()
        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="SELL")
        ack1 = channel.send(msg)
        ack2 = channel.send(msg)
        assert ack1.ack_status == AckStatus.ACCEPTED
        assert ack2.ack_status == AckStatus.DUPLICATE


class TestMessageTraceEnvelope:
    """Tests that A2AMessage carries trace_envelope and records steps."""

    def test_message_auto_creates_trace(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage

        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="SELL")
        assert msg.trace_envelope is not None
        assert msg.trace_envelope.trace_id

    def test_send_records_trace_step(self):
        from agriconnect.protocols.a2a.messaging import A2AChannel, A2AMessage

        channel = A2AChannel()
        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="SELL")
        channel.send(msg)
        # send() records a ROUTING trace step
        assert len(msg.trace_envelope.steps) >= 1
        assert msg.trace_envelope.steps[0].action == "send"

    def test_message_schema_v2(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage

        msg = A2AMessage(sender_id="a1", intent="X")
        assert msg.schema_version == "2.0"

    def test_message_to_dict_includes_trace(self):
        from agriconnect.protocols.a2a.messaging import A2AMessage

        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="SELL")
        d = msg.to_dict()
        assert "trace_envelope" in d
        assert "correlation" in d
        assert d["trace_envelope"]["trace_id"]


class TestCachePolicy:
    """Tests for semantic cache invalidation."""

    def test_bypass_on_emergency_keyword(self):
        from agriconnect.protocols.core import CachePolicy

        policy = CachePolicy(key="test")
        assert policy.should_bypass("Mon maïs a une maladie grave")
        assert policy.should_bypass("URGENCE inondation dans la zone")
        assert not policy.should_bypass("Quel est le prix du mil ?")

    def test_bypass_on_payload_keyword(self):
        from agriconnect.protocols.core import CachePolicy

        policy = CachePolicy(key="test")
        assert policy.should_bypass_payload({"question": "invasion de criquets"})
        assert not policy.should_bypass_payload({"question": "prix du sorgho"})

    def test_bypass_on_image_attachment(self):
        from agriconnect.protocols.core import CachePolicy

        policy = CachePolicy(key="test")
        assert policy.should_bypass_payload({"image": "photo_leaf.jpg", "question": "normal"})

    def test_ttl_expiration(self):
        from agriconnect.protocols.core import CachePolicy
        from datetime import datetime, timezone, timedelta

        # Create policy that expired 10 seconds ago
        expired_time = (datetime.now(timezone.utc) - timedelta(seconds=310)).isoformat()
        policy = CachePolicy(key="test", ttl_seconds=300, created_at=expired_time)
        assert policy.is_expired

    def test_ttl_not_expired(self):
        from agriconnect.protocols.core import CachePolicy

        policy = CachePolicy(key="test", ttl_seconds=300)
        assert not policy.is_expired


class TestClientCapabilities:
    """Tests for multi-channel capability negotiation."""

    def test_whatsapp_limits(self):
        from agriconnect.protocols.core import ClientCapabilities

        caps = ClientCapabilities.whatsapp()
        assert caps.max_buttons == 3
        assert caps.max_list_items == 10
        assert not caps.supports_charts
        assert caps.max_chars == 4096

    def test_sms_limits(self):
        from agriconnect.protocols.core import ClientCapabilities

        caps = ClientCapabilities.sms()
        assert caps.max_chars == 160
        assert caps.max_buttons == 0
        assert not caps.supports_images
        assert not caps.supports_interactive

    def test_web_defaults(self):
        from agriconnect.protocols.core import ClientCapabilities

        caps = ClientCapabilities.web()
        assert caps.supports_charts
        assert caps.supports_images
        assert caps.supports_interactive

    def test_component_pruning(self):
        from agriconnect.protocols.ag_ui.renderer import prune_components
        from agriconnect.protocols.ag_ui.components import (
            TextBlock, ChartData, Card, ActionButton, ActionType,
        )
        from agriconnect.protocols.core import ClientCapabilities

        components = [
            TextBlock(content="Hello"),
            ChartData(title="Price Trends", chart_type="line"),
            Card(title="Market", body="Info", actions=[
                ActionButton(label=f"Btn{i}", action_type=ActionType.NAVIGATE) for i in range(5)
            ]),
        ]

        # WhatsApp: should remove chart, trim buttons to 3
        caps = ClientCapabilities.whatsapp()
        kept, log = prune_components(components, caps)
        assert len(kept) == 2  # text + card (chart removed)
        # Card actions should be trimmed
        card = [c for c in kept if hasattr(c, 'actions')][0]
        assert len(card.actions) <= 3

    def test_sms_pruning_removes_interactive(self):
        from agriconnect.protocols.ag_ui.renderer import prune_components
        from agriconnect.protocols.ag_ui.components import (
            TextBlock, ActionButton, ListPicker, ActionType,
        )
        from agriconnect.protocols.core import ClientCapabilities

        components = [
            TextBlock(content="Hello"),
            ActionButton(label="Buy", action_type=ActionType.BUY),
            ListPicker(title="Options", items=[{"label": "A"}, {"label": "B"}]),
        ]

        caps = ClientCapabilities.sms()
        kept, log = prune_components(components, caps)
        assert len(kept) == 1  # only text survives
        assert kept[0].content == "Hello"


class TestProtocolTracer:
    """Tests for observability layer (in-memory mode)."""

    def test_start_and_complete_trace(self):
        from agriconnect.protocols.observability import ProtocolTracer
        from agriconnect.protocols.core import CorrelationCtx, TraceCategory

        tracer = ProtocolTracer()  # no DB
        corr = CorrelationCtx(user_id="test_user")
        env = tracer.start_trace(corr)
        env.record(TraceCategory.ROUTING, "Test", "action1", reasoning="test step")
        tracer.complete_trace(env)

        assert env.status == "completed"
        # Should be buffered in-memory
        buffered = tracer.get_buffered_traces()
        assert len(buffered) == 1
        assert buffered[0]["trace_id"] == env.trace_id

    def test_get_trace_from_buffer(self):
        from agriconnect.protocols.observability import ProtocolTracer

        tracer = ProtocolTracer()
        env = tracer.start_trace()
        tracer.complete_trace(env)

        found = tracer.get_trace(env.trace_id)
        assert found is not None
        assert found["trace_id"] == env.trace_id

    def test_list_traces_with_filter(self):
        from agriconnect.protocols.observability import ProtocolTracer
        from agriconnect.protocols.core import CorrelationCtx

        tracer = ProtocolTracer()
        corr1 = CorrelationCtx(user_id="user_a")
        corr2 = CorrelationCtx(user_id="user_b")

        env1 = tracer.start_trace(corr1)
        tracer.complete_trace(env1)

        env2 = tracer.start_trace(corr2)
        tracer.complete_trace(env2)

        all_traces = tracer.list_traces()
        assert len(all_traces) == 2

        user_a_traces = tracer.list_traces(user_id="user_a")
        assert len(user_a_traces) == 1

    def test_fail_trace(self):
        from agriconnect.protocols.observability import ProtocolTracer

        tracer = ProtocolTracer()
        env = tracer.start_trace()
        tracer.fail_trace(env, error="Something went wrong")

        assert env.status == "error"
        # Error step should be recorded
        assert any(s.reasoning == "Something went wrong" for s in env.steps)


class TestBrokerAbstraction:
    """Tests for the message broker layer."""

    def test_in_memory_broker_enqueue_dequeue(self):
        from agriconnect.protocols.a2a.messaging import InMemoryBroker, A2AMessage

        broker = InMemoryBroker()
        msg = A2AMessage(sender_id="a1", receiver_id="a2", intent="X")
        pos = broker.enqueue("a2", msg)
        assert pos == 1

        batch = broker.dequeue("a2", limit=5)
        assert len(batch) == 1
        assert batch[0].sender_id == "a1"

        # Queue should be empty now
        assert broker.queue_length("a2") == 0

    def test_channel_with_custom_broker(self):
        from agriconnect.protocols.a2a.messaging import (
            A2AChannel, A2AMessage, InMemoryBroker,
        )
        from agriconnect.protocols.core import AckStatus

        broker = InMemoryBroker()
        channel = A2AChannel(broker=broker)
        msg = A2AMessage(sender_id="x", receiver_id="y", intent="Z")
        ack = channel.send(msg)
        assert ack.ack_status == AckStatus.ACCEPTED

        # Verify message is in the broker
        assert broker.queue_length("y") == 1


class TestScoredDiscovery:
    """Tests for scored agent discovery with trace recording."""

    def test_discover_scored_returns_scores(self):
        from agriconnect.protocols.a2a.registry import (
            A2ARegistry, AgentCard, AgentDomain,
        )

        reg = A2ARegistry()
        reg.register(AgentCard(
            name="FastAgent", domain=AgentDomain.MARKET,
            intents=["CHECK_PRICE"], zones=["ALL"], avg_response_ms=100,
        ))
        reg.register(AgentCard(
            name="SlowAgent", domain=AgentDomain.MARKET,
            intents=["CHECK_PRICE"], zones=["ALL"], avg_response_ms=800,
        ))

        scored = reg.discover_scored(intent="CHECK_PRICE")
        assert len(scored) == 2
        assert scored[0]["name"] == "FastAgent"  # faster = higher rank
        assert scored[0]["score"] > scored[1]["score"]
        assert "rank" in scored[0]
        assert "reason" in scored[0]
