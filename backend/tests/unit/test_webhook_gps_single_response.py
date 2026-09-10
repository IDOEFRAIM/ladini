"""`api/routes/twilio_webhook.py`/`whatsapp_webhook.py` — le webhook n'a plus
AUCUNE autorité de réponse pour un événement GPS. Il persiste le fait brut
(`location_outcome`) et transmet — rien de plus.

Historique (2 itérations avant celle-ci) : le webhook envoyait une
notification immédiate pour `LOCATION_OUT_OF_ZONE`, PUIS enqueuait quand même
la tâche Celery normale — qui, si l'utilisateur était en étape GPS, produisait
SON PROPRE message via `gps_delivery_gate.py::resolve_gps_stage`. Deux
messages pour un événement. Un premier correctif a fait lire au webhook
`pending_interaction_kind` pour décider s'il devait se taire — un rustinage
qui laissait deux composants propriétaires du même droit de réponse.

Fix architectural final (2026-09-02, "un seul propriétaire de la réponse") :
le webhook ne décide plus JAMAIS d'envoyer un message pour un résultat GPS —
il ne fait que persister et transmettre `location_outcome`. Le SEUL endroit
qui peut en parler est le tour de graphe : `gps_delivery_gate.py` si une
étape GPS est active, sinon `nodes/clarification.py` (le "je n'ai personne
d'autre pour répondre" habituel du graphe) — voir
`tests/nodes/test_clarification_location_outcome.py` pour ce second cas.

NB : `twilio_webhook.py`/`whatsapp_webhook.py` importent `api.tasks`
(`from celery.signals import ...`) au niveau module — non collectable sans
le paquet `celery` (pré-existant, sans rapport avec cette feature)."""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from tests.conftest import run

pytest.importorskip("celery", reason="webhook module imports api.tasks -> celery.signals")

PHONE = "+22670000001"


class _FakeWorkspace:
    def __init__(self, workspace_type: str = "buyer"):
        self.metadata: Dict[str, Any] = {}
        self.workspace_type = workspace_type


class _FakeRequest:
    """Mock minimal de `starlette.Request` — seuls `.form()` (async) et
    `.state` sont lus par `_handle_twilio_webhook`."""

    def __init__(self, form: Dict[str, str]):
        self._form = form

        class _State:
            trace_id = None

        self.state = _State()

    async def form(self):
        return self._form


def _location_form(lat: float = 48.85, lon: float = 2.35) -> Dict[str, str]:
    return {
        "From": f"whatsapp:{PHONE}",
        "Body": "",
        "MessageSid": "SMxxxLOCATIONxxx",
        "Latitude": str(lat),
        "Longitude": str(lon),
        "NumMedia": "0",
    }


class TestTwilioWebhookNeverOwnsTheGpsResponse:
    async def _run(self, monkeypatch, *, outcome_name: str):
        import ladini.api.routes.twilio_webhook as webhook_mod
        from ladini.core.location import LocationOutcome

        monkeypatch.setattr(webhook_mod.redis_client, "set", lambda *a, **k: True)
        monkeypatch.setattr(
            webhook_mod,
            "persist_shared_location",
            AsyncMock(
                return_value=(
                    LocationOutcome(outcome_name),
                    "un message que le webhook ne doit plus jamais envoyer",
                )
            ),
        )
        monkeypatch.setattr(
            webhook_mod.WorkspaceStore,
            "get",
            AsyncMock(return_value=_FakeWorkspace()),
        )
        delay_mock = Mock()
        monkeypatch.setattr(webhook_mod.process_agent_task, "delay", delay_mock)

        request = _FakeRequest(_location_form())
        await webhook_mod._handle_twilio_webhook(
            request, None, f"whatsapp:{PHONE}", "", "SMxxxLOCATIONxxx"
        )
        return delay_mock

    def test_out_of_zone_never_triggers_a_webhook_side_send(self, monkeypatch):
        import ladini.api.routes.twilio_webhook as webhook_mod

        # Le webhook n'importe même plus de fonction d'envoi — la preuve la
        # plus directe qu'il ne PEUT plus décider d'une réponse GPS.
        assert not hasattr(webhook_mod, "send_confirmation_text")

        delay_mock = run(self._run(monkeypatch, outcome_name="LOCATION_OUT_OF_ZONE"))
        # La tâche Celery normale part toujours — c'est ELLE (via le graphe)
        # qui produira l'unique réponse, quel que soit le contexte.
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["location_outcome"] == "LOCATION_OUT_OF_ZONE"

    def test_accepted_location_also_only_enqueues_the_normal_task(self, monkeypatch):
        delay_mock = run(
            self._run(monkeypatch, outcome_name="NEW_LOCATION_ACCEPTED")
        )
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["location_outcome"] == "NEW_LOCATION_ACCEPTED"

    def test_persistence_error_also_only_enqueues_the_normal_task(self, monkeypatch):
        delay_mock = run(
            self._run(monkeypatch, outcome_name="LOCATION_PERSISTENCE_ERROR")
        )
        delay_mock.assert_called_once()
        assert (
            delay_mock.call_args.kwargs["location_outcome"]
            == "LOCATION_PERSISTENCE_ERROR"
        )
