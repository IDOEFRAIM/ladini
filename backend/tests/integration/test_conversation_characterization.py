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
from unittest import mock

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

    def test_the_same_unit_is_shown_before_and_after_confirmation(self, conv):
        """B5 (audit 2026-09-24, fermé commit 8) : `utils.py::_CANONICAL_UNIT_MAP` confondait
        TETE (unité canonique propre à l'élevage — `domain/quantity_unit.py::
        default_unit_for_product`) avec UNITE (générique) — une réponse passée par
        `nodes/memory.py::_resolve_unit_value` réécrivait silencieusement "tete" en "UNITE"."""
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t2 = conv.send("oui")
        assert "TETE" in t2.response
        assert "UNITE" not in t1.response and "TETE" in t1.response

    @pytest.mark.xfail(strict=True, reason="H1: le récapitulatif générique avant confirmation n'affiche pas la fréquence (recurrence_type/weekly_days) — seul RecurringNeedDraft.render_summary() la connaît, le builder générique de confirmation_gate ne le consulte pas")
    def test_the_frequency_is_shown_before_confirmation(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
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

    def test_both_items_share_one_canonical_unit_even_with_mismatched_raw_spelling(self, conv):
        """P1 (audit 2026-09-24, fermé commit 8) : `utils.py::canonical_unit_label` confondait
        TETE (unité canonique de l'élevage) avec UNITE (générique) — et `_clean_additional_items`
        ne canonicalisait PAS du tout la graphie brute d'un item additionnel ("tête" minuscule
        accentué, tel que le LLM peut le renvoyer), contrairement à l'item principal. Les deux
        chemins doivent désormais produire EXACTEMENT "TETE", quelle que soit la graphie reçue."""
        t = conv.send(
            "je veux 14 coqs et 20 chèvres chaque semaine",
            llm=_coq(unit="TETE", additional_items=[{"product": "chèvre", "quantity": 20.0, "unit": "tête"}]),
        )
        draft = t.draft()
        assert draft["unit"] == "TETE"
        assert draft["additional_items"][0]["unit"] == "TETE", (
            f"unité additionnelle non canonicalisée : {draft['additional_items']!r}"
        )
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

    def test_a_fresh_full_request_during_an_unrelated_confirmation_is_never_swallowed_by_the_numeric_shortcut(self, conv):
        # H5 FERMÉ (Phase 2.5) : `_interpret_fast_path` ne décide plus rien pour
        # `_confirmation_correction` dès qu'un classifieur réel est disponible (il
        # s'abstient, `return None` — voir routing.py) ; c'est désormais le
        # classifieur RÉEL (new_task_v2) qui voit tout le message et remonte
        # `recurrence_type`, et `core/turn_policy.py::decide_active_draft_reply`
        # (appelé depuis `flows/buyer/recurring_need.py::_create_flow`) qui
        # tranche NEW_TASK plutôt que CORRECT dès que ce signal est présent —
        # exactement le mandat §9 cas (E)/(F).
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

    def test_the_abandoned_draft_does_not_survive(self, conv):
        self._start(conv)
        t = conv.send("stop", llm=self._REJECT_SLOT)
        assert t.draft() is None


# =====================================================================
# O. deux messages quasi simultanés
# =====================================================================


class TestO_ConcurrentMessages:
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


# =====================================================================
# P. "weekly_days" — champ manquant du registre canonique (mandat C7 §4)
# =====================================================================


class TestP_WeeklyDays:
    """`recurrence_type=WEEKLY_DAYS` laisse `weekly_days` manquant -> l'agent demande "quels
    jours ?" (ENTER_FIELD). Avant l'ajout de `weekly_days` à `core/slots.py::
    _EXPECTED_INPUT_MAP`, cette catégorie n'appartenait pas à `SLOT_FILLING_INPUTS` :
    `interpreter/state_router.py::choose_interpretation_route` routait alors la réponse
    ("lundi mercredi vendredi") vers le classifieur NEW_TASK complet plutôt que vers
    ACTIVE_SLOT (léger, conscient du tunnel) — un `interpreted_event=NEW_TASK` y annule
    silencieusement le draft en cours (`flows/buyer/recurring_need.py::_create_flow`,
    branche "nouvelle demande autonome"), perdant produit/quantité déjà collectés."""

    def test_the_weekly_days_reply_updates_the_same_draft_and_consumes_the_pending(self, conv):
        t1 = conv.send(
            "je veux 20 kg de tomate certains jours de la semaine",
            llm=new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG",
                         recurrence_type="WEEKLY_DAYS"),
        )
        assert t1.pending_after.kind.value == "ENTER_FIELD"
        assert t1.pending_after.field == "weekly_days"
        draft_id = t1.draft()["draft_id"]

        t2 = conv.send(
            "lundi mercredi vendredi",
            llm={"disposition": "ANSWER", "extracted_entities": {"weekly_days": [1, 3, 5]}, "confidence": 0.9},
        )
        draft = t2.draft()
        assert draft["draft_id"] == draft_id, "la réponse doit mettre à jour LE MÊME draft, jamais en recréer un"
        assert draft["product"] == "tomate" and draft["quantity"] == 20.0, "produit/quantité déjà collectés perdus"
        assert sorted(draft["weekly_days"]) == [1, 3, 5]
        assert t2.pending_after.kind.value == "CONFIRM_ACTION", "pending consommé, on avance vers la confirmation"


