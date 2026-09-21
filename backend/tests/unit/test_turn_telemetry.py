"""Télémétrie de tour (cockpit /admin/monitoring) : confidentialité, hooks, issue métier, et — surtout — GARANTIE que
l'observabilité ne casse jamais le traitement métier (aucune base, aucun réseau)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ladini.core import turn_telemetry as tt
from ladini.core.settings import settings as _settings


@pytest.fixture(autouse=True)
def _no_leak():
    tt.end_turn()
    yield
    tt.end_turn()


# ── Confidentialité ──────────────────────────────────────────────────────────

class TestRedaction:
    def test_phone_numbers_emails_and_tokens_are_masked(self):
        out = tt.redact_text("Appelle +226 70 12 45 82 ou 70124582, mail a.b@ex.com, clé sk_live_a1b2c3d4e5f6g7h8i9j0k1l2m3")
        assert "70 12 45" not in out and "70124582" not in out and "@" not in out and "sk_live" not in out
        assert "[tel]" in out and "[email]" in out and "[jeton]" in out

    def test_quantities_and_prices_stay_readable(self):
        assert tt.redact_text("Je veux vendre 50 kg de tomates à 700 fcfa") == "Je veux vendre 50 kg de tomates à 700 fcfa"

    def test_sensitive_goals_keep_nothing(self):
        assert tt.redact_text("4821", goal="VERIFY_DELIVERY_OTP") == "[contenu masqué]"

    def test_long_text_is_truncated_and_empty_is_none(self):
        assert len(tt.redact_text("a " * 500)) <= 281
        assert tt.redact_text("") is None and tt.redact_text(None) is None

    def test_phone_hash_is_stable_keyed_and_not_the_number(self):
        h = tt.hash_phone("+226 70 12 45 82")
        assert h == tt.hash_phone("22670124582") and len(h) == 64 and "7012" not in h
        assert tt.hash_phone("+22670000000") != h
        assert tt.phone_last4("+226 70 12 45 82") == "4582" and tt.phone_last4("12") is None


# ── Issue métier d'un tour ───────────────────────────────────────────────────

@pytest.mark.parametrize("final,expected", [
    ({"status": "COMPLETED"}, "COMPLETED"),
    ({"security_status": "PROMPT_INJECTION_DETECTED"}, "BLOCKED"),
    ({"security_status": "ACCOUNT_BLOCKED", "status": "COMPLETED"}, "BLOCKED"),
    ({"requires_human": True}, "HUMAN_REQUIRED"),
    ({"status": "ERROR"}, "ERROR"),
    ({"response_strategy": "CLARIFICATION"}, "CLARIFICATION"),
    ({"status": "WAITING_CONFIRMATION"}, "WAITING_USER"),
    ({"goal_status": "WAITING_INPUT"}, "WAITING_USER"),
    ({}, "COMPLETED"),
])
def test_derive_outcome(final, expected):
    assert tt.derive_outcome(final) == expected


def test_note_final_extracts_intent_workflow_and_step():
    rec = tt.TurnRecorder(phone="+22670000000")
    tt.begin_turn(rec)
    tt.note_final({
        "detected_intent": "SALES_PUBLISH", "interpreter_confidence": 0.93, "current_goal": "SALES_PUBLISH",
        "goal_status": "WAITING_INPUT", "pending_interaction": {"kind": "ENTER_PRICE"}, "user_id": "u1", "user_role": "PRODUCER",
    })
    assert (rec.intent, rec.workflow, rec.workflow_step, rec.goal_status) == ("SALES_PUBLISH", "SALES_PUBLISH", "ENTER_PRICE", "WAITING_INPUT")
    assert rec.intent_confidence == pytest.approx(0.93) and rec.outcome == "WAITING_USER" and rec.user_role == "PRODUCER"


def test_hooks_are_noops_outside_a_turn_and_never_raise():
    tt.note_final({"status": "ERROR"})
    tt.note_error("X", "TOOL")
    tt.note_tool("t", "READ", datetime.now(timezone.utc), 0.01, "SUCCESS")
    tt.note_llm(name="x", model="m", provider=None, profile=None, agent_node=None, latency_s=0.1, usage=None, error=None, fallback_from=None)
    tt.note_final("pas un dict")  # entrée absurde


# ── LLM : classification + double comptage gateway/adapter ──────────────────

def _llm(name="llm_gateway_completion", model="llama", profile="INTERPRETER", latency=0.5, error=None, fb=None, node=None):
    tt.note_llm(name=name, model=model, provider="groq", profile=profile, agent_node=node, latency_s=latency,
                usage={"prompt_tokens": 10, "completion_tokens": 5}, error=error, fallback_from=fb)


def test_gateway_event_replaces_the_adapter_event_of_the_same_call():
    rec = tt.TurnRecorder(phone="+22670000000")
    tt.begin_turn(rec)
    _llm(name="groq_completion", profile=None)      # émis en premier par l'adaptateur
    _llm(name="llm_gateway_completion")             # puis par le gateway, pour le MÊME appel
    assert len(rec.llms) == 1 and rec.llms[0].source == "gateway" and rec.llms[0].kind == "INTERPRETER"


def test_interpreter_vs_response_and_fallback_are_distinguished():
    rec = tt.TurnRecorder(phone="+22670000000")
    tt.begin_turn(rec)
    _llm(profile="INTERPRETER", latency=0.4)
    _llm(profile="REASONING", latency=0.8, model="nova", fb="groq:llama")
    _llm(profile="REASONING", latency=0.1, error="429", model="x")
    assert rec.llm_ms("INTERPRETER") == 400 and rec.llm_ms("RESPONSE") == 900
    assert sum(c.is_fallback for c in rec.llms) == 1 and rec.llms[2].status == "ERROR"


# ── SQL / Redis ──────────────────────────────────────────────────────────────

def test_sql_listener_counts_queries_without_capturing_statement_or_params():
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    tt.attach_sql_listeners(engine)
    rec = tt.TurnRecorder(phone="+22670000000")
    with engine.connect() as c:
        c.execute(text("select 1"))                 # hors tour : non compté
        tt.begin_turn(rec)
        c.execute(text("select :secret"), {"secret": "mot-de-passe"})
        c.execute(text("select 2"))
    assert rec.sql_count == 2 and rec.sql_max_ms <= rec.sql_ms
    assert not any("mot-de-passe" in str(v) for v in vars(rec).values())


def test_redis_wrapper_counts_commands_inside_a_turn_only(monkeypatch):
    import redis.client as rc

    monkeypatch.setattr(tt, "_patched_redis", False)
    monkeypatch.setattr(rc.Redis, "execute_command", lambda self, *a, **k: "OK")
    tt.install_redis_instrumentation()
    r = rc.Redis()
    assert r.execute_command("GET", "k") == "OK"     # hors tour
    rec = tt.TurnRecorder(phone="+22670000000")
    tt.begin_turn(rec)
    assert r.execute_command("SET", "k", "v") == "OK" and r.execute_command("GET", "k") == "OK"
    assert rec.redis_count == 2


# ── File Celery ──────────────────────────────────────────────────────────────

def test_queue_duration_comes_from_the_publish_header():
    req = SimpleNamespace(**{tt.ENQUEUED_AT_HEADER: time.time() - 1.5})
    enq = tt.enqueued_at_from_request(req)
    rec = tt.TurnRecorder(phone="+22670000000", enqueued_at=enq)
    assert 1400 <= rec.queue_duration_ms <= 2500
    assert tt.enqueued_at_from_request(SimpleNamespace(headers={tt.ENQUEUED_AT_HEADER: 5.0})) == 5.0
    assert tt.enqueued_at_from_request(SimpleNamespace()) is None


# ── GARANTIE : la télémétrie ne casse jamais le métier ──────────────────────

class TestObservabilityNeverBreaksTheTurn:
    def test_a_persistence_error_is_swallowed_counted_and_returns_false(self, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("DB de télémétrie indisponible")

        monkeypatch.setattr(tt, "_persist", boom)
        before = tt.write_failures()
        assert asyncio.run(tt.finish_and_persist(tt.TurnRecorder(phone="+22670000000"))) is False
        assert tt.write_failures() == before + 1

    def test_a_slow_write_is_cut_by_the_timeout(self, monkeypatch):
        async def slow(*a, **k):
            await asyncio.sleep(5)

        monkeypatch.setattr(tt, "_persist", slow)
        monkeypatch.setattr(_settings, "AGENT_MONITORING_WRITE_TIMEOUT_SECONDS", 0.05, raising=False)
        t0 = time.perf_counter()
        assert asyncio.run(tt.finish_and_persist(tt.TurnRecorder(phone="+22670000000"))) is False
        assert time.perf_counter() - t0 < 1.0

    def test_disabled_monitoring_writes_nothing(self, monkeypatch):
        called = []

        async def spy(*a, **k):
            called.append(1)

        monkeypatch.setattr(tt, "_persist", spy)
        monkeypatch.setattr(_settings, "AGENT_MONITORING_ENABLED", False, raising=False)
        assert asyncio.run(tt.finish_and_persist(tt.TurnRecorder(phone="+22670000000"))) is False and not called

    def test_the_turn_context_is_closed_before_writing(self, monkeypatch):
        seen = {}

        async def spy(rec, *a):
            seen["ctx"] = tt.current()

        monkeypatch.setattr(tt, "_persist", spy)
        rec = tt.TurnRecorder(phone="+22670000000")
        tt.begin_turn(rec)
        assert asyncio.run(tt.finish_and_persist(rec)) is True
        assert seen["ctx"] is None  # les requêtes d'écriture de la télémétrie ne se comptent pas elles-mêmes


# ── MCP : le wrapper mesure sans modifier résultat ni exceptions ────────────

class TestMcpWrapper:
    def _server(self, monkeypatch, inner):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer

        srv = AgriDBMCPServer.__new__(AgriDBMCPServer)
        monkeypatch.setattr(AgriDBMCPServer, "_call_tool_inner", inner)
        return srv

    def test_success_is_recorded_and_result_unchanged(self, monkeypatch):
        async def inner(self, name, arguments=None, **kw):
            return {"ok": True}

        rec = tt.TurnRecorder(phone="+22670000000")
        tt.begin_turn(rec)
        assert asyncio.run(self._server(monkeypatch, inner).call_tool("search_products", {"q": "x"})) == {"ok": True}
        assert [(t.tool_name, t.status, t.tool_category) for t in rec.tools] == [("search_products", "SUCCESS", "READ")]

    def test_errors_and_denials_propagate_unchanged_and_are_recorded(self, monkeypatch):
        from ladini.infrastructure.mcp.security import PermissionDenied

        async def inner_err(self, name, arguments=None, **kw):
            raise ValueError("boom")

        async def inner_deny(self, name, arguments=None, **kw):
            raise PermissionDenied(name, "nope")

        rec = tt.TurnRecorder(phone="+22670000000")
        tt.begin_turn(rec)
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(self._server(monkeypatch, inner_err).call_tool("create_order", {}))
        with pytest.raises(PermissionDenied):
            asyncio.run(self._server(monkeypatch, inner_deny).call_tool("create_order", {}))
        assert [(t.status, t.error_category, t.tool_category) for t in rec.tools] == [("ERROR", "TOOL", "WRITE"), ("DENIED", "SECURITY", "WRITE")]

    def test_a_telemetry_failure_never_breaks_a_tool(self, monkeypatch):
        async def inner(self, name, arguments=None, **kw):
            return "résultat"

        monkeypatch.setattr(tt, "note_tool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("télémétrie cassée")))
        assert asyncio.run(self._server(monkeypatch, inner).call_tool("search_products", {})) == "résultat"


# ── Diagnostic : la télémétrie arrive-t-elle dans la bonne base ? ───────────

class TestStartupDiagnostic:
    def test_describe_database_never_leaks_credentials(self, monkeypatch):
        monkeypatch.setattr(_settings, "DATABASE_URL", "postgres://user:s3cr3t@db.example.com:6543/ladini?sslmode=require", raising=False)
        out = tt.describe_database()
        assert out == "db.example.com:6543/ladini" and "s3cr3t" not in out and "user" not in out

    def _run(self, monkeypatch, scalar=None, error=None):
        import ladini.core.database as dbmod

        class _Res:
            def scalar(self_inner):
                return scalar

        class _Sess:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def execute(self_inner, *a, **k):
                if error:
                    raise error
                return _Res()

        monkeypatch.setattr(dbmod, "get_sessionmaker", lambda: (lambda: _Sess()))
        return asyncio.run(tt.startup_check())

    def test_present_tables(self, monkeypatch):
        assert self._run(monkeypatch, scalar=True)["tables_present"] is True

    def test_missing_tables_are_reported_loudly_with_the_remedy(self, monkeypatch, caplog):
        with caplog.at_level("ERROR", logger="ladini.turn_telemetry"):
            res = self._run(monkeypatch, scalar=False)
        assert res["tables_present"] is False
        assert "migration Drizzle 0001" in caplog.text and "ABSENTES" in caplog.text

    def test_a_broken_database_never_raises(self, monkeypatch):
        res = self._run(monkeypatch, error=ConnectionError("down"))
        assert res["tables_present"] is None and res["error"] == "ConnectionError"

    def test_disabled_monitoring_skips_the_check(self, monkeypatch):
        monkeypatch.setattr(_settings, "AGENT_MONITORING_ENABLED", False, raising=False)
        assert asyncio.run(tt.startup_check())["enabled"] is False
