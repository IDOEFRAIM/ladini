"""`core/conversation_lock.py::conversation_turn_lock` (Phase 2 hardening, commit 9, mandat
décision E). Exercé contre `core/idempotency.py::claim_once`/`release` réels, avec un vrai
client Redis local (`FakeRedis` du harnais réutilisé ici directement pour rester cohérent avec
le reste de la suite — voir `tests/harness/conversation.py`)."""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from ladini.core import conversation_lock as lock_module
from ladini.core import idempotency
from tests.harness.conversation import FakeRedis


@pytest.fixture()
def fake_redis():
    redis = FakeRedis()
    with mock.patch.object(idempotency, "_client", redis):
        yield redis


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestUncontendedAcquisition:
    def test_acquires_immediately_when_free(self, fake_redis):
        async def scenario():
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0) as acquired:
                assert acquired is True

        _run(scenario())

    def test_releases_on_exit_so_a_later_call_can_acquire_immediately(self, fake_redis):
        async def scenario():
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0):
                pass
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0) as acquired:
                assert acquired is True

        _run(scenario())

    def test_releases_even_if_the_body_raises(self, fake_redis):
        async def scenario():
            with pytest.raises(ValueError):
                async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0):
                    raise ValueError("boom")
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0) as acquired:
                assert acquired is True

        _run(scenario())

    def test_different_conversations_never_contend(self, fake_redis):
        async def scenario():
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0) as a:
                async with lock_module.conversation_turn_lock("+226999", timeout_seconds=5.0) as b:
                    assert a is True and b is True

        _run(scenario())


class TestContendedAcquisition:
    def test_a_second_holder_waits_and_then_acquires_once_released(self, fake_redis):
        """Preuve directe de l'attente BORNÉE (mandat décision E) : le 2e appelant reste
        bloqué tant que le 1er tient le verrou, puis l'acquiert dès qu'il est libéré — jamais
        un rejet immédiat, jamais une exécution concurrente des deux."""
        events = []

        async def holder():
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0):
                events.append("A_enter")
                await asyncio.sleep(0.1)
                events.append("A_exit")

        async def waiter():
            await asyncio.sleep(0.02)  # laisse A acquérir en premier
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=5.0) as acquired:
                events.append("B_enter")
                assert acquired is True
                assert events == ["A_enter", "A_exit", "B_enter"], events

        async def scenario():
            await asyncio.gather(holder(), waiter())

        _run(scenario())
        assert events == ["A_enter", "A_exit", "B_enter"]

    def test_proceeds_in_degraded_mode_if_the_timeout_is_exceeded(self, fake_redis):
        """Jamais un blocage indéfini : si le verrou n'est toujours pas libre après
        `timeout_seconds`, le tour continue quand même (`acquired=False`), journalisé."""

        async def scenario():
            async with lock_module.conversation_turn_lock("+226700", timeout_seconds=10.0):
                async with lock_module.conversation_turn_lock(
                    "+226700", timeout_seconds=0.05
                ) as acquired:
                    assert acquired is False

        _run(scenario())


class TestFailOpenWhenRedisIsUnavailable:
    def test_never_blocks_when_redis_is_down(self):
        """`claim_once`/`release` sont fail-open (voir leur docstring) — ce verrou n'ajoute
        donc aucune nouvelle panne là où Redis est déjà indisponible."""
        with mock.patch.object(idempotency, "_client", None):
            async def scenario():
                async with lock_module.conversation_turn_lock("+226700", timeout_seconds=1.0) as acquired:
                    assert acquired is True

            _run(scenario())
