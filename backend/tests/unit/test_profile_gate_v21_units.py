"""V2.1 — primitives unitaires : nom d'affichage, lecture de slot, commande en pause, gabarit fail-closed."""
from __future__ import annotations

import asyncio
import json

import pytest

from ladini.domain.profile_requirements import (
    ProfileAction,
    ProfileField,
    display_name_or_none,
    is_placeholder_display_name,
)
from ladini.graphs.agents.market_coach.core.profile_gate import (
    build_gate,
    command_is_resumable,
)
from ladini.graphs.agents.market_coach.flows.common.profile_gate_turn import (
    build_resume_patch,
)
from ladini.graphs.agents.market_coach.flows.common.profile_slot import (
    ANSWER,
    UNCLEAR,
    read_profile_slot,
)


@pytest.mark.parametrize("value", [None, "", "  ", "User_2876", "user_1", "Utilisateur", "Utilisateur_12", "Guest", "Contact_123", "Client", "Zone inconnue"])
def test_system_fallbacks_are_placeholders(value):
    assert is_placeholder_display_name(value)
    assert display_name_or_none(value) is None


@pytest.mark.parametrize("value", ["Zouba", "Restau chez Zouba", "Hôtel Wend Panga", "Société Wend Konta", "Chez Zouba"])
def test_real_names_are_kept(value):
    assert not is_placeholder_display_name(value)
    assert display_name_or_none(value) == value


class _NoLlmRuntime:
    llm = None


@pytest.mark.parametrize("text,expected", [("Zouba", "Zouba"), ("C'est Zouba", "Zouba"), ("Mon nom c'est Zouba", "Zouba"), ("Chez Zouba", "Chez Zouba")])
def test_without_llm_a_short_answer_is_a_name_and_the_intro_is_stripped(text, expected):
    r = asyncio.run(read_profile_slot(_NoLlmRuntime(), text, ProfileField.NAME))
    assert r.kind == ANSWER and r.value == expected


@pytest.mark.parametrize("text", ["montre-moi d'abord le prix", "pourquoi ?", "plus tard", "je veux 5 litres de lait", ""])
def test_without_llm_interruptions_and_questions_are_never_a_name(text):
    assert asyncio.run(read_profile_slot(_NoLlmRuntime(), text, ProfileField.NAME)).kind == UNCLEAR


def test_the_region_is_resolved_deterministically_without_any_llm():
    r = asyncio.run(read_profile_slot(_NoLlmRuntime(), "A ouagadougou", ProfileField.REGION))
    assert r.kind == ANSWER and r.value == "Kadiogo" and r.source == "deterministic"


def test_the_paused_command_is_restored_identically_and_the_region_replaces_the_placeholder_zone():
    state = {"current_goal": "BUYER_PREORDER_INIT", "detected_intent": "BUYER_PREORDER_INIT", "interpreted_event": "CONFIRM",
             "transaction_payload": {"product": "lait", "quantity": 5, "zone": "Zone inconnue"}, "extracted_entities": {"product": "lait"}}
    gate = build_gate(state, ProfileAction.BUY_COMMIT, (ProfileField.NAME,))
    state["transaction_payload"]["quantity"] = 999  # le snapshot est une COPIE : la mutation ultérieure ne le contamine pas
    patch = build_resume_patch({"working_memory": {}}, gate, {"declared_location": "Kadiogo", "user_name": "Zouba"})
    assert patch["current_goal"] == "BUYER_PREORDER_INIT" and patch["interpreted_event"] == "CONFIRM"
    assert patch["transaction_payload"] == {"product": "lait", "quantity": 5, "zone": "Kadiogo"}
    assert patch["working_memory"]["active_goal"] == "BUYER_PREORDER_INIT" and patch["profile_gate"] is None


def test_a_command_without_a_real_goal_is_not_resumable():
    assert not command_is_resumable({}, {}) and not command_is_resumable({"goal": "NONE"}, {})
    assert command_is_resumable({"goal": "BUYER_PREORDER_INIT"}, {})


def test_the_preorder_template_never_announces_zero_for_your_request():
    from ladini.graphs.agents.market_coach.nodes.rendering import success

    out = success._transactional_fallback_text("BUYER_PREORDER_INIT", "", {})
    text = json.dumps(out, ensure_ascii=False)
    assert "pour 0" not in text and "votre demande" not in text
