"""Test d'architecture — mandat de consolidation finale : `ResponseDispatcher`
(api/response_dispatch.py) est le SEUL point d'autorité pour l'envoi d'une
réponse réactive, TEXTE et MÉDIA confondus.

Couvre :
  1. Anti-bypass structurel : aucun module métier (nodes/, flows/, domain/,
     interpreter/) n'appelle un transport Twilio/WhatsApp directement.
  2. Media retry : un item déjà envoyé (`ImageResponse`) n'est jamais
     renvoyé sur un second `dispatch()` avec le même `event_id`.
  3. Multipart texte + image : les deux partent au premier dispatch ; un
     retry ne renvoie NI L'UN NI L'AUTRE (chaque item a sa propre clé
     `event_id:index`, pas une seule clé par plan)."""
from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import run


class TestNoBusinessModuleSendsDirectly:
    """§22/§1 — sweep de code source : `Client(...).messages.create`,
    `send_whatsapp_media`, `send_whatsapp_message` ne doivent apparaître que
    dans `api/response_dispatch.py` (le dispatcher) et
    `services/twilio_sender.py`/`services/whatsapp/` (transport primitives,
    appelées UNIQUEMENT par le dispatcher) — jamais dans un node/flow/domain."""

    _SRC = Path(__file__).resolve().parents[2] / "src" / "ladini"
    _FORBIDDEN_DIRS = ("graphs/agents/market_coach/nodes", "graphs/agents/market_coach/flows",
                       "graphs/agents/market_coach/domain", "graphs/agents/market_coach/interpreter")
    _SEND_PATTERNS = re.compile(r"\.messages\.create\(|send_whatsapp_media\(|send_whatsapp_message\(")

    def test_no_node_flow_domain_or_interpreter_file_calls_a_transport_primitive(self):
        violations = []
        for rel_dir in self._FORBIDDEN_DIRS:
            base = self._SRC / rel_dir
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for match in self._SEND_PATTERNS.finditer(text):
                    violations.append(f"{path.relative_to(self._SRC)}:{match.start()}")
        assert violations == [], (
            "Un module métier appelle un transport directement — doit passer "
            f"par ResponseDispatcher : {violations}"
        )


class _FakeRedis:
    def __init__(self):
        self._store: dict = {}

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    def delete(self, key):
        self._store.pop(key, None)


class TestMediaSharesTheSameIdempotenceAsText:
    def test_a_retry_of_the_same_event_does_not_resend_an_already_sent_image(
        self, monkeypatch
    ):
        import ladini.api.response_dispatch as mod
        import ladini.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        sent_media = []
        monkeypatch.setattr(
            mod,
            "_send_image_via_twilio",
            lambda phone, item: sent_media.append(item.url) or {"status": "message_sent"},
        )

        dispatcher = mod.ResponseDispatcher()
        plan = mod.ResponsePlan(
            event_id="SM-MEDIA-RETRY",
            items=(mod.ImageResponse(url="https://x/a.jpg"),),
        )

        run(dispatcher.dispatch("+22670000001", plan))
        run(dispatcher.dispatch("+22670000001", plan))  # simule un retry Celery

        assert sent_media == ["https://x/a.jpg"], "l'image ne doit partir qu'UNE fois"

    def test_a_text_and_image_multipart_plan_sends_both_but_a_retry_sends_neither(
        self, monkeypatch
    ):
        import ladini.api.response_dispatch as mod
        import ladini.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        monkeypatch.setattr(mod.settings, "MESSAGING_PROVIDER", "twilio", raising=False)
        sent_text = []
        sent_media = []
        monkeypatch.setattr(
            mod,
            "_send_via_twilio",
            lambda phone, text, result: sent_text.append(text) or {"status": "message_sent"},
        )
        monkeypatch.setattr(
            mod,
            "_send_image_via_twilio",
            lambda phone, item: sent_media.append(item.url) or {"status": "message_sent"},
        )

        dispatcher = mod.ResponseDispatcher()
        plan = mod.ResponsePlan(
            event_id="SM-MULTIPART",
            items=(
                mod.TextResponse(text="Voici votre commande"),
                mod.ImageResponse(url="https://x/receipt.jpg"),
            ),
        )

        run(dispatcher.dispatch("+22670000001", plan))
        assert sent_text == ["Voici votre commande"]
        assert sent_media == ["https://x/receipt.jpg"]

        # Retry Celery du MÊME event_id — chaque item a sa PROPRE clé
        # (event_id:0, event_id:1), donc aucun des deux ne repart.
        run(dispatcher.dispatch("+22670000001", plan))
        assert sent_text == ["Voici votre commande"], "le texte n'a pas dû repartir"
        assert sent_media == ["https://x/receipt.jpg"], "l'image n'a pas dû repartir"

    def test_ten_concurrent_dispatches_of_the_same_media_item_yield_one_sender(
        self, monkeypatch
    ):
        """§8 du mandat — 10 workers concurrents sur LE MÊME plan média : un
        seul gagnant envoie réellement l'image."""
        import threading

        import ladini.api.response_dispatch as mod
        import ladini.core.idempotency as idempotency_mod

        class _ThreadSafeFakeRedis:
            def __init__(self):
                self._store: dict = {}
                self._lock = threading.Lock()

            def set(self, key, value, ex=None, nx=None):
                with self._lock:
                    if nx and key in self._store:
                        return None
                    self._store[key] = value
                    return True

        monkeypatch.setattr(idempotency_mod, "_client", _ThreadSafeFakeRedis())

        claims = []
        lock = threading.Lock()

        def _record_claim(item_key):
            result = mod.claim_response_item(item_key)
            with lock:
                claims.append(result)
            return result

        barrier = threading.Barrier(10)

        def _worker():
            barrier.wait()
            _record_claim("SM-CONCURRENT-MEDIA:0")

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert sum(1 for c in claims if c is True) == 1
        assert sum(1 for c in claims if c is False) == 9


