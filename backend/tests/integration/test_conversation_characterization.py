"""Incidents réels du moteur conversationnel, rejoués sur le VRAI graphe compilé.

Harnais : `tests/harness/conversation.py` (orchestrateur réel, graphe réel, checkpointer
réel, persistance JSON réelle ; seuls LLM/MCP/Redis/envoi sont doublés).

Chaque test affirme le comportement CORRECT. Quand le code actuel viole encore un
invariant, le test porte `xfail(strict=True, reason=<défaut>)` : il DOIT échouer tant que
le défaut existe, et le moindre passage inattendu (XPASS) casse la CI — la correction
doit alors retirer le marqueur dans le même commit. Identifiants des défauts :
`docs/CONVERSATIONAL_ENGINE_HARDENING_AUDIT_2026-09-24.md` (B1..B13) et les découvertes
faites en écrivant ce harnais (H1..).

Ces tests sont permanents (mandat Phase 2 §5).
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

_INFRA_TOOLS = {"get_account_status", "get_prohibited_terms", "get_user_by_phone"}


def _coq(**extra: Any) -> Dict[str, Any]:
    return new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY", **extra)


def _coq_chevre() -> Dict[str, Any]:
    return _coq(additional_items=[{"product": "chèvre", "quantity": 20.0}])


def _coq_moutons_chevres() -> Dict[str, Any]:
    return _coq(ambiguous_groups=[{"quantity": 57.0, "candidates": ["mouton", "chevre"]}])


_UNKNOWN = {"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}}


def _created(conv: ConversationHarness) -> list:
    return [(tool, kw) for tool, kw in conv.runtime.calls if tool in ("create_recurring_need", "create_recurring_needs")]


def _no_active_transaction(state: Dict[str, Any]) -> list:
    """Liste des traces transactionnelles encore actives — vide = état propre."""
    leaks = []
    wm = state.get("working_memory") or {}
    if wm.get("active_goal"):
        leaks.append(f"working_memory.active_goal={wm.get('active_goal')}")
    if state.get("current_goal"):
        leaks.append(f"current_goal={state.get('current_goal')}")
    if state.get("pending_interaction"):
        leaks.append(f"pending_interaction={state.get('pending_interaction')}")
    if state.get("recurring_need_draft"):
        leaks.append("recurring_need_draft")
    tp = {k: v for k, v in (state.get("transaction_payload") or {}).items() if v not in (None, "", [], {})}
    if tp:
        leaks.append(f"transaction_payload={sorted(tp)}")
    return leaks


@pytest.fixture()
def conv():
    with ConversationHarness(role="BUYER", channel="whatsapp") as harness:
        yield harness


# =====================================================================
# A. "14 coqs chaque semaine"
# =====================================================================


class TestA_SingleLivestockWeekly:
    def test_draft_is_built_and_confirmation_is_requested(self, conv):
        t = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        assert t.error is None
        assert t.event == "NEW_TASK" and t.intent == "CREATE_RECURRING_NEED"
        assert t.goal_after == "CREATE_RECURRING_NEED"
        assert t.tunnel_after == "recurring_need"
        draft = t.draft()
        assert draft["status"] == "DRAFT"
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("coq", 14.0, "WEEKLY")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        assert t.pending_after.target and t.pending_after.target["draft_id"] == draft["draft_id"]

    def test_confirmation_creates_exactly_one_need_and_leaves_a_clean_state(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send("oui")
        assert t.llm_calls == 0, "un « oui » exact ne doit jamais coûter un appel LLM"
        assert len(_created(conv)) == 1
        assert t.goal_after is None
        assert _no_active_transaction(t.after) == []

    @pytest.mark.xfail(strict=True, reason="B5/H1: récapitulatif générique (UNITE, sans fréquence) au lieu du draft (TETE)")
    def test_the_same_unit_and_frequency_are_shown_before_and_after_confirmation(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t2 = conv.send("oui")
        assert "TETE" in t2.response
        assert "UNITE" not in t1.response and "TETE" in t1.response
        assert "semaine" in t1.response.lower()


# =====================================================================
# B. "14 coqs et 20 chèvres chaque semaine"
# =====================================================================


class TestB_TwoItems:
    def test_both_items_reach_the_draft_and_the_single_atomic_create(self, conv):
        t1 = conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        draft = t1.draft()
        assert draft["product"] == "coq"
        assert [it["product"] for it in draft["additional_items"]] == ["chèvre"]
        conv.send("oui")
        created = _created(conv)
        assert [tool for tool, _ in created] == ["create_recurring_needs"]
        items = created[0][1]["items"]
        assert [(it["product_query"], it["quantity"]) for it in items] == [("coq", 14.0), ("chèvre", 20.0)]

    def test_both_items_share_one_canonical_unit(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        conv.send("oui")
        items = _created(conv)[0][1]["items"]
        assert {it["unit"] for it in items} == {"TETE"}

    @pytest.mark.xfail(strict=True, reason="H1: le récapitulatif avant confirmation n'affiche que le 1er item")
    def test_the_confirmation_shows_every_item(self, conv):
        t = conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        text = t.response.lower()
        assert "coq" in text and "chèvre" in text


# =====================================================================
# C/D/E. clarification "57 moutons chèvres"
# =====================================================================


class TestCDE_AmbiguousGroup:
    def test_c_ambiguous_quantity_asks_a_structured_clarification(self, conv):
        t = conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        assert "57" in t.response
        pending = t.pending_after
        assert (pending.kind.value, pending.field) == ("ENTER_FIELD", "ambiguous_quantity")
        assert pending.target["total_quantity"] == 57.0
        assert t.draft()["product"] == "coq"
        assert _created(conv) == []

    @pytest.mark.xfail(strict=True, reason="H2: réponse à une clarification structurée classée UNKNOWN -> RECOVER, le flow propriétaire du pending n'est jamais exécuté")
    def test_d_explicit_split_resolves_to_three_items(self, conv):
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        t = conv.send("50 moutons et 7 chèvres", llm=_UNKNOWN)
        items = t.draft()["additional_items"]
        assert [(i["product"], i["quantity"]) for i in items] == [("mouton", 50.0), ("chevre", 7.0)]
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        conv.send("oui")
        created = _created(conv)
        assert len(created) == 1 and len(created[0][1]["items"]) == 3

    def test_d_split_reply_labelled_as_a_task_reaches_the_owning_flow(self, conv):
        """Même réponse, mais étiquetée NEW_TASK par le LLM : le flow est atteint. Le résultat
        dépend donc aujourd'hui de l'étiquette LLM (voir H2) — ce test fixe le chemin qui marche."""
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        t = conv.send(
            "50 moutons et 7 chèvres",
            llm=new_task("CREATE_RECURRING_NEED", confidence=0.8, product="mouton", quantity=50.0,
                         additional_items=[{"product": "chevre", "quantity": 7.0}]),
        )
        draft = t.draft()
        assert draft["product"] == "coq"
        assert [(i["product"], i["quantity"]) for i in draft["additional_items"]] == [("mouton", 50.0), ("chevre", 7.0)]
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    @pytest.mark.xfail(strict=True, reason="H2: réponse à une clarification structurée classée UNKNOWN -> RECOVER, le flow propriétaire du pending n'est jamais exécuté")
    def test_e_the_rest_is_computed(self, conv):
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        t = conv.send("20 moutons et le reste pour les chèvres", llm=_UNKNOWN)
        items = t.draft()["additional_items"]
        assert [(i["product"], i["quantity"]) for i in items] == [("mouton", 20.0), ("chevre", 37.0)]


# =====================================================================
# F. nouvelle tâche pendant la clarification
# =====================================================================


class TestF_NewTaskDuringClarification:
    def test_a_new_recurring_request_replaces_the_clarification(self, conv):
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        t = conv.send(
            "je veux 30 poulets chaque semaine",
            llm=new_task("CREATE_RECURRING_NEED", product="poulet", quantity=30.0, recurrence_type="WEEKLY"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("poulet", 30.0)
        assert not draft.get("additional_items")
        assert t.pending_after.field != "ambiguous_quantity"
        assert "57" not in t.response


# =====================================================================
# G. "non plutôt 23 boeufs" (CORRECTION)
# =====================================================================


class TestG_Correction:
    def test_single_item_correction_replaces_the_item_and_keeps_the_frequency(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send(
            "non plutôt 23 boeufs",
            llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("boeuf", 23.0, "WEEKLY")
        assert draft["status"] == "DRAFT"
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        assert _created(conv) == []

    def test_ambiguous_multi_item_correction_asks_instead_of_guessing(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        t = conv.send(
            "non plutôt 23 boeufs",
            llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0),
        )
        draft = t.draft()
        assert draft["product"] == "coq" and draft["additional_items"], "le draft ne doit pas être deviné"
        assert t.pending_after.kind.value != "CONFIRM_ACTION"


class TestG_CorrectionPolicy:
    """Politique Phase 2 (C/D) sur le graphe réel."""

    def test_a_reformulation_during_confirmation_is_a_correction_never_a_cancellation(self, conv):
        """« non, plutôt 23 bœufs » : le LLM classe ce message NEW_TASK/même intention (le
        système prompt distingue REJECT — un refus PUR — d'un message qui reformule une
        valeur), `cognitive_guard` ne l'interrompt pas (même intention que le goal courant),
        et le flow le traite comme une correction du draft en cours, jamais une annulation."""
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send(
            "non, plutôt 23 boeufs",
            llm=new_task("CREATE_RECURRING_NEED", confidence=0.9, product="boeuf", quantity=23.0),
        )
        draft = t.draft()
        assert draft["draft_id"] == t1.draft()["draft_id"], "même draft, version +1"
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("boeuf", 23.0, "WEEKLY")
        assert conv.drafts.status_of(draft["draft_id"]) == "DRAFT"
        assert t.decision.get("action") == "CONTINUE_ACTIVE_GOAL", "même intention : jamais une interruption"

    def test_naming_an_item_of_a_multi_item_draft_changes_only_that_item(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        t = conv.send(
            "mets plutôt les chèvres à 23",
            llm=new_task("CREATE_RECURRING_NEED", product="chèvres", quantity=23.0, correction_scope="ITEM"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("coq", 14.0)
        assert [(i["product"], i["quantity"]) for i in draft["additional_items"]] == [("chèvre", 23.0)]

    def test_an_explicit_replace_all_replaces_every_item(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        t = conv.send(
            "remplace tout par 23 boeufs",
            llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0, correction_scope="ALL"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("boeuf", 23.0, "WEEKLY")
        assert not draft.get("additional_items")

    def test_the_ambiguous_correction_is_kept_in_the_question(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        t = conv.send("non plutôt 23 boeufs", llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0))
        pending = t.pending_after
        assert (pending.kind.value, pending.field) == ("ENTER_FIELD", "correction_scope")
        assert pending.target["fields"] == {"product": "boeuf", "quantity": 23.0}

    @pytest.mark.xfail(strict=True, reason="H2/C7: la réponse à la question de portée n'a pas encore de consommateur")
    def test_answering_the_scope_question_applies_the_kept_correction(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_coq_chevre())
        conv.send("non plutôt 23 boeufs", llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0))
        t = conv.send("tout remplacer", llm=_UNKNOWN)
        assert t.draft()["product"] == "boeuf" and not t.draft().get("additional_items")

    def test_a_new_autonomous_request_cancels_the_superseded_draft_durably(self, conv):
        # Premier draft laissé INCOMPLET (ENTER_FIELD recurrence_type, éligible ACTIVE_SLOT —
        # pas de confirmation) pour isoler CETTE propriété de H5 (fast-path numérique pendant
        # une CONFIRMATION), couvert séparément ci-dessous par un xfail permanent. La réponse
        # de l'utilisateur passe d'abord par le micro-prompt ACTIVE_SLOT (contrat DEVIATION),
        # qui retombe ensuite sur le classifieur NEW_TASK — script LLM sensible au prompt reçu
        # pour honorer les DEUX contrats dans le même tour.
        def _deviate_then_new_task(kwargs):
            import json as _json

            if "DEVIATION" in _json.dumps(kwargs.get("messages") or []):
                return {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9}
            return new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY")

        t1 = conv.send("je veux 14 coqs", llm=new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0))
        assert t1.pending_after.field == "recurrence_type"
        conv.send("je veux 20 kg de tomate tous les jours", llm=_deviate_then_new_task)
        assert conv.drafts.status_of(t1.draft()["draft_id"]) == "CANCELLED", "jamais d'orphelin en DRAFT"

    @pytest.mark.xfail(strict=True, reason=(
        "H5: le raccourci déterministe fast-path (une seule valeur numérique typée) "
        "s'exécute AVANT tout appel LLM, même pendant la confirmation d'un tout autre draft "
        "récurrent — il vole silencieusement le nombre (quantité) et perd le reste du message "
        "(produit, fréquence), écrasant le draft en cours au lieu de préserver la nouvelle "
        "demande. Cette classe de bug relève de l'ownership unique de decide_turn (commit 6) : "
        "un raccourci déterministe ne doit jamais pouvoir décider seul qu'un message est une "
        "réponse au slot courant sans consulter la même politique que le LLM."
    ))
    def test_a_fresh_full_request_during_an_unrelated_confirmation_is_never_swallowed_by_the_numeric_shortcut(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send(
            "je veux 20 kg de tomate tous les jours",
            llm=new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY"),
        )
        draft = t.draft()
        assert draft["product"] == "tomate", f"le draft coq a été corrompu par le raccourci numérique : {draft!r}"
        assert conv.drafts.status_of(t1.draft()["draft_id"]) == "CANCELLED"


# =====================================================================
# H/M. annulation ("laisse tomber") — le tour suivant repart propre
# =====================================================================


class TestHM_Cancellation:
    @pytest.mark.xfail(strict=True, reason="H2: réponse à une clarification structurée classée UNKNOWN -> RECOVER, le flow propriétaire du pending n'est jamais exécuté")
    def test_h_bare_abandon_during_clarification_cancels_the_draft(self, conv):
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        t = conv.send("laisse tomber", llm=_UNKNOWN)
        assert _created(conv) == []
        assert t.draft() is None
        assert t.pending_after.kind.value == "NONE"

    def test_m_successful_cancellation_leaves_no_transactional_state(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send("laisse tomber", llm={"disposition": "REJECT", "intent": None, "confidence": 0.9, "entities": {}})
        assert _created(conv) == []
        assert _no_active_transaction(t.after) == []
        assert conv.drafts.status_of(t1.draft()["draft_id"]) == "CANCELLED"

    def test_m_the_turn_after_a_cancellation_starts_clean(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        conv.send("laisse tomber", llm={"disposition": "REJECT", "intent": None, "confidence": 0.9, "entities": {}})
        t = conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        assert t.goal_before is None
        assert "coq" not in t.response.lower()


# =====================================================================
# I. vieux tunnel catalogue actif -> nouvelle demande récurrente
# =====================================================================


def _stale_catalog_state() -> Dict[str, Any]:
    """État laissé par `flows/buyer/procurement.py` après « produit non disponible…
    lancer un appel d'offres ? oui/non » jamais répondu."""
    return {
        "current_goal": "BUYER_REQUEST",
        "status": "WAITING_INPUT",
        "user_role": "BUYER",
        "transaction_payload": {"product": "mais"},
        "working_memory": {
            "active_goal": "BUYER_REQUEST",
            "buyer_request_catalog_checked": True,
            "buyer_request_waiting_choice": True,
            "buyer_request_last_product": "mais",
        },
        "pending_interaction": {
            "kind": "CONFIRM_ACTION", "goal": None, "context_ref": "confirmation",
            "status": "ACTIVE", "created_at": 0, "candidates": [], "field": None, "target": None,
        },
    }