# =====================================================================
# MONTHLY (Phase 3, mandat "ajouter MONTHLY sans rouvrir l'architecture", §29)
#
# MONTHLY est une VALEUR de plus de `recurrence_type` — aucun nouveau chemin
# conversationnel : chaque test ci-dessous est le miroir EXACT d'un test
# WEEKLY déjà existant plus haut (TestA/B/CDE/F/G/HM/K/I), seule la fréquence
# change. Si un de ces tests échouait alors que son équivalent WEEKLY passe,
# ce serait la preuve que MONTHLY a rouvert l'architecture conversationnelle
# — exactement ce que ce chantier interdit.
# =====================================================================


def _lait_mensuel(**extra: Any) -> Dict[str, Any]:
    return new_task(
        "CREATE_RECURRING_NEED", product="lait", quantity=90.0, unit="L", recurrence_type="MONTHLY", **extra
    )


def _chevres_mensuelles(**extra: Any) -> Dict[str, Any]:
    return new_task("CREATE_RECURRING_NEED", product="chèvre", quantity=35.0, recurrence_type="MONTHLY", **extra)


def _coq_chevre_mensuel() -> Dict[str, Any]:
    return new_task(
        "CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="MONTHLY",
        additional_items=[{"product": "chèvre", "quantity": 20.0}],
    )


def _coq_moutons_chevres_mensuel() -> Dict[str, Any]:
    return new_task(
        "CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="MONTHLY",
        ambiguous_groups=[{"quantity": 57.0, "candidates": ["mouton", "chevre"]}],
    )


class TestMonthlyA_SingleItemMilk:
    """A. "j'ai besoin de 90 L de lait chaque mois" — LE test du bug historique (mandat §30)
    est couvert séparément (`tests/interpreter/test_new_task_micro.py::TestRecurringNeedMonthly`,
    au niveau de l'interpréteur) ; celui-ci vérifie le même message bout-en-bout sur le VRAI
    graphe compilé, miroir exact de `TestA_SingleLivestockWeekly`."""

    def test_draft_is_built_and_confirmation_is_requested(self, conv):
        t = conv.send("j'ai besoin de 90 L de lait chaque mois", llm=_lait_mensuel())
        assert t.error is None
        assert t.event == "NEW_TASK" and t.intent == "CREATE_RECURRING_NEED"
        assert t.goal_after == "CREATE_RECURRING_NEED"
        assert t.tunnel_after == "recurring_need"
        draft = t.draft()
        assert draft["status"] == "DRAFT"
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("lait", 90.0, "MONTHLY")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_confirmation_creates_exactly_one_need_and_leaves_a_clean_state(self, conv):
        conv.send("j'ai besoin de 90 L de lait chaque mois", llm=_lait_mensuel())
        t = conv.send("oui")
        assert len(_created(conv)) == 1
        assert t.goal_after is None
        assert _no_active_transaction(t.after) == []


class TestMonthlyB_SingleItemGoats:
    """B. "je veux 35 chèvres chaque mois" — second smoke-test à un seul item (produit/unité
    différents de A), même invariant."""

    def test_draft_is_built_and_confirmation_is_requested(self, conv):
        t = conv.send("je veux 35 chèvres chaque mois", llm=_chevres_mensuelles())
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("chèvre", 35.0, "MONTHLY")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"


class TestMonthlyC_TwoItems:
    """C. "je veux 14 coqs et 20 chèvres chaque mois" — miroir exact de `TestB_TwoItems`."""

    def test_both_items_reach_the_draft_and_the_single_atomic_create(self, conv):
        t1 = conv.send("je veux 14 coqs et 20 chèvres chaque mois", llm=_coq_chevre_mensuel())
        draft = t1.draft()
        assert draft["product"] == "coq" and draft["recurrence_type"] == "MONTHLY"
        assert [it["product"] for it in draft["additional_items"]] == ["chèvre"]
        conv.send("oui")
        created = _created(conv)
        assert [tool for tool, _ in created] == ["create_recurring_needs"]
        items = created[0][1]["items"]
        assert [(it["product_query"], it["quantity"]) for it in items] == [("coq", 14.0), ("chèvre", 20.0)]


class TestMonthlyD_AmbiguousGroup:
    """D. "je veux 14 coqs et 57 moutons chèvres chaque mois" — miroir exact de
    `TestCDE_AmbiguousGroup`."""

    def test_ambiguous_quantity_asks_a_structured_clarification(self, conv):
        t = conv.send("je veux 14 coqs et 57 moutons chèvres chaque mois", llm=_coq_moutons_chevres_mensuel())
        assert "57" in t.response
        pending = t.pending_after
        assert (pending.kind.value, pending.field) == ("ENTER_FIELD", "ambiguous_quantity")
        assert t.draft()["product"] == "coq" and t.draft()["recurrence_type"] == "MONTHLY"
        assert _created(conv) == []

    def test_explicit_split_resolves_to_three_items_and_keeps_the_frequency(self, conv):
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque mois", llm=_coq_moutons_chevres_mensuel())
        t = conv.send("50 moutons et 7 chèvres", llm=_UNKNOWN)
        draft = t.draft()
        assert draft["recurrence_type"] == "MONTHLY"
        items = draft["additional_items"]
        assert [(i["product"], i["quantity"]) for i in items] == [("mouton", 50.0), ("chevre", 7.0)]
        conv.send("oui")
        created = _created(conv)
        assert len(created) == 1 and len(created[0][1]["items"]) == 3


