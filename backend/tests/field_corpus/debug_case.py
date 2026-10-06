"""Outil de diagnostic (manuel) : rejoue UN cas du corpus avec le vrai modèle et imprime ce que le modèle a renvoyé.

    FIELD_CASE=K2_take_and_quantity REDIS_URL=redis://localhost:6379/15 python -m pytest tests/field_corpus/debug_case.py -s -p no:cacheprovider
"""
from __future__ import annotations

import os

from ladini.graphs.agents.market_coach.llm_gateway.gateway import get_llm_gateway
from tests.field_corpus.cases import CASES
from tests.field_corpus.harness import CONTEXTS, FieldConv, business_snapshot, reply


def test_debug_case():
    wanted = os.environ.get("FIELD_CASE", "")
    if not wanted:
        return
    case = next(c for c in CASES if c["id"] == wanted)
    gw = get_llm_gateway()
    orig = gw.complete
    raws = []

    async def spy(*a, **k):
        res = await orig(*a, **k)
        raws.append((k.get("agent_node"), str(res.choices[0].message.content)[:700].replace("\n", " ")))
        return res

    gw.complete = spy
    conv = FieldConv(mode="real")
    for turn in CONTEXTS[case["ctx"]]:
        conv.say(turn)
    raws.clear()
    st = conv.say(case["utt"])
    print("\n## UTT", case["utt"])
    for node, raw in raws:
        print("## RAW", node, raw)
    print("## SNAPSHOT", business_snapshot(st))
    print("## REPLY", reply(st)[:300].replace("\n", " / "))
