"""Câblage `INTENT_CONFIG → core/goals.py → core/router.py` pour l'approvisionnement récurrent
(Phase 2, mandat §17).

Ce que ce fichier NE teste PAS : la classification LLM elle-même ("40 kg de tomates chaque jour" →
`CREATE_RECURRING_NEED`) — aucun test de ce dépôt n'invoque le vrai classifieur (LLM, non
déterministe, voir `[[llm-decides-not-frozen-french-lists]]` : le LLM décide, jamais une liste de
mots-clés figée côté tests non plus). Ce qui EST déterministe et testé ici : que les 3 intents sont
correctement enregistrés, routés vers le bon tunnel, et — le point de non-régression demandé par le
mandat — que les intents EXISTANTS (`BUYER_REQUEST`, `PROCUREMENT_CREATE_REQUEST`) restent dans LEUR
tunnel propre, jamais absorbés par le nouveau."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.goals import (
    BUYER_RECURRING_NEED_GOALS,
    BUYER_REQUEST_SPECIALIZATIONS,
)
from ladini.graphs.agents.market_coach.core.router import DomainRouter
from ladini.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)

NEW_GOALS = ("CREATE_RECURRING_NEED", "UPDATE_RECURRING_NEED", "GET_MY_NEEDS")


def test_the_three_new_intents_exist_in_the_catalog():
    for goal in NEW_GOALS:
        assert goal in INTENT_CONFIG, goal


def test_the_three_new_intents_are_buyer_only():
    for goal in NEW_GOALS:
        assert INTENT_ROLE[goal] == "BUYER", goal


def test_the_three_new_intents_share_the_recurring_need_tunnel():
    for goal in NEW_GOALS:
        assert INTENT_CONFIG[goal]["tunnel"] == "recurring_need", goal
    assert set(NEW_GOALS) == set(BUYER_RECURRING_NEED_GOALS)


def test_create_recurring_need_requires_the_minimal_slots_only():
    """Mandat §3 : produit, quantité, unité, fréquence — rien d'optionnel au pilote (starts_at,
    max_price_per_unit, weekly_days) n'est dans `required`."""
    required = set(INTENT_CONFIG["CREATE_RECURRING_NEED"]["required"])
    assert required == {"product", "quantity", "unit", "recurrence_type"}


def test_update_and_get_my_needs_have_no_hard_required_field():
    """Mandat §6 : l'identifiant du besoin est résolu conversationnellement (nom de produit), jamais
    exigé comme un slot obligatoire brut."""
    assert INTENT_CONFIG["UPDATE_RECURRING_NEED"]["required"] == []
    assert INTENT_CONFIG["GET_MY_NEEDS"]["required"] == []


def test_update_and_get_my_needs_are_handled_by_their_own_flow():
    """Pas de draft, pas de confirmation_gate générique — même choix que PROCUREMENT_UPDATE_REQUEST/
    BUYER_LIST_AUCTIONS (audit Phase 2, section H)."""
    assert INTENT_CONFIG["UPDATE_RECURRING_NEED"]["handled_by_flow"] is True
    assert INTENT_CONFIG["GET_MY_NEEDS"]["handled_by_flow"] is True


def test_no_new_intent_is_wrongly_added_to_buyer_request_specializations():
    """`BUYER_REQUEST` reste le tunnel de l'achat PONCTUEL — un besoin récurrent n'est jamais une
    "spécialisation" de BUYER_REQUEST (mandat §17 : "je cherche 5 kg de tomate maintenant" doit rester
    sur l'ancien flux d'achat, jamais capturé par le nouveau tunnel)."""
    assert not (set(NEW_GOALS) & BUYER_REQUEST_SPECIALIZATIONS)


def test_procurement_create_request_is_a_distinct_tunnel_from_recurring_need():
    """"2 tonnes de maïs... faites-moi des offres" doit continuer à utiliser le tunnel appel d'offres
    (PROCUREMENT_CREATE_REQUEST n'a pas de tunnel dédié — géré par le pipeline générique — mais il
    n'est, dans tous les cas, PAS dans le tunnel recurring_need)."""
    assert INTENT_CONFIG["PROCUREMENT_CREATE_REQUEST"].get("tunnel") != "recurring_need"
    assert "PROCUREMENT_CREATE_REQUEST" not in BUYER_RECURRING_NEED_GOALS
    assert "BUYER_REQUEST" not in BUYER_RECURRING_NEED_GOALS


def test_domain_router_sends_all_three_recurring_need_goals_to_the_resolver():
    router = DomainRouter.build()
    for goal in NEW_GOALS:
        state = {"current_goal": goal, "status": "PLANNING"}
        target = router.decide(state)
        assert target == "to_resolver", f"{goal} -> {target}"
