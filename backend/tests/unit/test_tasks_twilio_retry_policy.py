"""`api/tasks.py::process_agent_task` — incident 2026-08-27.

Avant ce correctif, `autoretry_for=(Exception,)` faisait retenter TOUTE
exception, y compris une `TwilioRestException` 4xx (erreur de VALIDATION
CLIENT — corps trop long, template mal formé...) qui échoue EXACTEMENT de
la même façon à chaque tentative. Résultat observé en production : 3
tentatives Celery consommées pour rien sur un message de 2178 caractères,
rejouant tout le graphe de l'agent à chaque fois pour un problème que
seul un changement de CODE peut résoudre, jamais un retry.

Ces tests appellent `process_agent_task.run(...)` directement (contourne
la machinerie de retry Celery elle-même, comme les autres tâches de ce
dépôt — voir test_workers_crons_and_payments.py) pour vérifier le
comportement de la fonction : une 4xx doit être avalée (pas de retry), une
5xx/erreur transitoire doit continuer à se propager (retry légitime).

(2026-09-02, consolidation ResponseDispatcher) : `process_agent_task` reste
dans `api.tasks` mais délègue tout l'envoi à `api.response_dispatch` — cette
garde (4xx avalée, 5xx propagée) vit désormais dans
`ResponseDispatcher.dispatch`, appelée via `get_dispatcher()`. Ces tests
patchent donc `response_dispatch._send_via_twilio` (ce que le dispatcher
appelle réellement) tout en invoquant `tasks.process_agent_task` (le point
d'entrée inchangé)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.fixture()
def tasks_mod(monkeypatch):
    import agriconnect.api.response_dispatch as dispatch_mod
    import agriconnect.api.tasks as mod

    loop = asyncio.new_event_loop()

    async def _fake_handle(*args, **kwargs):
        return {"final_response": "Réponse courte", "agent": "market"}

    monkeypatch.setattr(mod, "_loop", loop, raising=False)
    monkeypatch.setattr(mod, "_orchestrator", SimpleNamespace(handle=_fake_handle), raising=False)
    monkeypatch.setattr(dispatch_mod.settings, "MESSAGING_PROVIDER", "twilio", raising=False)
    try:
        yield mod
    finally:
        loop.close()


@pytest.fixture()
def dispatch_mod():
    import agriconnect.api.response_dispatch as mod

    return mod


class TestProcessAgentTaskTwilioRetryPolicy:
    def test_a_4xx_twilio_error_is_swallowed_without_raising(
        self, tasks_mod, dispatch_mod, monkeypatch
    ):
        """L'erreur de validation (ex: 21617, corps trop long) ne doit
        JAMAIS remonter jusqu'à Celery — sinon `autoretry_for=(Exception,)`
        rejoue le tour en boucle pour un problème qui ne se résoudra pas."""
        from twilio.base.exceptions import TwilioRestException

        def _boom(phone, text, result):
            raise TwilioRestException(400, "uri", msg="body too long", code=21617)

        monkeypatch.setattr(dispatch_mod, "_send_via_twilio", _boom)

        out = tasks_mod.process_agent_task.run(phone_number="+22670000001", user_query="salut")
        assert out == [
            {"status": "message_failed", "reason": "twilio_client_error", "code": 21617}
        ]

    def test_a_5xx_twilio_error_still_propagates_for_celery_to_retry(
        self, tasks_mod, dispatch_mod, monkeypatch
    ):
        """Une panne Twilio réelle (5xx, transitoire) doit continuer à
        déclencher le retry Celery normal — seules les 4xx sont un cul-de-sac."""
        from twilio.base.exceptions import TwilioRestException

        def _boom(phone, text, result):
            raise TwilioRestException(500, "uri", msg="internal error", code=20500)

        monkeypatch.setattr(dispatch_mod, "_send_via_twilio", _boom)

        with pytest.raises(TwilioRestException):
            tasks_mod.process_agent_task.run(phone_number="+22670000001", user_query="salut")

    def test_a_successful_send_returns_its_result_untouched(
        self, tasks_mod, dispatch_mod, monkeypatch
    ):
        monkeypatch.setattr(
            dispatch_mod, "_send_via_twilio",
            lambda phone, text, result: {"status": "message_sent", "sid": "SM1"},
        )
        out = tasks_mod.process_agent_task.run(phone_number="+22670000001", user_query="salut")
        assert out == [{"status": "message_sent", "sid": "SM1"}]

    def test_a_non_twilio_exception_still_propagates_normally(
        self, tasks_mod, dispatch_mod, monkeypatch
    ):
        """Non-régression : le nouveau bloc try/except ne doit intercepter
        QUE TwilioRestException — toute autre erreur (réseau, bug interne)
        continue de se propager tel quel."""

        def _boom(phone, text, result):
            raise ConnectionError("réseau down")

        monkeypatch.setattr(dispatch_mod, "_send_via_twilio", _boom)

        with pytest.raises(ConnectionError):
            tasks_mod.process_agent_task.run(phone_number="+22670000001", user_query="salut")
