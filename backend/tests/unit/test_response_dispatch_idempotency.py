"""`api/response_dispatch.py::claim_response_item` — garde d'idempotence de
l'ENVOI (mandat de consolidation finale, puis durcissement architectural
2026-09-03) : un retry Celery (`process_agent_task`, `send_confirmation_text`,
ou une tâche `product_photo_task.py`) APRÈS un envoi déjà réussi ne doit
jamais produire un second message. Distincte du dédoublonnage webhook
(`msg:{MessageSid}`, qui protège contre une redelivery HTTP de Twilio/Meta,
une couche différente) — même IDENTITÉ (`message_sid`/`event_id`) réutilisée.

Depuis le durcissement 2026-09-03, `claim_response_item` délègue à
`core/idempotency.py::claim_once` (préfixe de clé `resp:`) — LA SEULE
implémentation SET-NX-EX du dépôt, partagée avec
`domain/procurement_draft.py` (préfixe `procurement_confirm:`). Le double
Redis client (`api/response_dispatch.py` vs `core/idempotency.py`) a été
supprimé — ces tests monkeypatchent donc le client PARTAGÉ."""
from __future__ import annotations

from agriconnect.api.response_dispatch import claim_response_item


class _FakeRedis:
    def __init__(self):
        self._store: dict = {}

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True


class TestClaimResponseItem:
    def test_the_first_claim_for_an_item_key_succeeds(self, monkeypatch):
        import agriconnect.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        assert claim_response_item("SMxxx1:0") is True

    def test_a_second_claim_for_the_same_item_key_fails(self, monkeypatch):
        """LE cas central : un retry Celery de la même tâche (même
        `event_id:index`) après un envoi déjà réussi ne doit jamais réclamer
        le droit d'envoyer une seconde fois."""
        import agriconnect.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        assert claim_response_item("SMxxx2:0") is True
        assert claim_response_item("SMxxx2:0") is False

    def test_different_item_keys_each_get_their_own_claim(self, monkeypatch):
        import agriconnect.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        assert claim_response_item("SMxxxA:0") is True
        assert claim_response_item("SMxxxA:1") is True  # 2e item du MÊME event

    def test_a_missing_item_key_fails_open(self, monkeypatch):
        """Un appelant qui ne fournit pas d'identité (ex: ancien tour hors
        webhook) ne doit jamais être bloqué par cette garde — mieux vaut un
        doublon rarissime qu'une réponse jamais envoyée."""
        import agriconnect.core.idempotency as idempotency_mod

        monkeypatch.setattr(idempotency_mod, "_client", _FakeRedis())
        assert claim_response_item(None) is True
        assert claim_response_item("") is True

    def test_a_redis_outage_fails_open_rather_than_blocking_every_response(
        self, monkeypatch
    ):
        import agriconnect.core.idempotency as idempotency_mod

        class _BrokenRedis:
            def set(self, *a, **k):
                raise ConnectionError("redis down")

        monkeypatch.setattr(idempotency_mod, "_client", _BrokenRedis())
        assert claim_response_item("SMxxxC:0") is True

    def test_the_key_carries_the_resp_namespace_prefix(self, monkeypatch):
        """Preuve que `claim_response_item` et
        `domain/procurement_draft.py`'s confirmation claim partagent LA
        MÊME primitive mais des espaces de clés distincts (mandat §5) — pas
        deux mécanismes, un mécanisme + deux préfixes."""
        import agriconnect.core.idempotency as idempotency_mod

        seen_keys = []

        class _RecordingRedis:
            def set(self, key, value, ex=None, nx=None):
                seen_keys.append(key)
                return True

        monkeypatch.setattr(idempotency_mod, "_client", _RecordingRedis())
        claim_response_item("SMxxxD:0")
        assert seen_keys == ["resp:SMxxxD:0"]
