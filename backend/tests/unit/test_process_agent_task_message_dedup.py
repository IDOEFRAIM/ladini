"""Dédoublonnage GLOBAL par `message_sid` au niveau de `process_agent_task`
(2026-09-12) — distinct du dédoublonnage webhook (`msg:{MessageSid}`,
empêche seulement un double ENQUEUE) et du cache LLM de l'interpréteur
(empêche seulement un double APPEL LLM). Ce garde-fou empêche tout le
PIPELINE MÉTIER (executor + envoi WhatsApp) de s'exécuter deux fois pour
le même message.

Ces tests monkeypatchent `claim_once`/`get_cached`/`set_cached`/`release`
tels qu'importés dans `ladini.api.tasks` par un magasin en mémoire — la
logique testée est le COMPORTEMENT de dédoublonnage (clé dérivée de
`message_sid`, séquence claim → traitement → done/release), pas la
disponibilité réelle de Redis (déjà couverte séparément, voir
`test_redis_unavailable_still_processes_the_message` ci-dessous, qui
utilise volontairement les VRAIES primitives — fail-open par construction
sans Redis joignable en environnement de test)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Dict, Set

import pytest


class _FakeIdempotencyStore:
    """SETNX + get/set + delete en mémoire — même contrat que
    `core/idempotency.py` (claim_once retourne True la 1ère fois, False
    ensuite ; release() retire la réclamation)."""

    def __init__(self) -> None:
        self._claims: Set[str] = set()
        self._values: Dict[str, str] = {}

    def claim_once(self, key, *, ttl_seconds: int = 3600) -> bool:
        if not key:
            return True
        if key in self._claims:
            return False
        self._claims.add(key)
        return True

    def get_cached(self, key):
        if not key:
            return None
        return self._values.get(key)

    def set_cached(self, key, value, *, ttl_seconds: int = 3600) -> None:
        if key:
            self._values[key] = value

    def release(self, key) -> None:
        self._claims.discard(key)


@pytest.fixture()
def tasks_mod(monkeypatch):
    import ladini.api.response_dispatch as dispatch_mod
    import ladini.api.tasks as mod

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(mod, "_loop", loop, raising=False)
    monkeypatch.setattr(dispatch_mod.settings, "MESSAGING_PROVIDER", "twilio", raising=False)
    monkeypatch.setattr(
        dispatch_mod, "_send_via_twilio",
        lambda phone, text, result: {"status": "message_sent", "sid": "SM_OUT"},
    )
    try:
        yield mod
    finally:
        loop.close()


@pytest.fixture()
def fake_store(monkeypatch):
    import ladini.api.tasks as mod

    store = _FakeIdempotencyStore()
    monkeypatch.setattr(mod, "claim_once", store.claim_once)
    monkeypatch.setattr(mod, "get_cached", store.get_cached)
    monkeypatch.setattr(mod, "set_cached", store.set_cached)
    monkeypatch.setattr(mod, "release", store.release)
    return store


def _handle_counter(tasks_mod, monkeypatch, response=None, error: Exception | None = None):
    """Installe un `_orchestrator.handle` factice qui compte ses appels et,
    optionnellement, lève sur le Nème appel (pour simuler un échec DB/MCP
    aval après une interprétation déjà réussie)."""
    calls = {"n": 0}

    async def _fake_handle(*args, **kwargs):
        calls["n"] += 1
        if error is not None and calls["n"] == 1:
            raise error
        return response or {"final_response": "Réponse", "agent": "market"}

    monkeypatch.setattr(
        tasks_mod, "_orchestrator", SimpleNamespace(handle=_fake_handle), raising=False
    )
    return calls


class TestSameMessageSidProcessedOnce:
    def test_same_message_sid_twice_only_runs_business_logic_once(
        self, tasks_mod, fake_store, monkeypatch
    ):
        calls = _handle_counter(tasks_mod, monkeypatch)

        first = tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_DUP_1"
        )
        second = tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_DUP_1"
        )

        assert calls["n"] == 1
        assert first == [{"status": "message_sent", "sid": "SM_OUT"}]
        assert second == [{"status": "duplicate_skipped", "reason": "already_completed"}]

    def test_concurrent_duplicate_before_completion_is_also_skipped(
        self, tasks_mod, fake_store, monkeypatch
    ):
        """Un 2e worker qui recevrait le MÊME message_sid PENDANT que le 1er
        est encore en cours (claim posé, `done` pas encore écrit) doit aussi
        être sauté — pas seulement après complétion."""
        calls = _handle_counter(tasks_mod, monkeypatch)
        # Simule "déjà en cours" : le claim est posé mais jamais libéré/marqué done.
        fake_store.claim_once("task_claim:SM_INFLIGHT")

        result = tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_INFLIGHT"
        )

        assert calls["n"] == 0
        assert result == [{"status": "duplicate_skipped", "reason": "already_processing"}]


class TestDifferentMessageSidsEachProcessed:
    def test_two_distinct_message_sids_both_run_business_logic(
        self, tasks_mod, fake_store, monkeypatch
    ):
        calls = _handle_counter(tasks_mod, monkeypatch)

        tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_A"
        )
        tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_B"
        )

        assert calls["n"] == 2


class TestRetryableErrorReleasesTheClaim:
    def test_a_failure_releases_the_claim_so_the_retry_can_reprocess(
        self, tasks_mod, fake_store, monkeypatch
    ):
        """1er appel : l'orchestrateur échoue (panne transitoire) — l'exception
        doit continuer à se propager (pour `autoretry_for`) ET la
        réclamation doit être relâchée. 2e appel (= le retry Celery, même
        message_sid) : ne doit PAS être sauté comme un doublon — il doit
        réellement rejouer le pipeline."""
        calls = _handle_counter(
            tasks_mod, monkeypatch, error=ConnectionError("DB down transitoirement")
        )

        with pytest.raises(ConnectionError):
            tasks_mod.process_agent_task.run(
                phone_number="+22670000001", user_query="salut", message_sid="SM_RETRY"
            )
        assert calls["n"] == 1

        # "Retry Celery" = un second appel de la même tâche, même message_sid.
        result = tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_RETRY"
        )
        assert calls["n"] == 2  # le pipeline a bien REJOUÉ, pas été sauté
        assert result == [{"status": "message_sent", "sid": "SM_OUT"}]


class TestRedisUnavailableFailsOpen:
    def test_redis_unavailable_still_processes_the_message(self, tasks_mod, monkeypatch):
        """Utilise les VRAIES primitives de `core/idempotency.py` (pas de
        mock du magasin), avec `_get_client()` forcé à `None` — simulation
        DÉTERMINISTE d'un Redis injoignable (plus fiable qu'espérer un
        échec réseau réel contre l'URL de test). La politique fail-open déjà
        documentée dans `core/idempotency.py` doit laisser le traitement
        s'exécuter normalement, jamais bloquer un message par précaution."""
        import ladini.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_get_client", lambda: None)
        calls = _handle_counter(tasks_mod, monkeypatch)

        result = tasks_mod.process_agent_task.run(
            phone_number="+22670000001", user_query="salut", message_sid="SM_NO_REDIS"
        )

        assert calls["n"] == 1
        assert result == [{"status": "message_sent", "sid": "SM_OUT"}]
