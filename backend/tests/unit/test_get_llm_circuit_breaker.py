"""`core/get_llm.py::_CircuitBreaker` / `_GroqAdapter` fallback (2026-08-31,
incident AGENT_TIMEOUT 163s) — voir le commentaire d'incident en tête de
`get_llm.py` pour l'analyse complète.

Root cause : le timeout HTTP du client était PLUS LONG (20.0s) que le
`asyncio.wait_for` englobant (15.0s) aux sites d'appel — `wait_for` annule
la coroutine avant que le thread bloquant (`asyncio.to_thread`, exécuté dans
le pool partagé du process) n'ait pu se terminer via SON PROPRE timeout,
laissant un thread orphelin fuité à chaque appel lent. Sous dégradation
soutenue de la passerelle, ces fuites épuisent le pool partagé, transformant
un appel lent isolé en un gel de plusieurs dizaines de secondes.

Ces tests couvrent le disjoncteur + le repli inter-provider ajoutés en
réponse — sans réseau réel, avec des clients factices."""
from __future__ import annotations

from ladini.core.get_llm import (
    _CircuitBreaker,
    _CircuitOpenError,
    _GroqAdapter,
)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _FakeCompletions:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, response=None, raise_exc=None):
        self.completions = _FakeCompletions(response, raise_exc)
        self.chat = _FakeChat(self.completions)


class TestCircuitBreaker:
    def test_closed_by_default(self):
        cb = _CircuitBreaker(failure_threshold=3, cooldown_s=30.0)
        assert cb.is_open is False

    def test_opens_after_threshold_consecutive_failures(self):
        cb = _CircuitBreaker(failure_threshold=3, cooldown_s=30.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False
        cb.record_failure()
        assert cb.is_open is True

    def test_a_success_resets_the_failure_count(self):
        cb = _CircuitBreaker(failure_threshold=3, cooldown_s=30.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False  # only 2 consecutive since the reset

    def test_closes_again_after_cooldown_elapses(self, monkeypatch):
        # `import ladini.core.get_llm as get_llm_mod` is unreliable —
        # `ladini/core/__init__.py` re-exports the `get_llm` FUNCTION,
        # which shadows the submodule as a package attribute. See
        # `tests/unit/test_get_llm_bedrock_adapter.py` for the full
        # explanation. `importlib.import_module` bypasses it.
        import importlib

        get_llm_mod = importlib.import_module("ladini.core.get_llm")

        fake_time = {"now": 1000.0}
        monkeypatch.setattr(get_llm_mod.time, "monotonic", lambda: fake_time["now"])

        cb = _CircuitBreaker(failure_threshold=1, cooldown_s=10.0)
        cb.record_failure()
        assert cb.is_open is True

        fake_time["now"] += 10.1
        assert cb.is_open is False


class TestGroqAdapterFallback:
    def test_no_fallback_configured_re_raises_on_primary_failure(self):
        primary = _FakeClient(raise_exc=RuntimeError("primary down"))
        adapter = _GroqAdapter(primary)

        try:
            adapter.chat.completions.create(model="m", messages=[])
            assert False, "expected the primary exception to propagate"
        except RuntimeError as exc:
            assert "primary down" in str(exc)

    def test_falls_back_to_the_secondary_client_on_primary_failure(self):
        primary = _FakeClient(raise_exc=RuntimeError("primary down"))
        fallback = _FakeClient(response=_Completion("réponse de secours"))
        adapter = _GroqAdapter(primary, fallback_client=fallback)

        out = adapter.chat.completions.create(model="m", messages=[])

        assert out.choices[0].message.content == "réponse de secours"
        assert primary.completions.calls == 1
        assert fallback.completions.calls == 1

    def test_circuit_opens_after_repeated_primary_failures_then_bypasses_primary(self):
        primary = _FakeClient(raise_exc=RuntimeError("degraded gateway"))
        fallback = _FakeClient(response=_Completion("ok"))
        adapter = _GroqAdapter(primary, fallback_client=fallback)
        adapter._circuit = _CircuitBreaker(failure_threshold=2, cooldown_s=30.0)

        adapter.chat.completions.create(model="m", messages=[])
        assert primary.completions.calls == 1
        adapter.chat.completions.create(model="m", messages=[])
        assert primary.completions.calls == 2
        assert adapter._circuit.is_open is True

        # Circuit now open: primary must NOT be attempted again — straight
        # to the fallback client, no wasted network round-trip.
        out = adapter.chat.completions.create(model="m", messages=[])
        assert primary.completions.calls == 2, "primary was called despite an open circuit"
        assert out.choices[0].message.content == "ok"
        assert fallback.completions.calls == 3

    def test_circuit_open_without_a_fallback_fails_fast(self):
        primary = _FakeClient(raise_exc=RuntimeError("degraded gateway"))
        adapter = _GroqAdapter(primary)
        adapter._circuit = _CircuitBreaker(failure_threshold=1, cooldown_s=30.0)

        try:
            adapter.chat.completions.create(model="m", messages=[])
        except RuntimeError:
            pass
        assert adapter._circuit.is_open is True

        try:
            adapter.chat.completions.create(model="m", messages=[])
            assert False, "expected _CircuitOpenError"
        except _CircuitOpenError:
            pass
        # Still only the ONE attempt from before the circuit opened — the
        # second call must never touch the primary client at all.
        assert primary.completions.calls == 1

    def test_a_success_after_recorded_failures_resets_the_circuit(self):
        calls = {"n": 0}

        class _FlakyCompletions(_FakeCompletions):
            def create(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient")
                return _Completion("recovered")

        client = _FakeClient()
        client.completions = _FlakyCompletions()
        client.chat = _FakeChat(client.completions)
        fallback = _FakeClient(response=_Completion("fallback"))
        adapter = _GroqAdapter(client, fallback_client=fallback)

        # First call fails on primary, falls back — circuit records ONE
        # failure (below the default threshold of 3), stays closed.
        adapter.chat.completions.create(model="m", messages=[])
        assert adapter._circuit.is_open is False

        # Second call succeeds directly on the (now-recovered) primary.
        out = adapter.chat.completions.create(model="m", messages=[])
        assert out.choices[0].message.content == "recovered"
        assert calls["n"] == 2
