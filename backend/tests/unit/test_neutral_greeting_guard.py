"""Accueil NEUTRE (onboarding progressif) : jamais de nom/région/rôle/compte demandé d'emblée ; il montre acheter/vendre/récurrent."""
from __future__ import annotations

import asyncio

import pytest

from ladini.graphs.agents.market_coach.nodes.rendering.common import RenderContext
from ladini.graphs.agents.market_coach.nodes.rendering.feedback import (
    NEUTRAL_WELCOME,
    _asks_profile_up_front,
    render_clarification,
)


def _ctx(state):
    return RenderContext(state=state, mc_runtime=None, strategy="CLARIFICATION", status="WAITING_INPUT", goal=None, salutation="", payload={})


def test_the_deterministic_welcome_shows_the_three_doors_and_asks_nothing_personal():
    low = NEUTRAL_WELCOME.lower()
    assert "acheter" in low and "vendre" in low and "approvisionnement régulier" in low
    assert not _asks_profile_up_front({}, NEUTRAL_WELCOME)


@pytest.mark.parametrize("reply", [
    "Bonjour ! Quel est votre nom ?",
    "Pour l'inscription, je dois d'abord connaître votre nom.",
    "Dans quelle région êtes-vous ?",
    "Voulez-vous créer un compte ?",
    "Êtes-vous acheteur ou vendeur ?",
])
def test_a_generated_reply_asking_for_the_profile_up_front_is_replaced(reply):
    out = asyncio.run(render_clarification(_ctx({"final_response": reply, "turn_count": 1})))
    assert out["final_response"] != reply
    assert "approvisionnement régulier" in out["final_response"].lower()


def test_a_normal_reply_is_kept_and_the_guard_is_off_during_a_real_profile_collection():
    assert asyncio.run(render_clarification(_ctx({"final_response": "Voici vos options.", "turn_count": 1})))["final_response"] == "Voici vos options."
    assert not _asks_profile_up_front({"profile_gate": {"goal": "X"}}, "Quel est votre nom ?")
