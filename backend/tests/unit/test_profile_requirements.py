"""Exigences de profil PROGRESSIVES (`domain/profile_requirements.py`) — contrat pur, sans graphe ni base."""
from __future__ import annotations

import pytest

from ladini.domain.profile_requirements import (
    CapabilityState,
    ProfileAction,
    ProfileFacts,
    ProfileField,
    action_for_goal,
    capability_state,
    get_missing_requirements,
    is_real_name,
)

F = ProfileField


def facts(**kw):
    return ProfileFacts(**kw)


@pytest.mark.parametrize("value", [None, "", "  ", "Utilisateur", "Client", "N/A", "User_1234", "user_0001", "unknown"])
def test_system_placeholders_are_not_a_known_name(value):
    assert is_real_name(value) is False


@pytest.mark.parametrize("value", ["Moussa", "Restaurant Wend Konta", "Chez Ali", "Hôtel Silmandé"])
def test_real_names_people_or_establishments(value):
    assert is_real_name(value) is True


# La matrice du contrat : (action, faits) -> manquant, dans l'ordre où on le demande.
@pytest.mark.parametrize(
    "action,known,expected",
    [
        (ProfileAction.DISCOVERY, {}, ()),
        (ProfileAction.BUY_REQUEST, {}, (F.REGION,)),
        (ProfileAction.BUY_REQUEST, {"has_region": True}, ()),
        (ProfileAction.BUY_COMMIT, {}, (F.NAME, F.REGION)),
        (ProfileAction.BUY_COMMIT, {"name": "Awa"}, (F.REGION,)),
        (ProfileAction.BUY_COMMIT, {"has_region": True}, (F.NAME,)),
        (ProfileAction.BUY_COMMIT, {"name": "Awa", "has_region": True}, ()),
        (ProfileAction.SELL, {}, (F.NAME, F.REGION)),
        (ProfileAction.SELL, {"name": "Moussa"}, (F.REGION,)),
        (ProfileAction.SELL, {"name": "Moussa", "has_region": True}, ()),
        (ProfileAction.FINANCING, {"name": "Moussa", "has_region": True}, (F.VERIFIED_IDENTITY,)),
        (ProfileAction.FINANCING, {"name": "Moussa", "has_region": True, "identity_verified": True}, ()),
    ],
)
def test_requirements_matrix(action, known, expected):
    assert get_missing_requirements(action, facts(**known)) == expected


def test_a_placeholder_name_does_not_satisfy_a_requirement():
    assert get_missing_requirements(ProfileAction.SELL, facts(name="Utilisateur", has_region=True)) == (F.NAME,)


def test_discovery_needs_nothing_whatever_the_profile():
    assert get_missing_requirements(ProfileAction.DISCOVERY, facts()) == ()


@pytest.mark.parametrize(
    "goal,action",
    [
        ("SALES_PUBLISH_PRODUCT", ProfileAction.SELL),
        ("STOCK_REGISTER_HARVEST", ProfileAction.SELL),
        ("PRODUCTION_DECLARE_FUTURE", ProfileAction.SELL),
        ("BUYER_PREORDER_INIT", ProfileAction.BUY_COMMIT),
        ("BUYER_PREORDER_CONFIRM", ProfileAction.BUY_COMMIT),
        ("BUYER_REQUEST", ProfileAction.BUY_REQUEST),
        ("PROCUREMENT_CREATE_REQUEST", ProfileAction.BUY_REQUEST),
        ("CREATE_RECURRING_NEED", ProfileAction.BUY_REQUEST),
        ("MARKET_SNAPSHOT", ProfileAction.DISCOVERY),
        ("BUYER_ADD_TO_CART", ProfileAction.DISCOVERY),
        ("GET_MY_NEEDS", ProfileAction.DISCOVERY),
        (None, ProfileAction.DISCOVERY),
        ("", ProfileAction.DISCOVERY),
    ],
)
def test_goal_to_action(goal, action):
    assert action_for_goal(goal) is action


@pytest.mark.parametrize(
    "known,state",
    [
        ({}, CapabilityState.CONTACT),
        ({"can_buy": True}, CapabilityState.BUYER_DISCOVERY),
        ({"can_buy": True, "name": "Awa", "has_region": True}, CapabilityState.BUYER_READY),
        ({"can_sell": True}, CapabilityState.PRODUCER_PENDING_PROFILE),
        ({"can_sell": True, "name": "Moussa", "has_region": True}, CapabilityState.PRODUCER_READY),
        ({"can_sell": True, "name": "Moussa", "has_region": True, "producer_status": "PENDING"}, CapabilityState.PRODUCER_READY),
        ({"can_sell": True, "name": "Moussa", "has_region": True, "producer_status": "APPROVED"}, CapabilityState.PRODUCER_VERIFIED),
        ({"can_sell": True, "can_buy": True, "name": "Moussa", "has_region": True}, CapabilityState.PRODUCER_READY),
    ],
)
def test_capability_state_is_derived_never_stored(known, state):
    assert capability_state(facts(**known)) is state


def test_a_complete_profile_is_not_a_verified_profile():
    f = facts(can_sell=True, name="Moussa", has_region=True, producer_status="PENDING", identity_verified=False)
    assert capability_state(f) is CapabilityState.PRODUCER_READY  # prêt à publier, PAS vérifié


def test_facts_from_state_trust_permissions_only_when_the_profile_provided_them():
    f = ProfileFacts.from_state({"user_name": "Awa", "zone_id": "z-1"})
    assert f.has_name and f.has_region and not f.can_buy and not f.can_sell
    g = ProfileFacts.from_state({"user_permissions": {"can_buy": True, "can_sell": True}, "declared_location": "Kadiogo"})
    assert g.can_buy and g.can_sell and g.has_region and not g.has_name


def test_region_can_be_the_declared_location_without_an_operational_zone_row():
    assert ProfileFacts.from_state({"declared_location": "Nando"}).has_region is True
    assert ProfileFacts.from_state({"declared_location": "  "}).has_region is False