class TestMonthlyE_Correction:
    """E. "non plutôt 23 boeufs" pendant une confirmation MONTHLY — miroir exact de
    `TestG_CorrectionPolicy::test_a_reformulation_during_confirmation_is_a_correction_never_a_cancellation` :
    ne doit JAMAIS réintroduire H5/H7 (la fréquence MONTHLY doit survivre, la correction reste
    une correction, jamais une nouvelle tâche ni une clarification inutile)."""

    def test_a_reformulation_during_confirmation_is_a_correction_and_keeps_monthly(self, conv):
        t1 = conv.send("je veux 35 chèvres chaque mois", llm=_chevres_mensuelles())
        t = conv.send(
            "non plutôt 23 boeufs",
            llm=new_task("CREATE_RECURRING_NEED", confidence=0.9, product="boeuf", quantity=23.0),
        )
        draft = t.draft()
        assert draft["draft_id"] == t1.draft()["draft_id"], "même draft, version +1"
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("boeuf", 23.0, "MONTHLY")
        assert conv.drafts.status_of(draft["draft_id"]) == "DRAFT"
        assert t.decision.get("action") == "CONTINUE_ACTIVE_GOAL", "même intention : jamais une interruption"


class TestMonthlyF_Interruption:
    """F. Pendant une confirmation MONTHLY, "je veux 30 poulets chaque semaine" — miroir exact
    de `TestF_NewTaskDuringClarification` : le draft MONTHLY précédent est abandonné/superseded
    selon le lifecycle existant, le nouveau draft est WEEKLY — la fréquence précédente ne doit
    jamais fuiter dans le nouveau draft (mandat §15)."""

    def test_a_new_weekly_request_supersedes_the_monthly_draft_without_leaking_its_frequency(self, conv):
        t1 = conv.send("je veux 35 chèvres chaque mois", llm=_chevres_mensuelles())
        t = conv.send(
            "je veux 30 poulets chaque semaine",
            llm=new_task("CREATE_RECURRING_NEED", product="poulet", quantity=30.0, recurrence_type="WEEKLY"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["recurrence_type"]) == ("poulet", 30.0, "WEEKLY")
        assert draft["draft_id"] != t1.draft()["draft_id"]
        assert conv.drafts.status_of(t1.draft()["draft_id"]) == "CANCELLED"


class TestMonthlyG_Cancellation:
    """G. "annule" pendant une confirmation MONTHLY — miroir exact de `TestHM_Cancellation`."""

    def test_cancellation_leaves_no_transactional_state(self, conv):
        t1 = conv.send("je veux 35 chèvres chaque mois", llm=_chevres_mensuelles())
        t = conv.send("annule", llm={"disposition": "REJECT", "intent": None, "confidence": 0.9, "entities": {}})
        assert _created(conv) == []
        assert _no_active_transaction(t.after) == []
        assert conv.drafts.status_of(t1.draft()["draft_id"]) == "CANCELLED"


class TestMonthlyH_NewRequestAfterCompleted:
    """H. Nouvelle tâche après un draft MONTHLY COMPLETED — miroir exact de
    `TestK_NewRequestAfterCompleted`."""

    def test_a_new_request_starts_a_fresh_draft(self, conv):
        t1 = conv.send("je veux 35 chèvres chaque mois", llm=_chevres_mensuelles())
        first_id = t1.draft()["draft_id"]
        conv.send("oui")
        t3 = conv.send(
            "je veux 20 kg de tomate tous les jours",
            llm=new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY"),
        )
        draft = t3.draft()
        assert draft["draft_id"] != first_id
        assert (draft["product"], draft["recurrence_type"]) == ("tomate", "DAILY")
        assert "chèvre" not in t3.response.lower()


class TestMonthlyI_StaleCatalogThenRecurring:
    """I. Vieux tunnel catalogue actif -> nouvelle demande MONTHLY — miroir exact de
    `TestI_StaleCatalogThenRecurring`."""

    def test_a_clear_monthly_request_escapes_the_stale_catalog_tunnel(self, conv):
        conv.seed(_stale_catalog_state())
        t = conv.send("je veux 35 chèvres chaque mois", llm=_chevres_mensuelles())
        assert t.goal_before == "BUYER_REQUEST"
        assert t.goal_after == "CREATE_RECURRING_NEED"
        assert t.draft()["recurrence_type"] == "MONTHLY"
        assert "appel d'offres" not in t.response.lower()


# =====================================================================
# Q. TTL de PendingInteraction (30 min, mandat C7 §8/§9/§10 — décision F)
# =====================================================================


class TestQ_PendingInteractionTTL:
    def test_an_expired_pending_no_longer_captures_the_next_message(self, conv):
        """`core/pending_interaction.py::is_pending_expired` en unité prouve la frontière
        30:00 ; ce test prouve le VRAI effet bout-en-bout : passé le TTL, une confirmation
        `CONFIRM_ACTION` en attente ne bloque plus rien — le tour suivant est routé comme si
        rien n'était en attente, sans ressusciter l'ancien goal (invariant §9 : jamais mélangé
        avec le TTL, différent, du draft abandonné à 24h — voir C5)."""
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        assert t1.pending_after.kind.value == "CONFIRM_ACTION"
        stale = dict(conv.state()["pending_interaction"])
        conv.seed({"pending_interaction": {**stale, "created_at": stale["created_at"] - 1900.0}})

        t2 = conv.send(
            "je veux 20 kg de tomate tous les jours",
            llm=new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY"),
        )
        assert t2.pending_before.kind.value == "NONE", "le pending périmé doit déjà être NONE au début du tour"
        draft = t2.draft()
        assert draft["product"] == "tomate", f"le pending périmé a capturé le message : {draft!r}"
        # La persistance IMMÉDIATE (DB) de l'annulation du VIEUX draft coq est une propriété
        # SÉPARÉE (voir H6, TestH6_OrphanedDraftOnUnconditionalPurge ci-dessous) — pas encore
        # garantie ici : `goal_planner` purge `recurring_need_draft` de l'ÉTAT avant que
        # `_create_flow` ait pu voir l'ancien draft pour le persister CANCELLED. Le draft coq
        # reste néanmoins récupérable par la réconciliation "draft abandonné" (C5, 24h) — jamais
        # perdu, seulement pas encore annulé IMMÉDIATEMENT dans ce cas précis.

    def test_a_pending_well_within_the_ttl_is_unaffected(self, conv):
        """Contrôle négatif : reculer l'horodatage de quelques secondes (toujours dans le TTL)
        ne doit RIEN changer au comportement normal — la garde ne doit pas être trop agressive."""
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        stale = dict(conv.state()["pending_interaction"])
        conv.seed({"pending_interaction": {**stale, "created_at": stale["created_at"] - 5.0}})
        t2 = conv.send("oui")
        assert t2.llm_calls == 0
        assert len(_created(conv)) == 1


class TestH6_OrphanedDraftOnUnconditionalPurge:
    """H6 (découvert en écrivant TestQ_PendingInteractionTTL ci-dessus, commit 7) :
    `interpreter/goal_planner.py`, la règle `event == "NEW_TASK"` (~ligne 847), purge
    INCONDITIONNELLEMENT `recurring_need_draft` (et les 3 autres drafts du registre,
    `core/draft_registry.py`) vers `None` dès qu'AUCUNE `PendingInteraction` n'est active
    (`already_expecting=False`) — AVANT que `flows/buyer/recurring_need.py::_create_flow` ait pu
    voir l'ANCIEN draft pour le persister CANCELLED en base (`_persist`). Le draft reste
    en DRAFT en base, orphelin — récupéré au pire par la réconciliation "draft abandonné" 24h
    (C5), jamais perdu, mais pas annulé IMMÉDIATEMENT comme l'invariant C4/C5 (`_create_flow`)
    le garantit dans TOUS les autres cas testés (`TestG_CorrectionPolicy::
    test_a_new_autonomous_request_cancels_the_superseded_draft_durably`, qui elle passe : la
    différence est qu'un `PendingInteraction` ENTER_FIELD y est encore actif, ce qui fait
    prendre à `goal_planner` une branche PLUS TÔT qui ne purge pas — voir RULE 1/1bis, plus haut
    dans ce fichier — et laisse `_create_flow` décider seul). Avant commit 7 (TTL), ce chemin
    n'était structurellement jamais atteint avec un draft DRAFT encore vivant (un pending était
    TOUJOURS actif dans ce cas) — l'expiration TTL le rend désormais atteignable.

    Fix probable (hors périmètre de C7, big-bang à éviter) : `core/draft_registry.py` pourrait
    porter, par draft, une action de cancellation domaine à appliquer AVANT la purge plutôt
    qu'un simple reset d'état — mais cela touche les 4 domaines (procurement/preorder/
    sales_publish/recurring_need) et leurs 4 stores, à traiter comme un commit dédié."""

    @pytest.mark.xfail(strict=True, reason=(
        "H6: goal_planner purge inconditionnellement recurring_need_draft (event=NEW_TASK, "
        "aucune PendingInteraction active) avant que _create_flow ait pu persister l'annulation "
        "de l'ancien draft DRAFT — orphelin en base jusqu'à la réconciliation 24h (C5), jamais "
        "perdu mais pas annulé immédiatement comme partout ailleurs."
    ))
    def test_a_live_draft_purged_without_an_active_pending_is_cancelled_in_the_store(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        stale = dict(conv.state()["pending_interaction"])
        conv.seed({"pending_interaction": {**stale, "created_at": stale["created_at"] - 1900.0}})
        conv.send(
            "je veux 20 kg de tomate tous les jours",
            llm=new_task("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG", recurrence_type="DAILY"),
        )
        assert conv.drafts.status_of(t1.draft()["draft_id"]) == "CANCELLED"


# =====================================================================
# R. un pending CONSOMMÉ ne capture plus rien au tour suivant (mandat C7 §7/§15 TEST G)
# =====================================================================


class TestR_ConsumedPendingNeverReactivates:
    def test_a_resolved_structured_clarification_is_never_consulted_again(self, conv):
        """`ambiguous_quantity` est résolu (RESOLVED) au tour 2 -> le pending passe à
        CONFIRM_ACTION. Un 3e message, même s'il RESSEMBLE à une réponse de répartition
        ("50/50" ou un texte quelconque), ne doit JAMAIS repasser par
        `_resolve_ambiguous_group_reply` : ce résolveur n'est consulté QUE quand
        `pending.field == 'ambiguous_quantity'`, qui n'est plus le cas une fois consommé."""
        conv.send("je veux 14 coqs et 57 moutons chèvres chaque semaine", llm=_coq_moutons_chevres())
        t2 = conv.send("50 moutons et 7 chèvres", llm=_UNKNOWN)
        assert t2.pending_after.kind.value == "CONFIRM_ACTION"
        assert t2.pending_after.field != "ambiguous_quantity"

        # Un 3e message qui, s'il était (à tort) encore traité par le résolveur de clarification,
        # y serait interprété comme une répartition — mais le pending est déjà CONFIRM_ACTION,
        # donc il doit atteindre la confirmation normale à la place.
        t3 = conv.send("oui")
        assert t3.llm_calls == 0
        created = _created(conv)
        assert len(created) == 1 and len(created[0][1]["items"]) == 3


# =====================================================================
# S. panne à mi-tour, entre la persistance domaine et le flush workspace
# (mandat C10 : preuve contre "partial-turn persistence")
# =====================================================================


class TestS_MidTurnCrashAfterDomainPersist:
    """Fenêtre d'échec RÉELLE et reproduite (mandat C10 §"atomic turn snapshot") :
    `flows/buyer/recurring_need.py` (comme toute mutation transactionnelle de ce moteur)
    persiste durablement le draft DOMAINE (`_persist`, CAS versionné) AVANT de retourner son
    patch d'état à LangGraph. LangGraph ne persiste l'état du WORKSPACE lui-même qu'à la fin
    du tour (`orchestrator.py::_flush_workspace` — voir `workspace/checkpointer.py::
    aput_writes`, "NO database call", tout est staged en RAM jusqu'au flush). Si le PROCESSUS
    meurt (kill -9, OOM, panne hôte — jamais une exception Python : celles-ci sont déjà
    rattrapées par `Orchestrator.handle()` qui flush MÊME sur erreur, voir son `except
    Exception`) exactement entre les deux, la table du draft montre la NOUVELLE valeur mais
    l'état workspace (qui pilote le tour SUIVANT) montre encore l'ANCIENNE — un split-brain
    entre les deux magasins durables.

    Fermer ENTIÈREMENT cette fenêtre exigerait une transaction distribuée entre deux magasins
    indépendants (workspace vs draft) — hors périmètre d'un chantier de durcissement (mandat :
    pas de big-bang). Le filet de sécurité EXISTANT (CAS partout + réconciliation "draft
    abandonné" 24h, C5) borne déjà l'impact à : correction perdue pour CE tour (l'utilisateur
    la retape), jamais une double création, jamais une conversation bloquée, jamais un draft
    orphelin permanent. Ces tests PROUVENT ces 3 garanties précises plutôt que de prétendre
    fermer une fenêtre qu'aucun test Python ne peut réellement fermer (un vrai kill -9 ne se
    simule pas par une exception — celle-ci est délibérément injectée ICI juste après le
    `_persist` domaine, pour se placer exactement dans la fenêtre visée)."""

    def test_a_correction_that_crashes_right_after_its_domain_persist_is_never_lost_twice(self, conv):
        """Le draft domaine porte la correction (persistée) ; l'état workspace, lui, ne l'a
        jamais vue (jamais retournée à LangGraph) — split-brain reproduit. Le tour SUIVANT ne
        doit ni planter, ni dupliquer, ni ressusciter un état incohérent : il repart propre."""
        import ladini.graphs.agents.market_coach.flows.buyer.recurring_need as rn

        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        draft_id = t1.draft()["draft_id"]

        async def crashing_correct(draft, said, state, conversation_id):
            action = rn.plan_correction(
                draft, said, scope=(state.get("extracted_entities") or {}).get("correction_scope")
            )
            outcome = rn.apply_domain_action(draft, action)
            await rn._persist(draft, outcome.draft, conversation_id)
            raise RuntimeError("panne processus simulée juste après la persistance domaine")

        with mock.patch.object(rn, "_correct", crashing_correct):
            t2 = conv.send(
                "non plutôt 23 boeufs",
                llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0),
            )
        assert t2.error is None, "une panne mi-tour ne doit jamais remonter comme une exception non gérée"

        # Split-brain confirmé : le magasin domaine a la correction, l'état workspace ne l'a pas.
        db_draft = conv._run(conv.drafts.load(draft_id))
        assert (db_draft.product, db_draft.quantity) == ("boeuf", 23.0)
        assert t2.after.get("recurring_need_draft", {}).get("product") == "coq"

        # Garantie 1 : le tour SUIVANT n'est jamais bloqué (pas de crash-loop, pas de pending
        # fantôme qui capturerait un message sans rapport).
        t3 = conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        assert t3.error is None

        # Garantie 2 : jamais de double création — le draft orphelin ("boeuf") ne peut plus
        # être confirmé par une conversation qui ne le connaît plus (il n'apparaît dans aucun
        # `current_goal`/`pending_interaction` retrouvable).
        assert _created(conv) == []

        # Garantie 3 : le draft orphelin reste repérable par la réconciliation "draft abandonné"
        # 24h (C5) — jamais perdu silencieusement de la base, jamais réactivable autrement.
        assert conv.drafts.status_of(draft_id) == "DRAFT"

    def test_a_cancellation_that_crashes_right_after_its_domain_persist_still_recovers_cleanly(self, conv):
        """Même fenêtre, sur le chemin d'annulation (`CancelRecurringNeedDraft`) : le draft est
        déjà CANCELLED en base quand la panne survient — un état TERMINAL, donc aucun risque
        de réutilisation ultérieure même si l'état workspace ne le reflète jamais."""
        import ladini.graphs.agents.market_coach.flows.buyer.recurring_need as rn

        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        draft_id = t1.draft()["draft_id"]

        original_persist = rn._persist

        async def crashing_persist(before, after, conversation_id):
            result = await original_persist(before, after, conversation_id)
            assert result is True, "la persistance domaine elle-même doit réussir avant la panne simulée"
            raise RuntimeError("panne processus simulée juste après la persistance domaine")

        with mock.patch.object(rn, "_persist", crashing_persist):
            t2 = conv.send(
                "laisse tomber",
                llm={"disposition": "REJECT", "intent": None, "confidence": 0.9, "entities": {}},
            )
        assert t2.error is None

        db_draft = conv._run(conv.drafts.load(draft_id))
        assert db_draft.status.value == "CANCELLED"

        t3 = conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        assert t3.error is None
        assert _created(conv) == []


# =====================================================================
# Q. Quantité orpheline multi-item (bug réel 2026-09-26) : "150 kg tomate et
#    200 kg chaque semaine" ne doit JAMAIS devenir "350 kg de tomate" — une
#    quantité sans produit rattaché doit être clarifiée, jamais sommée.
# =====================================================================


def _tomate_weekly(**extra: Any) -> Dict[str, Any]:
    return new_task(
        "CREATE_RECURRING_NEED", product="tomate", quantity=150.0, unit="KG",
        recurrence_type="WEEKLY", **extra,
    )


class TestQ_OrphanQuantity:
    def test_a_orphan_quantity_triggers_clarification_never_a_sum(self, conv):
        """Cas A du mandat : le texte porte DEUX paires quantité+unité convertibles ('150 kg' et
        '200 kg') mais un SEUL produit nommé ('tomate') — la garde générique de
        `new_task_micro.py::_finalize` doit détecter la deuxième quantité comme orpheline et
        JAMAIS laisser `parse_compound_quantity` la refusionner en 350 (l'ancien bug réel)."""
        t = conv.send(
            "j ai besoin de 150 kg tomate et 200 kg chaque semaine", llm=_tomate_weekly()
        )
        assert t.error is None
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["unit"]) == ("tomate", 150.0, "KG")
        assert "350" not in t.response
        assert "200" in t.response
        pending = t.pending_after
        assert (pending.kind.value, pending.field) == ("ENTER_FIELD", "orphan_quantity")
        assert pending.target == {"quantity": 200.0, "unit": "KG"}
        assert _created(conv) == []

    def test_b_two_named_products_reach_the_draft_directly_no_clarification(self, conv):
        """Cas B : les DEUX quantités ont chacune un produit explicite — `additional_items`
        porte déjà l'information, aucune ambiguïté, aucune clarification orpheline."""
        t = conv.send(
            "j ai besoin de 150 kg tomate et 200 kg oignon chaque semaine",
            llm=_tomate_weekly(additional_items=[{"product": "oignon", "quantity": 200.0, "unit": "KG"}]),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("tomate", 150.0)
        assert [(i["product"], i["quantity"]) for i in draft["additional_items"]] == [("oignon", 200.0)]
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_c_three_named_products_reach_the_draft(self, conv):
        """Cas C : trois produits, chacun avec sa propre quantité explicite."""
        t = conv.send(
            "150 kg tomates, 200 kg oignons et 50 kg carottes chaque semaine",
            llm=_tomate_weekly(
                additional_items=[
                    {"product": "oignon", "quantity": 200.0, "unit": "KG"},
                    {"product": "carotte", "quantity": 50.0, "unit": "KG"},
                ]
            ),
        )
        draft = t.draft()
        assert [(i["product"], i["quantity"]) for i in draft["additional_items"]] == [
            ("oignon", 200.0), ("carotte", 50.0),
        ]
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_d_a_price_is_never_read_as_a_second_orphan_quantity(self, conv):
        """Cas D : '200 FCFA le kg' est un PRIX (`price`/`price_unit`), pas une deuxième
        quantité de produit — `find_convertible_quantity_pairs` ne reconnaît que des unités de
        poids/volume/comptage (jamais 'fcfa'), donc une seule paire trouvée, aucune détection."""
        t = conv.send(
            "je veux 150 kg de tomates a 200 fcfa le kg chaque semaine",
            llm=_tomate_weekly(price=200.0, price_unit="KG"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("tomate", 150.0)
        assert not draft.get("additional_items")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_e_a_duration_is_never_read_as_a_second_orphan_quantity(self, conv):
        """Cas E : 'pendant 3 mois' est une DURÉE, jamais une quantité ('mois' n'est pas une
        unité de produit reconnue) — la quantité reste 150, jamais recalculée."""
        t = conv.send(
            "je veux 150 kg de tomates chaque semaine pendant 3 mois",
            llm=_tomate_weekly(),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("tomate", 150.0)
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_f_completing_the_missing_product_reaches_a_full_two_item_confirmation_and_creation(self, conv):
        """Cas F, bout en bout : clarification -> réponse 'oignons' -> draft complet à 2 items ->
        confirmation -> création atomique (mandat §6, exemple donné mot pour mot)."""
        t1 = conv.send(
            "j ai besoin de 150 kg tomate et 200 kg chaque semaine", llm=_tomate_weekly()
        )
        assert t1.pending_after.field == "orphan_quantity"

        t2 = conv.send("oignons", llm=_UNKNOWN)
        draft = t2.draft()
        assert (draft["product"], draft["quantity"]) == ("tomate", 150.0)
        # Le produit répondu est repris TEL QUEL (`_sanitize_product_candidate`, jamais résolu
        # contre le catalogue ici) — même convention que le produit principal côté LLM.
        assert [(i["product"], i["quantity"], i["unit"]) for i in draft["additional_items"]] == [
            ("oignons", 200.0, "KG")
        ]
        assert t2.pending_after.kind.value == "CONFIRM_ACTION"

        t3 = conv.send("oui")
        assert t3.llm_calls == 0
        created = _created(conv)
        assert [tool for tool, _ in created] == ["create_recurring_needs"]
        items = created[0][1]["items"]
        assert [(it["product_query"], it["quantity"]) for it in items] == [
            ("tomate", 150.0), ("oignons", 200.0),
        ]

    def test_g_livestock_orphan_quantity_without_any_literal_unit_never_becomes_a_sum(self, conv):
        """Cas G : '40 chèvres et 20 chaque semaine' — aucun mot d'unité littéral pour distinguer
        les deux nombres au niveau texte (contrairement aux cas A/B/C en KG) : cette détection
        dépend du prompt à jour (`orphan_quantities` rempli par le LLM, jamais un hack lexical
        sur 'chèvre'). Le test fixe le comportement attendu UNE FOIS le LLM correctement
        instruit : jamais 60 chèvres, toujours une clarification."""
        t = conv.send(
            "j ai besoin de 40 chevres et 20 chaque semaine",
            llm=new_task(
                "CREATE_RECURRING_NEED", product="chevre", quantity=40.0, unit="TETE",
                recurrence_type="WEEKLY", orphan_quantities=[{"quantity": 20.0}],
            ),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("chevre", 40.0)
        assert "60" not in t.response
        pending = t.pending_after
        assert (pending.kind.value, pending.field) == ("ENTER_FIELD", "orphan_quantity")

        t2 = conv.send("chevres", llm=_UNKNOWN)
        draft2 = t2.draft()
        assert draft2["quantity"] == 40.0
        assert [(i["product"], i["quantity"]) for i in draft2["additional_items"]] == [("chevres", 20.0)]

    def test_a_new_autonomous_task_during_orphan_clarification_replaces_it(self, conv):
        """Mandat §5 : une tâche autonome tapée pendant la clarification ('je veux 30 poulets
        chaque semaine', qui contient un CHIFFRE) ne doit jamais être lue comme un nom de
        produit répondant 'à quel produit ?' — elle remplace le draft, et la quantité orpheline
        du tour précédent (200 KG tomate) ne doit plus jamais réapparaître (non-régression du
        même bug de collage déjà fermé pour `ambiguous_groups`, `nodes/memory.py`)."""
        t1 = conv.send(
            "j ai besoin de 150 kg tomate et 200 kg chaque semaine", llm=_tomate_weekly()
        )
        assert t1.pending_after.field == "orphan_quantity"

        t2 = conv.send(
            "je veux 30 poulets chaque semaine",
            llm=new_task("CREATE_RECURRING_NEED", product="poulet", quantity=30.0, recurrence_type="WEEKLY"),
        )
        draft = t2.draft()
        assert (draft["product"], draft["quantity"]) == ("poulet", 30.0)
        assert not draft.get("additional_items")
        assert t2.pending_after.field != "orphan_quantity"
        assert "200" not in t2.response

        # Non-régression du collage (nodes/memory.py) : un troisième tour, neutre, ne doit
        # jamais faire réapparaître l'orphelin "200 KG" d'il y a deux tours.
        t3 = conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        assert "200" not in t3.response

    def test_bare_abandon_during_orphan_clarification_cancels_cleanly(self, conv):
        conv.send("j ai besoin de 150 kg tomate et 200 kg chaque semaine", llm=_tomate_weekly())
        t2 = conv.send("laisse tomber", llm=_UNKNOWN)
        assert t2.draft() is None
        assert t2.pending_after.kind.value == "NONE"
        assert _created(conv) == []

    def test_correction_after_a_valid_two_item_confirmation_targets_only_the_named_item(self, conv):
        """Mandat §7 : la correction d'un item nommé ('non plutôt 250 kg d'oignons') après un
        récap correct à 2 items ne touche JAMAIS l'autre item (tomate reste à 150 KG) — vérifie
        que la garde orpheline ne casse pas la politique de correction ciblée existante."""
        conv.send(
            "150 kg tomates et 200 kg oignons chaque semaine",
            llm=_tomate_weekly(additional_items=[{"product": "oignon", "quantity": 200.0, "unit": "KG"}]),
        )
        t2 = conv.send(
            "non plutot 250 kg d oignons",
            llm=new_task("CREATE_RECURRING_NEED", product="oignon", quantity=250.0, unit="KG"),
        )
        draft = t2.draft()
        assert (draft["product"], draft["quantity"]) == ("tomate", 150.0)
        assert [(i["product"], i["quantity"]) for i in draft["additional_items"]] == [("oignon", 250.0)]


# =====================================================================
# R. Quantité orpheline SANS unité littérale (suite du bug 2026-09-26) : le
#    filet de TestQ_OrphanQuantity ne voit que le KG/TONNE — "40 chèvres et
#    20 chaque semaine" (bétail compté en TETE, aucun mot d'unité écrit)
#    échappait entièrement à la détection Python, laissant le risque
#    ENTIER reposer sur le LLM. `find_bare_number_candidates` (générique :
#    durée/prix/plage, jamais un nom d'espèce) comble ce trou.
# =====================================================================


def _chevre_weekly(**extra: Any) -> Dict[str, Any]:
    return new_task(
        "CREATE_RECURRING_NEED", product="chevre", quantity=40.0, recurrence_type="WEEKLY", **extra,
    )


class TestR_BareOrphanQuantity:
    def test_a_bare_orphan_quantity_triggers_clarification_never_a_sum(self, conv):
        """Cas A : reproduit EXACTEMENT le scénario du suivi bloquant — le LLM ne renvoie NI
        `additional_items` NI `orphan_quantities` (stub réaliste d'un modèle qui n'a pas
        (encore) suivi la consigne du prompt) : c'est le filet Python SEUL, sur un nombre SANS
        aucune unité littérale ('20', pas '20 kg'), qui doit détecter l'orphelin. Avant ce
        correctif, ce tour allait directement à CONFIRM_ACTION avec quantity=40 et '20'
        silencieusement perdu (jamais sommé à 60 dans ce cas précis, mais jamais non plus
        signalé — voir le rapport de mission pour la preuve par `git stash`)."""
        t = conv.send(
            "j ai besoin de 40 chevres et 20 chaque semaine", llm=_chevre_weekly()
        )
        assert t.error is None
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["unit"]) == ("chevre", 40.0, "TETE")
        assert "60" not in t.response
        assert "20" in t.response
        pending = t.pending_after
        assert (pending.kind.value, pending.field) == ("ENTER_FIELD", "orphan_quantity")
        assert pending.target == {"quantity": 20.0, "unit": None}
        assert _created(conv) == []

    def test_b_two_named_livestock_products_reach_the_draft_directly(self, conv):
        """Cas B : les deux quantités ont chacune un produit explicite (le LLM peuple
        `additional_items`) — aucune ambiguïté, la garde orpheline ne doit rien changer."""
        t = conv.send(
            "j ai besoin de 40 chevres et 20 moutons chaque semaine",
            llm=_chevre_weekly(additional_items=[{"product": "mouton", "quantity": 20.0, "unit": "TETE"}]),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"], draft["unit"]) == ("chevre", 40.0, "TETE")
        assert [(i["product"], i["quantity"], i["unit"]) for i in draft["additional_items"]] == [
            ("mouton", 20.0, "TETE")
        ]
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_c_a_duration_is_never_read_as_a_bare_orphan_quantity(self, conv):
        """Cas C : 'pendant 2 semaines' est une DURÉE (mot de durée immédiatement après le
        nombre) — jamais une deuxième quantité, quelle que soit l'unité du produit principal."""
        t = conv.send("40 chevres pendant 2 semaines", llm=_chevre_weekly())
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("chevre", 40.0)
        assert not draft.get("additional_items")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_d_a_price_is_never_read_as_a_bare_orphan_quantity(self, conv):
        """Cas D : '20000 FCFA la tête' est un PRIX (mot de devise immédiatement après le
        nombre) — jamais une deuxième quantité de bétail."""
        t = conv.send(
            "40 chevres a 20000 fcfa la tete chaque semaine",
            llm=_chevre_weekly(price=20000.0, price_unit="TETE"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("chevre", 40.0)
        assert not draft.get("additional_items")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    def test_e_a_cadence_is_never_read_as_a_bare_orphan_quantity(self, conv):
        """Cas E : 'chaque 2 semaines' — le nombre de la CADENCE est immédiatement suivi d'un
        mot de durée, exactement comme 'pendant 2 semaines' (même garde générique) — jamais une
        deuxième quantité."""
        t = conv.send("40 chevres chaque 2 semaines", llm=_chevre_weekly())
        draft = t.draft()
        assert draft["quantity"] == 40.0
        assert not draft.get("additional_items")
        # (le champ 'field' peut être 'weekly_days'/autre selon le modèle de récurrence — seul
        # le NON-déclenchement de la clarification orpheline importe ici)
        assert t.pending_after.field != "orphan_quantity"

    def test_f_completing_the_missing_livestock_product_reaches_full_confirmation_and_creation(self, conv):
        """Cas F, bout en bout : clarification -> réponse 'moutons' -> draft complet à 2 items ->
        confirmation -> création atomique."""
        t1 = conv.send(
            "j ai besoin de 40 chevres et 20 chaque semaine", llm=_chevre_weekly()
        )
        assert t1.pending_after.field == "orphan_quantity"

        t2 = conv.send("moutons", llm=_UNKNOWN)
        draft = t2.draft()
        assert (draft["product"], draft["quantity"], draft["unit"]) == ("chevre", 40.0, "TETE")
        assert [(i["product"], i["quantity"], i["unit"]) for i in draft["additional_items"]] == [
            ("moutons", 20.0, "TETE")
        ]
        assert t2.pending_after.kind.value == "CONFIRM_ACTION"

        t3 = conv.send("oui")
        assert t3.llm_calls == 0
        created = _created(conv)
        assert [tool for tool, _ in created] == ["create_recurring_needs"]
        items = created[0][1]["items"]
        assert [(it["product_query"], it["quantity"]) for it in items] == [
            ("chevre", 40.0), ("moutons", 20.0),
        ]

    def test_g_poultry_bare_orphan_quantity_never_becomes_a_sum(self, conv):
        """Cas G : généralisation au-delà du bétail caprin/ovin — même mécanisme générique
        (aucun mot 'poulet' en dur dans le garde-fou), sur une autre espèce."""
        t = conv.send(
            "10 poulets et 5 chaque semaine",
            llm=new_task("CREATE_RECURRING_NEED", product="poulet", quantity=10.0, recurrence_type="WEEKLY"),
        )
        draft = t.draft()
        assert (draft["product"], draft["quantity"]) == ("poulet", 10.0)
        assert "15" not in t.response
        assert t.pending_after.field == "orphan_quantity"
        assert t.pending_after.target == {"quantity": 5.0, "unit": None}

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Limite honnête (mandat suivi 2026-09-26, §7 cas H) : la garde orpheline générique "
            "détecte bien la clarification pour '8 caisses de tomates et 4 chaque semaine' "
            "(prouvé par ailleurs), mais 'CAISSE' n'est PAS une unité canonique du domaine "
            "(absente de `UNIT_SYNONYMS`, `domain/quantity_unit.py`) — le produit principal lui-"
            "même retombe donc sur le défaut KG plutôt que de préserver 'caisse', une limite "
            "PRÉEXISTANTE du système d'unités (pas introduite ici) que ce correctif n'étend pas "
            "le mandat à résoudre. Ce test fixe le comportement voulu UNE FOIS 'CAISSE' un "
            "défaut canonique reconnu — jamais un hack ad hoc ici."
        ),
    )
    def test_h_crates_orphan_quantity_preserves_the_crate_unit(self, conv):
        t = conv.send(
            "8 caisses de tomates et 4 chaque semaine",
            llm=new_task(
                "CREATE_RECURRING_NEED", product="tomate", quantity=8.0, unit="CAISSE",
                recurrence_type="WEEKLY",
            ),
        )
        draft = t.draft()
        assert draft["unit"] == "CAISSE"
        assert t.pending_after.field == "orphan_quantity"