class TestI_StaleCatalogThenRecurring:
    def test_a_clear_recurring_request_escapes_the_stale_catalog_tunnel(self, conv):
        conv.seed(_stale_catalog_state())
        t = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        assert t.goal_before == "BUYER_REQUEST"
        assert t.goal_after == "CREATE_RECURRING_NEED"
        assert t.draft()["product"] == "coq"
        assert "appel d'offres" not in t.response.lower()


# =====================================================================
# J. confirmation répétée / message rejoué
# =====================================================================


class TestJ_RepeatedConfirmation:
    def test_a_second_oui_never_creates_a_second_need(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        conv.send("oui")
        conv.send("oui", llm=_UNKNOWN)
        assert len(_created(conv)) == 1

    def test_the_same_whatsapp_message_replayed_is_processed_once(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        first = conv.send("oui", message_id="wamid.CONFIRM-1")
        replay = conv.send("oui", message_id="wamid.CONFIRM-1")
        assert len(_created(conv)) == 1
        assert first.dispatched and not replay.dispatched
        assert replay.nodes == [], "un message déjà traité ne doit pas rejouer le graphe"


# =====================================================================
# K. nouvelle demande après COMPLETED
# =====================================================================


class TestK_NewRequestAfterCompleted:
    def test_a_new_request_starts_a_fresh_draft(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        first_id = t1.draft()["draft_id"]
        conv.send("oui")
        t3 = conv.send(
            "je veux 20 kg de tomate tous les jours",
            llm=new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY"),
        )
        draft = t3.draft()
        assert draft["draft_id"] != first_id
        assert (draft["product"], draft["unit"], draft["recurrence_type"]) == ("tomate", "KG", "DAILY")
        assert "coq" not in t3.response.lower()


# =====================================================================
# L. interruption courte approuvée par cognitive_guard
# =====================================================================


class TestL_ShortApprovedInterruption:
    @pytest.mark.xfail(strict=True, reason="B3: goal_planner.is_short annule l'interruption approuvée (et fusionne 'maïs' dans le draft)")
    def test_mais_interrupts_and_never_mutates_the_previous_draft(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send("maïs", llm=new_task("BUYER_ADD_TO_CART", product="maïs"))
        assert t.decision.get("action") == "INTERRUPT_ACTIVE_GOAL"
        assert t.goal_after != "CREATE_RECURRING_NEED"
        draft = t.draft()
        assert draft is None or draft["product"] == "coq", "le draft coq ne doit jamais devenir maïs"


# =====================================================================
# N. refus -> le goal ne ressuscite jamais
# =====================================================================


class TestN_RejectionIsTerminal:
    def test_the_rejected_goal_never_resurfaces(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t2 = conv.send("non")
        assert _no_active_transaction(t2.after) == []
        t3 = conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        assert t3.goal_before is None
        assert "confirmez" not in t3.response.lower()

    def test_a_late_oui_after_rejection_claims_nothing(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        conv.send("non")
        conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        t = conv.send("oui", llm=_UNKNOWN)
        assert _created(conv) == []
        assert "noté" not in t.response.lower()


class TestN_RejectDuringSlotFilling:
    """B1 : un REJECT hors confirmation effaçait `current_goal` mais PAS
    `working_memory.active_goal` (clé retirée par `pop`, ignorée par merge_dict)."""

    _REJECT_SLOT = {"disposition": "REJECT", "extracted_entities": {}, "confidence": 0.9}

    def _start(self, conv):
        t = conv.send("je veux 14 coqs", llm=new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0))
        assert (t.pending_after.kind.value, t.pending_after.field) == ("ENTER_FIELD", "recurrence_type")

    def test_the_goal_lock_is_really_released(self, conv):
        self._start(conv)
        t = conv.send("stop", llm=self._REJECT_SLOT)
        assert t.event == "REJECT"
        assert t.goal_after is None
        assert "active_goal" not in (t.after.get("working_memory") or {})

    def test_the_next_turn_does_not_resurrect_the_goal(self, conv):
        self._start(conv)
        conv.send("stop", llm=self._REJECT_SLOT)
        t = conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        assert t.goal_before is None
        assert "confirmez" not in t.response.lower()

    @pytest.mark.xfail(strict=True, reason="C5: recurring_need_draft absent de _purge_transaction_state")
    def test_the_abandoned_draft_does_not_survive(self, conv):
        self._start(conv)
        t = conv.send("stop", llm=self._REJECT_SLOT)
        assert t.draft() is None


# =====================================================================
# O. deux messages quasi simultanés
# =====================================================================


class TestO_ConcurrentMessages:
    @pytest.mark.xfail(strict=True, reason="B6: aucune sérialisation par conversation")
    def test_two_simultaneous_messages_never_run_concurrently(self):
        with ConversationHarness(role="BUYER", channel="webchat") as conv:
            conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
            result = conv.send_concurrently([
                ("je veux 14 coqs chaque semaine", _coq()),
                ("je veux 20 kg de tomate tous les jours",
                 new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY")),
            ])
            assert not result.interleaved, "deux tours de la même conversation se sont entrelacés"
            turns_before = int(result.before.get("turn_count") or 0)
            assert int(result.after.get("turn_count") or 0) == turns_before + 2, "lost update"
