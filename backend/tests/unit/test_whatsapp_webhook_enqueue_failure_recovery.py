"""`api/routes/whatsapp_webhook.py` — miroir volontaire de
`test_twilio_webhook_enqueue_failure_recovery.py` : même bug, même
correctif, provider différent (Meta Cloud API au lieu de Twilio).

Avant ce correctif, une exception issue de `process_agent_task.delay(...)`
(§6 de `_process_single_message`) remontait jusqu'au filet générique de
`whatsapp_webhook()` (`except Exception: return _plain_ok()` — un 200) sans
jamais relâcher le claim `msg:{id}` posé en §1 — le message disparaissait
silencieusement, sans redélivraison possible pendant jusqu'à 1h (TTL du
claim). `_EnqueueFailed` distingue maintenant ce cas précis pour renvoyer un
503 (Meta redélivre le lot) après avoir relâché le claim du message en échec.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

pytest.importorskip("celery", reason="webhook module imports api.tasks -> celery.signals")

import ladini.api.routes.whatsapp_webhook as whatsapp_webhook_module
from tests.harness.conversation import FakeRedis


class _FakeBackgroundTasks:
    def add_task(self, *_a: Any, **_kw: Any) -> None:
        pass


class _FakeWorkspaceStore:
    async def get(self, _workspace_id: str) -> Optional[Any]:
        return None


def _wire_common_doubles(monkeypatch, *, delay_side_effect: Optional[BaseException] = None):
    fake_redis = FakeRedis()
    monkeypatch.setattr(whatsapp_webhook_module, "redis_client", fake_redis)
    monkeypatch.setattr(whatsapp_webhook_module, "is_celery_producer_paused", lambda: False)
    monkeypatch.setattr(
        whatsapp_webhook_module, "WorkspaceStore", lambda: _FakeWorkspaceStore()
    )

    delay_calls: list[Dict[str, Any]] = []

    def _fake_delay(**kwargs: Any) -> Any:
        delay_calls.append(kwargs)
        if delay_side_effect is not None:
            raise delay_side_effect
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(whatsapp_webhook_module.process_agent_task, "delay", _fake_delay)
    return fake_redis, delay_calls


_MESSAGE_ID = "wamid.test0001"


def _text_message(body: str = "je veux vendre 50kg de maïs") -> Dict[str, Any]:
    return {
        "id": _MESSAGE_ID,
        "from": "22670000001",
        "type": "text",
        "text": {"body": body},
    }


async def _send_single_message(message: Dict[str, Any]):
    return await whatsapp_webhook_module._process_single_message(
        message, _FakeBackgroundTasks()
    )


class TestEnqueueFailureIsRecoverableNotSilentlyLost:
    @pytest.mark.asyncio
    async def test_a_broker_error_on_enqueue_releases_the_claim_and_raises_enqueue_failed(
        self, monkeypatch
    ):
        fake_redis, _calls = _wire_common_doubles(
            monkeypatch, delay_side_effect=RuntimeError("broker unreachable")
        )

        with pytest.raises(whatsapp_webhook_module._EnqueueFailed):
            await _send_single_message(_text_message())

        assert fake_redis.exists(f"msg:{_MESSAGE_ID}") == 0, (
            "le claim doit être relâché après un échec d'enqueue — sinon la "
            "redélivraison Meta (déclenchée par le 503 renvoyé par "
            "whatsapp_webhook()) tombera sur 'déjà en cours' et sera droppée"
        )

    @pytest.mark.asyncio
    async def test_the_outer_handler_turns_enqueue_failure_into_a_503_not_a_200(
        self, monkeypatch
    ):
        """Bout en bout via l'entrypoint HTTP réel (`whatsapp_webhook`),
        exactement le chemin que `_EnqueueFailed` doit intercepter AVANT le
        filet générique `except Exception: return _plain_ok()`."""
        fake_redis, _calls = _wire_common_doubles(
            monkeypatch, delay_side_effect=RuntimeError("broker unreachable")
        )

        monkeypatch.setattr(whatsapp_webhook_module.settings, "WHATSAPP_APP_SECRET", "")
        monkeypatch.setattr(whatsapp_webhook_module, "is_explicit_dev_mode", lambda: True)

        class _FakeRequest:
            async def body(self) -> bytes:
                return b"{}"

            async def json(self) -> Dict[str, Any]:
                return {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {"messages": [_text_message()]},
                                }
                            ]
                        }
                    ]
                }

        response = await whatsapp_webhook_module.whatsapp_webhook(
            _FakeRequest(), _FakeBackgroundTasks()
        )

        assert response.status_code == 503, (
            f"un échec d'enqueue doit demander une redélivraison Meta (503), "
            f"pas un 200 qui déclare le message livré à tort : {response.status_code}"
        )
        assert fake_redis.exists(f"msg:{_MESSAGE_ID}") == 0

    @pytest.mark.asyncio
    async def test_a_redelivery_after_enqueue_failure_is_treated_as_a_fresh_message(
        self, monkeypatch
    ):
        fake_redis, calls = _wire_common_doubles(
            monkeypatch, delay_side_effect=RuntimeError("broker unreachable")
        )
        with pytest.raises(whatsapp_webhook_module._EnqueueFailed):
            await _send_single_message(_text_message())
        assert len(calls) == 1

        # Broker réparé pour la redélivraison Meta.
        monkeypatch.setattr(
            whatsapp_webhook_module.process_agent_task,
            "delay",
            lambda **kwargs: (calls.append(kwargs), SimpleNamespace(id="task-2"))[1],
        )

        await _send_single_message(_text_message())

        assert len(calls) == 2, (
            "la redélivraison Meta du même id doit réellement retenter l'enqueue "
            "— pas être avalée par le dédoublonnage laissé par le 1er échec"
        )

    @pytest.mark.asyncio
    async def test_a_successful_enqueue_keeps_the_claim_so_a_true_duplicate_is_still_deduped(
        self, monkeypatch
    ):
        fake_redis, calls = _wire_common_doubles(monkeypatch, delay_side_effect=None)

        await _send_single_message(_text_message())
        assert len(calls) == 1
        assert fake_redis.exists(f"msg:{_MESSAGE_ID}") == 1

        await _send_single_message(_text_message())
        assert len(calls) == 1, "un message déjà réussi ne doit jamais être ré-enqueué"
