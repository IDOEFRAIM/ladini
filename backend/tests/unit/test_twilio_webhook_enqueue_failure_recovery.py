"""`api/routes/twilio_webhook.py` — P1-B : un webhook déjà marqué "reçu"
(claim Redis `msg:{MessageSid}`, posé en tête de `_handle_twilio_webhook`)
ne doit JAMAIS perdre silencieusement le message si l'enqueue Celery qui
suit (`process_agent_task.delay(...)`) échoue ou dépasse son budget temps.

## Le bug exact (avant ce correctif)

1. §1 pose `msg:{MessageSid}` (SETNX, TTL 3600s) — dédoublonnage webhook.
2. §6 appelle `process_agent_task.delay(...)`, borné par
   `asyncio.wait_for(..., timeout=_CELERY_DELAY_TIMEOUT_S)`.
3. Si CET appel lève (broker injoignable) ou dépasse son budget
   (`asyncio.TimeoutError`), l'exception remontait jusqu'au SEUL filet de
   sécurité du fichier — `twilio_webhook()` — `except Exception: return
   _empty_twiml()` — un 200 OK.
4. Twilio considère alors le message définitivement livré : AUCUNE
   redélivraison. Le claim posé en §1 reste actif jusqu'à son TTL (1h),
   bloquant même une redélivraison qui arriverait par un autre biais ou une
   nouvelle tentative manuelle. Le message disparaît sans réponse ni trace
   exploitable pendant jusqu'à 1h.

Même défaut, symptôme identique, sur la branche pause maintenance (§`is_
celery_producer_paused()`) : elle renvoyait déjà un 503 "Twilio redélivre"
(voir `core/maintenance.py`), mais SANS relâcher le claim posé en §1 —
rendant cette promesse illusoire : la redélivraison réelle tombait sur le
même claim "déjà en cours" et était droppée en silence par §1 lui-même.

## Le correctif

`_handle_twilio_webhook` capture maintenant localement toute exception issue
de l'enqueue (§6), relâche le claim (`_release_message_claim`), et renvoie
un 503 — Twilio redélivre, et cette redélivraison est maintenant traitée
comme un message NEUF plutôt que droppée par le dédoublonnage. Même
relâchement ajouté sur la branche maintenance (§ci-dessus).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

pytest.importorskip("celery", reason="webhook module imports api.tasks -> celery.signals")

import ladini.api.routes.twilio_webhook as twilio_webhook_module
from tests.harness.conversation import FakeRedis


class _FakeRequest:
    def __init__(self, form_data: Dict[str, Any]) -> None:
        self._form_data = form_data
        self.state = SimpleNamespace()

    async def form(self) -> Dict[str, Any]:
        return self._form_data


class _FakeBackgroundTasks:
    def add_task(self, *_a: Any, **_kw: Any) -> None:
        pass


class _FakeWorkspaceStore:
    """Aucun workspace existant — chemin le plus simple, sans toucher la DB."""

    async def get(self, _workspace_id: str) -> Optional[Any]:
        return None


def _wire_common_doubles(monkeypatch, *, delay_side_effect: Optional[BaseException] = None):
    fake_redis = FakeRedis()
    monkeypatch.setattr(twilio_webhook_module, "redis_client", fake_redis)
    monkeypatch.setattr(twilio_webhook_module, "is_celery_producer_paused", lambda: False)
    monkeypatch.setattr(
        twilio_webhook_module, "WorkspaceStore", lambda: _FakeWorkspaceStore()
    )

    delay_calls: list[Dict[str, Any]] = []

    def _fake_delay(**kwargs: Any) -> Any:
        delay_calls.append(kwargs)
        if delay_side_effect is not None:
            raise delay_side_effect
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(twilio_webhook_module.process_agent_task, "delay", _fake_delay)
    return fake_redis, delay_calls


async def _send(form_data: Dict[str, Any]):
    return await twilio_webhook_module._handle_twilio_webhook(
        _FakeRequest(form_data),
        _FakeBackgroundTasks(),
        form_data["From"],
        form_data.get("Body", ""),
        form_data["MessageSid"],
    )


_FORM = {
    "From": "whatsapp:+22670000001",
    "Body": "je veux vendre 50kg de maïs",
    "MessageSid": "SMtest0001",
}


class TestEnqueueFailureIsRecoverableNotSilentlyLost:
    @pytest.mark.asyncio
    async def test_a_broker_error_on_enqueue_releases_the_claim_and_asks_for_a_retry(
        self, monkeypatch
    ):
        fake_redis, _calls = _wire_common_doubles(
            monkeypatch, delay_side_effect=RuntimeError("broker unreachable")
        )

        response = await _send(_FORM)

        assert response.status_code == 503, (
            f"un échec d'enqueue doit demander une redélivraison Twilio (503), "
            f"pas un 200 qui déclare le message livré à tort : {response.status_code}"
        )
        assert fake_redis.exists(f"msg:{_FORM['MessageSid']}") == 0, (
            "le claim doit être relâché après un échec d'enqueue — sinon la "
            "redélivraison Twilio (déclenchée par ce 503) tombera sur "
            "'déjà en cours' et sera droppée en silence par le dédoublonnage"
        )

    @pytest.mark.asyncio
    async def test_a_timeout_on_enqueue_is_treated_the_same_as_a_hard_failure(
        self, monkeypatch
    ):
        import asyncio

        fake_redis, _calls = _wire_common_doubles(
            monkeypatch, delay_side_effect=asyncio.TimeoutError("slow broker")
        )

        response = await _send(_FORM)

        assert response.status_code == 503
        assert fake_redis.exists(f"msg:{_FORM['MessageSid']}") == 0

    @pytest.mark.asyncio
    async def test_a_redelivery_after_enqueue_failure_is_treated_as_a_fresh_message(
        self, monkeypatch
    ):
        """Bout en bout du scénario réel : 1er passage échoue (broker down),
        Twilio redélivre le MÊME MessageSid, le broker est réparé — cette 2e
        tentative doit RÉELLEMENT atteindre `process_agent_task.delay`, pas
        être droppée par le dédoublonnage du premier passage."""
        fake_redis, calls = _wire_common_doubles(
            monkeypatch, delay_side_effect=RuntimeError("broker unreachable")
        )
        first = await _send(_FORM)
        assert first.status_code == 503
        assert len(calls) == 1  # la tentative a bien eu lieu, elle a juste échoué

        # Le broker est réparé pour la redélivraison — on retire l'échec simulé.
        monkeypatch.setattr(
            twilio_webhook_module.process_agent_task,
            "delay",
            lambda **kwargs: (calls.append(kwargs), SimpleNamespace(id="task-2"))[1],
        )

        second = await _send(_FORM)

        assert len(calls) == 2, (
            "la redélivraison Twilio du même MessageSid doit réellement retenter "
            "l'enqueue — pas être avalée par le dédoublonnage laissé par le 1er échec"
        )
        assert second.status_code == 200

    @pytest.mark.asyncio
    async def test_a_successful_enqueue_keeps_the_claim_so_a_true_duplicate_is_still_deduped(
        self, monkeypatch
    ):
        """Non-régression : ce correctif ne doit RELÂCHER le claim QUE sur un
        échec d'enqueue — un succès doit continuer à protéger contre une
        VRAIE redélivraison Twilio du même message déjà traité avec succès."""
        fake_redis, calls = _wire_common_doubles(monkeypatch, delay_side_effect=None)

        response = await _send(_FORM)

        assert response.status_code == 200
        assert len(calls) == 1
        assert fake_redis.exists(f"msg:{_FORM['MessageSid']}") == 1, (
            "un enqueue réussi doit garder le claim (TTL 1h) — sinon une VRAIE "
            "redélivraison Twilio d'un message déjà traité serait retraitée"
        )

        # Une "redélivraison" arrivant APRÈS un succès doit être droppée avant
        # même de retenter l'enqueue.
        second = await _send(_FORM)
        assert len(calls) == 1, "un message déjà réussi ne doit jamais être ré-enqueué"
        assert second.status_code == 200


class TestMaintenancePauseAlsoReleasesTheClaim:
    @pytest.mark.asyncio
    async def test_pausing_the_broker_after_the_claim_was_taken_still_allows_a_retry(
        self, monkeypatch
    ):
        """Même défaut, déclencheur différent : la pause maintenance (§bascule
        broker) renvoyait déjà un 503 mais sans relâcher le claim posé en §1
        — la redélivraison Twilio qu'elle prétend permettre tombait alors sur
        'déjà en cours' et était droppée avant même de revérifier la pause."""
        fake_redis, calls = _wire_common_doubles(monkeypatch, delay_side_effect=None)
        monkeypatch.setattr(twilio_webhook_module, "is_celery_producer_paused", lambda: True)

        response = await _send(_FORM)

        assert response.status_code == 503
        assert len(calls) == 0
        assert fake_redis.exists(f"msg:{_FORM['MessageSid']}") == 0, (
            "le claim doit être relâché pendant la pause aussi — sinon la "
            "redélivraison Twilio, une fois la pause levée, est droppée en silence"
        )

        # Fin de la pause : la redélivraison doit maintenant vraiment passer.
        monkeypatch.setattr(twilio_webhook_module, "is_celery_producer_paused", lambda: False)
        second = await _send(_FORM)
        assert second.status_code == 200
        assert len(calls) == 1
