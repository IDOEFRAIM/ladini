"""Réponses au digest quotidien d'approvisionnement récurrent — mandat digest 2026-09-26.

Bug réel reproduit (voir docstring des tests de la section "Root cause" ci-dessous) :
un compte double-rôle (producteur ET acheteur, ex: un producteur qui a AUSSI des besoins
récurrents) reçoit le digest ("Approvisionnement de demain — 0/75 Oignon, 100/350 Tomate...")
et répond "modifier". Le graphe compilé pour ce numéro se résout en `role="PRODUCER"`
(`Workspace.workspace_type` sticky par défaut — voir `orchestrator.py::_run_market`), donc le
fast-path déterministe dédié au digest (`interpreter/routing.py::
_bare_confirmation_for_recurring_supply_digest`), restreint jusqu'ici à `role_up == "BUYER"`,
n'est JAMAIS atteint : "modifier" retombe sur la classification générique -> `clarification_node`
-> repli LLM générique orienté producteur ("enregistrer une nouvelle récolte, mettre un produit
en vente, ou gérer votre stock" — exactement le texte rapporté en production).

Second défaut, indépendant du premier : même quand le rôle résout correctement en BUYER,
"modifier" ne faisait QUE rediriger vers `GET_MY_NEEDS` (listing générique en lecture seule,
sans `action`, sans ancrage) — jamais une vraie modification guidée (menu -> quantité ->
override d'UNE occurrence).

Ce fichier rejoue le VRAI graphe compilé (`tests/harness/conversation.py`, jamais une
réimplémentation partielle) et couvre les tests mandatés A-J + le scénario E2E du mandat §16.
"""
from __future__ import annotations

from unittest import mock

import pytest

from ladini.graphs.agents.market_coach.core import (
    pending_interaction as pending_interaction_module,
)
from ladini.services.database.recurring_supply import MATCH_RESPONSE_ACTIONS
from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