class TestAFailedSendReleasesTheClaimForARealRetry:
    """Incident réel (2026-09-15) : un `WhatsAppCloudAPIError('(#131005)
    Access denied')` sur le PREMIER envoi laissait la réclamation posée
    (avant l'envoi) sans jamais la libérer — le retry Celery suivant du
    MÊME `event_id:index` la trouvait déjà prise et abandonnait l'item comme
    `duplicate_suppressed`, sans qu'aucun message n'ait jamais réellement
    été envoyé. Miroir exact de `TestMediaSharesTheSameIdempotenceAsText`
    ci-dessus, mais pour le chemin d'ÉCHEC : un retry après échec DOIT
    pouvoir réessayer réellement l'envoi."""

    def test_a_failed_whatsapp_cloud_send_lets_the_retry_actually_resend(
        self, monkeypatch
    ):
        import ladini.api.response_dispatch as mod
        import ladini.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        monkeypatch.setattr(mod.settings, "MESSAGING_PROVIDER", "whatsapp_cloud", raising=False)

        attempts = []

        async def _flaky_send(phone, text, result):
            attempts.append(text)
            if len(attempts) == 1:
                raise RuntimeError("WhatsAppCloudAPIError('(#131005) Access denied')")
            return {"status": "message_sent"}

        monkeypatch.setattr(mod, "_send_via_whatsapp_cloud", _flaky_send)

        dispatcher = mod.ResponseDispatcher()
        plan = mod.ResponsePlan(
            event_id="WA-RETRY-AFTER-FAILURE",
            items=(mod.TextResponse(text="Bonjour !"),),
        )

        # Premier essai : l'envoi échoue, l'exception se propage (Celery la
        # retry naturellement) — la réclamation ne doit PAS rester posée.
        import pytest

        with pytest.raises(RuntimeError):
            run(dispatcher.dispatch("+22670000001", plan))
        assert attempts == ["Bonjour !"]

        # Retry Celery du MÊME event_id:index — doit réellement RETENTER
        # l'envoi, jamais être abandonné comme "déjà envoyé".
        results = run(dispatcher.dispatch("+22670000001", plan))
        assert attempts == ["Bonjour !", "Bonjour !"], "le retry doit réellement renvoyer"
        assert results[0]["status"] == "message_sent"

    def test_a_successful_send_still_blocks_a_genuine_duplicate_retry(
        self, monkeypatch
    ):
        """Non-régression : la libération ne doit jamais s'appliquer à un
        envoi qui a RÉUSSI — seul l'échec libère la réclamation."""
        import ladini.api.response_dispatch as mod
        import ladini.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        monkeypatch.setattr(mod.settings, "MESSAGING_PROVIDER", "whatsapp_cloud", raising=False)

        attempts = []

        async def _always_succeeds(phone, text, result):
            attempts.append(text)
            return {"status": "message_sent"}

        monkeypatch.setattr(mod, "_send_via_whatsapp_cloud", _always_succeeds)

        dispatcher = mod.ResponseDispatcher()
        plan = mod.ResponsePlan(
            event_id="WA-SUCCESS-THEN-RETRY",
            items=(mod.TextResponse(text="Bonjour !"),),
        )

        run(dispatcher.dispatch("+22670000001", plan))
        run(dispatcher.dispatch("+22670000001", plan))  # simule un retry Celery

        assert attempts == ["Bonjour !"], "un envoi réussi ne doit jamais repartir"
