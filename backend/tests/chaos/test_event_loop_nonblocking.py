"""CHAOS 5 — La boucle d'événements ne se fige JAMAIS.

Contexte de production : UN process worker Celery, UNE boucle asyncio,
DES DIZAINES d'utilisateurs WhatsApp simultanés. Un seul appel bloquant
(SDK Groq synchrone hors to_thread) et TOUS les utilisateurs attendent
derrière le message d'un seul. Ces tests le prouvent dynamiquement.
"""
from __future__ import annotations

import asyncio

from conftest import BlockingSleepLLM, run


async def _heartbeat(stop: asyncio.Event, interval: float = 0.05) -> int:
    """Compte les battements de la boucle pendant qu'une autre tâche tourne.
    Si la boucle est bloquée par un appel synchrone, le compteur reste ~0."""
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(interval)
        ticks += 1
    return ticks


def test_llm_question_generation_does_not_block_loop(runtime_with):
    """Rupture prévenue : la question de coaching (rendering/ask.py) exécutée
    en direct sur la boucle — pendant les ~1-3s d'un appel Groq réel, aucun
    autre message ne serait traité par le worker. Preuve dynamique : le
    heartbeat doit continuer de battre PENDANT l'appel bloquant simulé."""
    from ladini.graphs.agents.market_coach.nodes.rendering.ask import (
        generate_llm_question,
    )

    llm = BlockingSleepLLM(sleep_s=0.6, reply="Quel est ton prix, Adama ?")
    rt = runtime_with(llm=llm)

    async def scenario():
        stop = asyncio.Event()
        hb_task = asyncio.create_task(_heartbeat(stop))
        question = await generate_llm_question(
            rt, "SALES_PUBLISH_PRODUCT", "price", "prix", {"product": "maïs"},
        )
        stop.set()
        ticks = await hb_task
        return question, ticks

    question, ticks = run(scenario())
    assert llm.calls == 1
    assert question, "réponse LLM perdue"
    # 0.6s d'appel / 0.05s d'intervalle ⇒ ~12 battements attendus.
    # Seuil à 5 : marge généreuse pour une machine lente, mais un appel
    # bloquant donnerait 0 ou 1 — l'écart est non ambigu.
    assert ticks >= 5, (
        f"boucle d'événements FIGÉE pendant l'appel LLM ({ticks} battements) — "
        "un appel synchrone a quitté asyncio.to_thread"
    )


def test_parallel_llm_calls_overlap_not_serialize():
    """Rupture prévenue : deux utilisateurs simultanés dont les appels LLM se
    sérialisent (2 × 0.5s = 1s au lieu de ~0.5s). to_thread doit permettre le
    recouvrement — sinon la latence croît linéairement avec la charge."""
    import time as _time

    from ladini.graphs.agents.market_coach.nodes.rendering.ask import (
        generate_llm_question,
    )

    class _Rt:
        llm = BlockingSleepLLM(sleep_s=0.5)
        model_answer = "llama-3.1-8b-instant"

    async def scenario():
        rt = _Rt()
        t0 = _time.perf_counter()
        await asyncio.gather(
            generate_llm_question(rt, "SALES_PUBLISH_PRODUCT", "price", "prix", {}),
            generate_llm_question(rt, "SALES_PUBLISH_PRODUCT", "quantity", "quantité", {}),
        )
        return _time.perf_counter() - t0

    elapsed = run(scenario())
    assert elapsed < 0.9, (
        f"2 appels de 0.5s ont pris {elapsed:.2f}s — sérialisation détectée, "
        "la latence explosera linéairement avec le nombre d'utilisateurs"
    )


def test_executor_transient_backoff_does_not_block_loop():
    """Rupture prévenue : un backoff de retry implémenté en time.sleep au
    lieu d'asyncio.sleep — pendant l'attente, le worker entier serait gelé.
    Le heartbeat doit battre pendant les retries de l'exécuteur."""
    from ladini.graphs.agents.market_coach.nodes.executor import mcp_tool_executor

    class _SlowFail:
        calls = 0
        llm = None

        async def call_db(self, tool_name, **kwargs):
            _SlowFail.calls += 1
            raise ConnectionError("timeout connection (chaos)")

    state = {
        "user_phone": "+22670000000",
        "user_role": "PRODUCER",
        "current_goal": "SALES_PUBLISH_PRODUCT",
        "execution_authorized": True,
        "transaction_payload": {"product": "maïs", "quantity": 50, "price": 250, "farm_id": "f1"},
    }

    async def scenario():
        stop = asyncio.Event()
        hb_task = asyncio.create_task(_heartbeat(stop, interval=0.05))
        result = await mcp_tool_executor(state, _SlowFail())
        stop.set()
        return result, await hb_task

    result, ticks = run(scenario())
    assert result["status"] == "ERROR"
    # Le backoff (0.5s × tentative) doit laisser battre la boucle.
    assert ticks >= 3, f"boucle figée pendant le backoff de retry ({ticks} battements)"