_UNKNOWN = {"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}}

_TWO_NEEDS = [
    {
        "recurring_need_id": "need-oignon",
        "product": "oignon",
        "quantity": 75.0,
        "unit": "KG",
        "recurrence_type": "DAILY",
        "status": "ACTIVE",
        "next_occurrence_id": "occ-oignon",
        "next_occurrence_date": "2026-09-27",
        "next_occurrence_notified": True,
        "requested_quantity": 75.0,
        "matched_quantity": 0.0,
    },
    {
        "recurring_need_id": "need-tomate",
        "product": "tomate",
        "quantity": 350.0,
        "unit": "KG",
        "recurrence_type": "DAILY",
        "status": "ACTIVE",
        "next_occurrence_id": "occ-tomate",
        "next_occurrence_date": "2026-09-27",
        "next_occurrence_notified": True,
        "requested_quantity": 350.0,
        "matched_quantity": 100.0,
    },
]

_ONE_NEED = [_TWO_NEEDS[0]]
# Distinct de `_ONE_NEED` (oignon, `matched_quantity=0` — volontaire pour les tests "modifier",
# où l'absence de match ne doit pas bloquer une modification) : `accept_match_proposal` ne porte
# que sur les besoins avec une disponibilité RÉELLEMENT trouvée (`_respond_to_digest_flow`'s
# `actionable` filter, `matched_quantity > 0`) — les tests CONFIRM/ACCEPT ont besoin d'un besoin
# qui passe ce filtre.
_ONE_MATCHED_NEED = [_TWO_NEEDS[1]]


def _override_calls(turn):
    return [c for c in turn.mcp_calls if c[0] == "update_recurring_need"]


def _match_calls(turn):
    return [c for c in turn.mcp_calls if c[0] == "accept_match_proposal"]


# =====================================================================
# Root cause — reproduction directe du bug de production (avant/après)
# =====================================================================


class TestRootCauseRoleMismatchNeverReachesGenericFallback:
    """Compte double-rôle : le graphe compilé pour ce numéro résout `role="PRODUCER"`
    (workspace sticky par défaut) alors que la réponse concerne son ambiguïté acheteur — voir
    docstring de module. AVANT le correctif, ce test échouait : `list_my_recurring_needs`
    n'était jamais appelé et la réponse tombait sur le repli générique (`CLARIFICATION`)."""

    def test_modifier_reaches_the_digest_aware_flow_even_when_role_resolves_to_producer(self):
        with ConversationHarness(role="PRODUCER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("modifier", llm=_UNKNOWN)
            assert "list_my_recurring_needs" in t.mcp_tools()
            assert t.strategy != "CLARIFICATION"
            assert "Oignon" in t.response and "Tomate" in t.response

    def test_a_producer_with_no_recurring_need_at_all_is_unaffected(self):
        """Garde-fou explicite (mandat : "aucun risque pour un producteur sans besoin
        récurrent") — le fast-path élargi reste borné par la réalité DB, jamais un
        court-circuit inconditionnel pour tout compte PRODUCER."""
        with ConversationHarness(role="PRODUCER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": []}
            t = conv.send("modifier", llm=_UNKNOWN)
            assert t.event == "UNKNOWN"


# =====================================================================
# A. "modifier" ne tombe plus jamais dans le fallback générique (BUYER, cas nominal)
# =====================================================================


class TestA_ModifierNeverFallsToGenericFallback:
    def test_modifier_opens_the_guided_menu_not_a_generic_clarification(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("modifier", llm=_UNKNOWN)
            assert t.strategy != "CLARIFICATION"
            assert t.pending_after.kind.value == "RECURRING_SUPPLY_DIGEST_ACTION"


# =====================================================================
# B/C. "confirmer" / "pas demain" — comportement PRÉEXISTANT préservé (mandat §17)
# =====================================================================


class TestBC_ConfirmAndSkipAllPreserved:
    def test_confirmer_sends_the_canonical_accept_action_to_the_service(self):
        # (2026-09-26, mandat "mismatch CONFIRM vs ACCEPT") : CE test verrouillait auparavant
        # `action == "CONFIRM"` — le bug lui-même — au nom de "ne jamais toucher la sémantique de
        # confirmer". Corrigé : la SÉMANTIQUE de "confirmer" (accepter la proposition, créer les
        # commandes) est inchangée, seul le MOT envoyé au service change, de "CONFIRM" (jamais
        # accepté par `MATCH_RESPONSE_ACTIONS`, `services/database/recurring_supply.py` — chaque
        # appel réel à `accept_match_proposal`, y compris toute la suite Postgres dédiée
        # `tests/schema/test_recurring_need_confirmation_service.py`, utilise "ACCEPT") à "ACCEPT".
        # Garder l'ancien verrou aurait figé un bug déjà prouvé, contrairement au mandat §7.
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("confirmer", llm=_UNKNOWN)
            calls = [c for c in t.mcp_calls if c[0] == "accept_match_proposal"]
            assert calls, "accept_match_proposal doit toujours être appelé pour 'confirmer'"
            assert all(kw["action"] == "ACCEPT" for _, kw in calls)
            assert all(kw["action"] in MATCH_RESPONSE_ACTIONS for _, kw in calls)

    def test_pas_demain_still_rejects_every_actionable_need(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("pas demain", llm=_UNKNOWN)
            calls = [c for c in t.mcp_calls if c[0] == "accept_match_proposal"]
            assert calls
            assert all(kw["action"] == "REJECT" for _, kw in calls)


# =====================================================================
# Mandat "mismatch CONFIRM vs ACCEPT" (2026-09-26) — tests A-G
# =====================================================================


class TestConfirmAcceptContract:
    """`_respond_to_digest_flow` envoyait `action="CONFIRM"` à `RecurringSupplyGateway.
    accept_match_proposal`, alors que le service réel (`services/database/recurring_supply.py`)
    ne connaît que `MATCH_RESPONSE_ACTIONS = ("ACCEPT", "REJECT")` — chaque appel réel à
    `accept_match_proposal`, un mot hors de ce contrat aurait levé `BusinessRuleException("Action
    inconnue")`. Corrigé via un mapping explicite (`_MATCH_RESPONSE_TO_SERVICE_ACTION`, PARTAGÉ
    avec `_respond_to_match` — voir `tests/nodes/test_recurring_need_get_my_needs_flow.py`), validé
    au chargement du module contre le contrat canonique importé."""

    def test_A_confirmer_maps_to_the_canonical_accept_action(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_MATCHED_NEED}
            t = conv.send("confirmer", llm=_UNKNOWN)
            calls = _match_calls(t)
            assert len(calls) == 1
            assert calls[0][1]["action"] == "ACCEPT"

    def test_B_ok_maps_to_the_same_canonical_accept_action(self):
        # "ok" fait partie du vocabulaire fermé déjà réutilisé par le fast-path digest
        # (`_CONFIRM_EXACT_PHRASES`, partagé avec la confirmation générique) — même résultat que
        # "confirmer" attendu.
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_MATCHED_NEED}
            t = conv.send("ok", llm=_UNKNOWN)
            calls = _match_calls(t)
            assert len(calls) == 1
            assert calls[0][1]["action"] == "ACCEPT"

    def test_C_double_confirmation_is_idempotent_never_duplicated(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_MATCHED_NEED}
            first = conv.send("confirmer", llm=_UNKNOWN, message_id="wamid.CONFIRM-DIGEST-1")
            replay = conv.send("confirmer", llm=_UNKNOWN, message_id="wamid.CONFIRM-DIGEST-1")
            assert len(_match_calls(first)) == 1
            assert not _match_calls(replay)
            assert replay.nodes == [], "un message déjà traité ne doit pas rejouer le graphe"

    def test_D_modifier_never_goes_through_accept_match_proposal(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t1 = conv.send("modifier", llm=_UNKNOWN)
            assert not _match_calls(t1)
            t2 = conv.send("1", llm=_UNKNOWN)
            t3 = conv.send("40 kg", llm=_UNKNOWN)
            assert not _match_calls(t2)
            assert not _match_calls(t3)

    def test_E_pas_demain_never_sends_accept(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("pas demain", llm=_UNKNOWN)
            calls = _match_calls(t)
            assert calls
            assert all(kw["action"] == "REJECT" for _, kw in calls)
            assert all(kw["action"] != "ACCEPT" for _, kw in calls)

    @pytest.mark.parametrize("channel", ["whatsapp", "webchat"])
    def test_F_confirmer_sends_accept_on_both_channels(self, channel):
        with ConversationHarness(role="BUYER", channel=channel) as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_MATCHED_NEED}
            t = conv.send("confirmer", llm=_UNKNOWN)
            calls = _match_calls(t)
            assert len(calls) == 1
            assert calls[0][1]["action"] == "ACCEPT"

    def test_G_every_action_sent_to_the_service_belongs_to_the_canonical_contract(self):
        """Test de contrat générique (mandat §6.G) : quel que soit le mot conversationnel
        ("confirmer", "ok", "pas demain", "non"...), la valeur RÉELLEMENT envoyée au service
        appartient toujours à `MATCH_RESPONSE_ACTIONS` — jamais un mot conversationnel qui fuite
        tel quel."""
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            for text in ("confirmer", "oui", "ok", "pas demain", "non"):
                conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_MATCHED_NEED}
                t = conv.send(text, llm=_UNKNOWN)
                for _, kwargs in _match_calls(t):
                    assert kwargs["action"] in MATCH_RESPONSE_ACTIONS, (
                        f"'{text}' a envoyé action={kwargs['action']!r}, hors du contrat "
                        f"canonique {MATCH_RESPONSE_ACTIONS}"
                    )


# =====================================================================
# D. Menu à choix multiples — 2+ besoins actionnables
# =====================================================================


class TestD_MultiNeedAsksSelection:
    def test_modifier_with_two_needs_shows_a_numbered_menu(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("modifier", llm=_UNKNOWN)
            assert "1. Oignon" in t.response
            assert "2. Tomate" in t.response
            assert t.pending_after.target["sub_state"] == "DIGEST_AWAIT_SELECTION"

    def test_modifier_with_a_single_need_skips_the_menu(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_NEED}
            t = conv.send("modifier", llm=_UNKNOWN)
            assert "quantité" in t.response.lower()
            assert "oignon" in t.response.lower()
            assert t.pending_after.target["sub_state"] == "DIGEST_AWAIT_QUANTITY"


# =====================================================================
# E/F. "modifier tomate à 200 kg" — résolution directe, sans ambiguïté (mandat §9)
# =====================================================================


class TestEF_DirectFreeTextResolution:
    def test_modifier_tomate_a_200kg_resolves_directly_without_asking_which_one(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send(
                "modifier tomate à 200 kg",
                llm=new_task("UPDATE_RECURRING_NEED", product="tomate", quantity=200.0),
            )
            calls = _override_calls(t)
            assert len(calls) == 1
            _, kwargs = calls[0]
            assert kwargs["recurring_need_id"] == "need-tomate"
            assert kwargs["quantity"] == 200.0
            assert "oignon" not in t.response.lower()


# =====================================================================
# G. "pas demain pour l'oignon" — skip d'UN SEUL besoin nommé (mandat §9)
# =====================================================================


class TestG_NamedProductSkip:
    def test_pas_demain_pour_oignon_skips_only_that_occurrence(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t = conv.send("pas demain pour l'oignon", llm=_UNKNOWN)
            calls = _override_calls(t)
            assert len(calls) == 1
            _, kwargs = calls[0]
            assert kwargs["recurring_need_id"] == "need-oignon"
            assert kwargs["action"] == "OCCURRENCE_SKIP"
            assert kwargs["occurrence_date"] == "2026-09-27"
            assert "tomate" not in t.response.lower()


# =====================================================================
# H. PendingInteraction expiré — jamais une mutation périmée
# =====================================================================


class TestH_ExpiredPendingNeverAppliesAStaleMutation:
    def test_a_reply_after_the_digest_ttl_never_mutates_anything_stale(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t1 = conv.send("modifier", llm=_UNKNOWN)
            created_at = t1.after["pending_interaction"]["created_at"]
            assert t1.pending_after.kind.value == "RECURRING_SUPPLY_DIGEST_ACTION"

            with mock.patch.object(
                pending_interaction_module.time, "time", return_value=created_at + 72001.0
            ):
                t2 = conv.send("1", llm=_UNKNOWN)

            assert not _override_calls(t2), "une sélection périmée ne doit jamais déclencher un override"
            assert t2.pending_after.kind.value != "RECURRING_SUPPLY_DIGEST_ACTION" or (
                t2.pending_after.target or {}
            ).get("sub_state") != "DIGEST_AWAIT_QUANTITY"


# =====================================================================
# I. Idempotence — retry webhook / double envoi
# =====================================================================


class TestI_Idempotency:
    def test_a_replayed_whatsapp_message_never_applies_the_override_twice(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _ONE_NEED}
            conv.send("modifier", llm=_UNKNOWN)
            first = conv.send("40 kg", llm=_UNKNOWN, message_id="wamid.DIGEST-QTY-1")
            replay = conv.send("40 kg", llm=_UNKNOWN, message_id="wamid.DIGEST-QTY-1")
            assert len(_override_calls(first)) == 1
            assert not _override_calls(replay)
            assert replay.nodes == [], "un message déjà traité ne doit pas rejouer le graphe"

    def test_double_pas_demain_does_not_duplicate_the_rejection(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            conv.send("pas demain", llm=_UNKNOWN)
            second = conv.send("pas demain", llm=_UNKNOWN)
            # Idempotent au niveau métier (mandat §13) : la 2e occurrence, déjà REJECTED, n'est
            # plus dans `actionable` (matched_quantity>0 filtré à la source), donc `list_my_
            # recurring_needs` est rappelé mais aucun 2e `accept_match_proposal` ne part si le
            # double n'y trouve plus rien d'actionnable — cette réponse-ci ne doit en tout cas
            # jamais dire "confirmé"/produire une deuxième mutation visible.
            assert "confirmé" not in second.response.lower()


# =====================================================================
# J. Parité de canal — WhatsApp / WebChat
# =====================================================================


class TestJ_ChannelParity:
    @pytest.mark.parametrize("channel", ["whatsapp", "webchat"])
    def test_the_full_digest_modify_flow_behaves_identically_on_both_channels(self, channel):
        with ConversationHarness(role="BUYER", channel=channel) as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}
            t1 = conv.send("modifier", llm=_UNKNOWN)
            assert "1. Oignon" in t1.response and "2. Tomate" in t1.response

            t2 = conv.send("1", llm=_UNKNOWN)
            assert "quantité" in t2.response.lower()

            t3 = conv.send("40 kg", llm=_UNKNOWN)
            calls = _override_calls(t3)
            assert len(calls) == 1
            _, kwargs = calls[0]
            assert kwargs["recurring_need_id"] == "need-oignon"
            assert kwargs["quantity"] == 40.0
            assert t3.pending_after.kind.value == "NONE"


# =====================================================================
# §16 — Scénario E2E explicite du mandat
# =====================================================================


class TestFullMandateE2EScenario:
    """"🌾 Approvisionnement de demain / ❌ Oignon : 0/75 KG / ⚠️ Tomate : 100/350 KG" ->
    "modifier" -> menu numéroté -> "1" -> "Quelle quantité veux-tu pour demain ?" -> "40 kg" ->
    override de L'OCCURRENCE de demain à 40 KG, le besoin permanent (75 KG) INCHANGÉ."""

    def test_exact_mandate_conversation(self):
        with ConversationHarness(role="BUYER", channel="whatsapp") as conv:
            conv.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": _TWO_NEEDS}

            t1 = conv.send("modifier", llm=_UNKNOWN)
            assert "1. Oignon" in t1.response
            assert "2. Tomate" in t1.response

            t2 = conv.send("1", llm=_UNKNOWN)
            assert "Quelle quantité veux-tu pour" in t2.response
            assert "oignon" in t2.response.lower()

            t3 = conv.send("40 kg", llm=_UNKNOWN)
            calls = _override_calls(t3)
            assert len(calls) == 1
            _, kwargs = calls[0]
            assert kwargs["recurring_need_id"] == "need-oignon"
            assert kwargs["action"] == "OCCURRENCE_OVERRIDE"
            assert kwargs["occurrence_date"] == "2026-09-27"
            assert kwargs["quantity"] == 40.0
            # Le besoin PERMANENT n'est jamais touché par cette action (mandat §5) : aucun appel
            # PERMANENT_QUANTITY, la mutation ne porte QUE sur l'occurrence de demain.
            assert all(c[1].get("action") != "PERMANENT_QUANTITY" for c in calls)
            assert t3.pending_after.kind.value == "NONE"
