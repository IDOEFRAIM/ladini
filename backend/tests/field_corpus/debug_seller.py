"""Diagnostic manuel vendeur : SELLER_CTX=<contexte> SELLER_UTT=<phrase> pytest tests/field_corpus/debug_seller.py -s"""
from __future__ import annotations

import os

from ladini.graphs.agents.market_coach.llm_gateway.gateway import get_llm_gateway
from tests.field_corpus.harness import (
    SELLER_CONTEXTS,
    SellerConv,
    reply,
    seller_snapshot,
)


def test_debug_seller():
    utt = os.environ.get("SELLER_UTT", "")
    if not utt:
        return
    gw = get_llm_gateway()
    orig = gw.complete
    raws = []

    async def spy(*a, **k):
        res = await orig(*a, **k)
        raws.append((k.get("agent_node"), str(res.choices[0].message.content)[:700].replace("\n", " ")))
        return res

    gw.complete = spy
    conv = SellerConv(mode="real")
    for turn in SELLER_CONTEXTS.get(os.environ.get("SELLER_CTX", ""), []):
        conv.say(turn)
    raws.clear()
    st = conv.say(utt)
    for node, raw in raws:
        print("## RAW", node, raw)
    print("## SNAP", seller_snapshot(st), "| goal", st.get("current_goal"), "| event", st.get("interpreted_event"))
    print("## REPLY", reply(st)[:260].replace("\n", " / "))
