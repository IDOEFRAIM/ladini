"""Test d'architecture — propriété centrale du mandat de clôture (§22) :

    UN événement entrant → AU PLUS UNE réponse réactive utilisateur.

Deux couches sont vérifiées ici :
  1. `claim_response_item` (api/response_dispatch.py — SEULE implémentation
     de la garde depuis la consolidation) est atomique sous concurrence
     réelle (§7) — le SEUL winner parmi N appelants concurrents sur la MÊME
     clé.
  2. Le dédoublonnage webhook (`msg:{MessageSid}`, Redis SETNX) rejette une
     redelivery du MÊME MessageSid — vérifié directement sur
     `_handle_twilio_webhook` (pas une réimplémentation du mécanisme).

NB : `api/tasks.py`/`twilio_webhook.py` importent `celery` — non collectable
sans le paquet (pré-existant, sans rapport avec cette feature)."""
from __future__ import annotations

import threading
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from tests.conftest import run

pytest.importorskip("celery", reason="modules importent celery")

PHONE = "+22670000001"


class _ThreadSafeFakeRedis:
    """Simule l'atomicité RÉELLE de `SET key val NX` — Redis est
    mono-thread côté serveur, donc `SETNX` est intrinsèquement atomique même
    sous accès concurrents ; ce fake reproduit cette garantie avec un
    `threading.Lock` pour que le test soit un exercice honnête de la
    logique Python autour de l'appel, pas une supposition non vérifiée."""

    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def set(self, key, value, ex=None, nx=None):
        with self._lock:
            if nx and key in self._store:
                return None
            self._store[key] = value
            return True


class TestClaimSingleResponseIsAtomicUnderConcurrency:
    def test_ten_concurrent_claims_on_the_same_event_yield_exactly_one_winner(
        self, monkeypatch
    ):
        import agriconnect.api.response_dispatch as tasks_mod
        import agriconnect.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _ThreadSafeFakeRedis())

        results: list = [None] * 10
        barrier = threading.Barrier(10)

        def _claim(i):
            barrier.wait()  # maximise le chevauchement réel des 10 appels
            results[i] = tasks_mod.claim_response_item("SM-CONCURRENT-EVENT")

        threads = [threading.Thread(target=_claim, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        winners = sum(1 for r in results if r is True)
        rejected = sum(1 for r in results if r is False)
        assert winners == 1, f"attendu exactement 1 winner, obtenu {winners}"
        assert rejected == 9, f"attendu exactement 9 rejetés, obtenu {rejected}"

    def test_two_different_events_each_get_their_own_independent_winner(
        self, monkeypatch
    ):
        import agriconnect.api.response_dispatch as tasks_mod
        import agriconnect.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _ThreadSafeFakeRedis())

        assert tasks_mod.claim_response_item("SM-EVENT-A") is True
        assert tasks_mod.claim_response_item("SM-EVENT-B") is True
        # Un retry de CHACUN reste rejeté indépendamment.
        assert tasks_mod.claim_response_item("SM-EVENT-A") is False
        assert tasks_mod.claim_response_item("SM-EVENT-B") is False


class _FakeRequest:
    def __init__(self, form: dict):
        self._form = form

        class _State:
            trace_id = None

        self.state = _State()

    async def form(self):
        return self._form


class TestWebhookDuplicateDeliveryProcessesOnce:
    def test_the_same_message_sid_delivered_twice_is_only_processed_once(
        self, monkeypatch
    ):
        """§5/§6 du mandat de clôture : la MÊME MessageSid livrée deux fois
        (redelivery Twilio réelle, ou deux workers qui la reçoivent tous les
        deux) ne doit déclencher qu'UN SEUL traitement — la seconde livraison
        doit court-circuiter avant tout enqueue Celery."""
        import agriconnect.api.routes.twilio_webhook as webhook_mod

        # Vrai SETNX en mémoire (mono-thread ici, suffisant pour prouver la
        # LOGIQUE — l'atomicité de Redis lui-même n'est pas ce qui est testé).
        store: dict = {}

        def _fake_set(key, value, ex=None, nx=None):
            if nx and key in store:
                return None
            store[key] = value
            return True

        monkeypatch.setattr(webhook_mod.redis_client, "set", _fake_set)
        monkeypatch.setattr(
            webhook_mod.WorkspaceStore,
            "get",
            AsyncMock(return_value=None),
        )
        delay_mock = Mock()
        monkeypatch.setattr(webhook_mod.process_agent_task, "delay", delay_mock)

        form = {
            "From": f"whatsapp:{PHONE}",
            "Body": "bonjour",
            "MessageSid": "SM-DUPLICATE-TEST",
        }

        run(
            webhook_mod._handle_twilio_webhook(
                _FakeRequest(form), None, f"whatsapp:{PHONE}", "bonjour",
                "SM-DUPLICATE-TEST",
            )
        )
        run(
            webhook_mod._handle_twilio_webhook(
                _FakeRequest(form), None, f"whatsapp:{PHONE}", "bonjour",
                "SM-DUPLICATE-TEST",
            )
        )

        delay_mock.assert_called_once()
