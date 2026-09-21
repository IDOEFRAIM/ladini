"""`process_agent_task` + télémétrie de tour : les 6 scénarios demandés (succès, clarification, erreur d'outil, erreur LLM,
fallback, erreur WhatsApp) et la garantie que la télémétrie ne transforme jamais un message réussi en échec."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ladini.core import turn_telemetry as tt


class _Store:
    def __init__(self):
        self.claims, self.vals = set(), {}

    def claim_once(self, key, *, ttl_seconds=3600):
        if not key:
            return True
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def get_cached(self, key):
        return self.vals.get(key) if key else None

    def set_cached(self, key, value, *, ttl_seconds=3600):
        if key:
            self.vals[key] = value

    def release(self, key):
        self.claims.discard(key)


@pytest.fixture()
def env(monkeypatch):
    import ladini.api.response_dispatch as dispatch_mod
    import ladini.api.tasks as mod

    store = _Store()
    for n in ("claim_once", "get_cached", "set_cached", "release"):
        monkeypatch.setattr(mod, n, getattr(store, n))
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(mod, "_loop", loop, raising=False)
    monkeypatch.setattr(dispatch_mod.settings, "MESSAGING_PROVIDER", "twilio", raising=False)
    # Envoi sans Redis : la garde d'idempotence d'envoi est neutralisée (on teste la télémétrie, pas l'envoi).
    monkeypatch.setattr(dispatch_mod, "claim_response_item", lambda key: True)
    monkeypatch.setattr(dispatch_mod, "_send_via_twilio", lambda phone, text, result: {"status": "message_sent", "sid": "SM_OUT"})
    captured: list[tt.TurnRecorder] = []

    async def spy(rec):
        tt.end_turn()
        captured.append(rec)
        return True

    monkeypatch.setattr(tt, "finish_and_persist", spy)
    yield SimpleNamespace(mod=mod, dispatch=dispatch_mod, captured=captured, monkeypatch=monkeypatch)
    loop.close()


def _orchestrator(env, hook=None, response=None):
    async def handle(*a, **k):
        if hook:
            hook()
        return response or {"final_response": "Votre offre a été créée.", "agent": "market"}

    env.monkeypatch.setattr(env.mod, "_orchestrator", SimpleNamespace(handle=handle), raising=False)


def _run(env, **kw):
    return env.mod.process_agent_task.run(phone_number="+22670124582", user_query="Je veux vendre 50 kg de tomates",
                                          message_sid=f"SM_{uuid.uuid4().hex[:8]}", **kw)


def test_successful_turn_is_recorded_with_outcome_and_delivery(env):
    def hook():
        tt.note_final({"detected_intent": "SALES_PUBLISH", "interpreter_confidence": 0.96, "current_goal": "SALES_PUBLISH", "status": "COMPLETED"})
        tt.note_tool("find_product", "READ", datetime.now(timezone.utc), 0.087, "SUCCESS")
        tt.note_tool("create_listing", "WRITE", datetime.now(timezone.utc), 0.143, "SUCCESS")

    _orchestrator(env, hook)
    assert _run(env) == [{"status": "message_sent", "sid": "SM_OUT"}]
    (rec,) = env.captured
    assert (rec.outcome, rec.response_status, rec.intent, rec.workflow) == ("COMPLETED", "SENT", "SALES_PUBLISH", "SALES_PUBLISH")
    assert [(t.tool_name, t.status) for t in rec.tools] == [("find_product", "SUCCESS"), ("create_listing", "SUCCESS")]
    assert rec.whatsapp_ms is not None and rec.response_text == "Votre offre a été créée." and rec.user_message.startswith("Je veux vendre")


def test_clarification_turn(env):
    _orchestrator(env, lambda: tt.note_final({"response_strategy": "CLARIFICATION", "detected_intent": "UNKNOWN", "interpreter_confidence": 0.31}))
    _run(env)
    assert env.captured[0].outcome == "CLARIFICATION" and env.captured[0].intent_confidence == pytest.approx(0.31)


def test_tool_error_is_recorded_and_the_user_still_gets_an_answer(env):
    _orchestrator(env, lambda: tt.note_tool("create_listing", "WRITE", datetime.now(timezone.utc), 0.2, "ERROR", ValueError("x")))
    assert _run(env) == [{"status": "message_sent", "sid": "SM_OUT"}]
    t = env.captured[0].tools[0]
    assert (t.status, t.error_category, t.error_code) == ("ERROR", "TOOL", "ValueError")


def test_llm_error_and_fallback_are_recorded(env):
    def hook():
        tt.note_llm(name="llm_gateway_completion", model="a", provider="groq", profile="REASONING", agent_node=None,
                    latency_s=0.3, usage=None, error="429", fallback_from=None)
        tt.note_llm(name="llm_gateway_completion", model="b", provider="bedrock", profile="REASONING", agent_node=None,
                    latency_s=0.9, usage=None, error=None, fallback_from="groq:a")

    _orchestrator(env, hook)
    _run(env)
    rec = env.captured[0]
    assert [(c.provider, c.status, c.is_fallback) for c in rec.llms] == [("groq", "ERROR", False), ("bedrock", "SUCCESS", True)]


def test_agent_timeout_fallback_turn_is_an_error_but_is_still_delivered(env):
    _orchestrator(env, lambda: tt.note_error("AGENT_TIMEOUT", "TIMEOUT"), response={"final_response": "Désolé…", "agent": "market"})
    _run(env)
    rec = env.captured[0]
    assert (rec.outcome, rec.error_code, rec.error_category, rec.response_status) == ("ERROR", "AGENT_TIMEOUT", "TIMEOUT", "SENT")


def test_whatsapp_failure_is_recorded_and_still_propagates_for_the_celery_retry(env):
    def boom(phone, text, result):
        raise ConnectionError("Twilio 503")

    env.monkeypatch.setattr(env.dispatch, "_send_via_twilio", boom)
    _orchestrator(env)
    with pytest.raises(ConnectionError):
        _run(env)
    rec = env.captured[0]
    assert (rec.response_status, rec.error_category, rec.outcome) == ("FAILED", "WHATSAPP", "ERROR")


def test_send_failed_status_is_mapped_to_failed(env):
    env.monkeypatch.setattr(env.dispatch, "_send_via_twilio", lambda p, t, r: {"status": "message_skipped", "reason": "send_failed"})
    _orchestrator(env)
    _run(env)
    assert env.captured[0].response_status == "FAILED"


def test_a_telemetry_crash_never_turns_a_delivered_message_into_a_failure(env):
    async def exploding(rec):
        raise RuntimeError("le module de télémétrie a planté")

    env.monkeypatch.setattr(tt, "finish_and_persist", exploding)
    _orchestrator(env)
    assert _run(env) == [{"status": "message_sent", "sid": "SM_OUT"}]


def test_duplicates_are_not_recorded(env):
    _orchestrator(env)
    sid = "SM_DUPLICATE"
    env.mod.process_agent_task.run(phone_number="+22670124582", user_query="a", message_sid=sid)
    again = env.mod.process_agent_task.run(phone_number="+22670124582", user_query="a", message_sid=sid)
    assert again == [{"status": "duplicate_skipped", "reason": "already_completed"}]
    assert len(env.captured) == 1
    assert tt.current() is None  # le contexte de tour est bien fermé sur le chemin « doublon »
