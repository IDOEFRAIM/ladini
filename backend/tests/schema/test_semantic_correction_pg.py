"""B24-correction — menus de commande (annuler / ignorer / suspendre / changer UNE livraison) contre un VRAI PostgreSQL.

Seuls LLM (scripté), Redis et dispatcher sont doublés ; la liste, le détail, la mise à jour et la matérialisation des occurrences
passent par le vrai `AgriDatabaseService`. On lit ce qui est RÉELLEMENT écrit : `recurring_needs` (durable) et
`recurring_need_occurrences` (une livraison) ne sont jamais confondus.
"""
from __future__ import annotations

import pytest

from tests.harness import new_task
from tests.integration.test_semantic_context_arbitration_e2e import UPDATE_QTY_3, ScriptLLM
from tests.schema import test_recurring_entrypoint_pg as entry
from tests.schema import test_semantic_context_arbitration_pg as base
from tests.schema.test_recurring_entrypoint_pg import Conv, _user, real_db  # noqa: F401
from tests.schema.test_semantic_context_arbitration_pg import (  # noqa: F401
    _account,
    _boeuf_detail,
    _need_ids,
    _real_recurring_tools,
    _rows,
)

pytestmark = pytest.mark.integration

UNKNOWN = entry.UNKNOWN
CANCEL = new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", update_action="CANCEL")
SKIP = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="SKIP_OCCURRENCE")
PAUSE = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="PAUSE")
RESUME = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="RESUME")
OVERRIDE_10 = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="OVERRIDE_OCCURRENCE", quantity=10.0)


def _occurrences(real_db, need_id):  # noqa: F811
    return entry._sql(
        real_db,
        "select occurrence_date::date::text, status, requested_quantity from marketplace.recurring_need_occurrences "
        "where recurring_need_id = %s order by occurrence_date", (need_id,))


def _one_need(real_db, account):  # noqa: F811
    (need_id,) = [r[0] for r in _need_ids(real_db, account)]
    return need_id


@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_cancel_menu_option_1_skips_exactly_one_occurrence_and_the_need_continues(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        occ_before = _occurrences(real_db, need_id)
        assert occ_before and occ_before[0][1] == "OPEN"
        menu = conv.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        assert "1. Ignorer seulement la prochaine livraison" in menu.response and "update_recurring_need" not in menu.mcp_tools()
        assert _occurrences(real_db, need_id) == occ_before and _rows(real_db, account)[0][3] == "ACTIVE", "rien avant la réponse fermée"
        done = conv.send("1", llm=UNKNOWN)
        assert "update_recurring_need" in done.mcp_tools() and "ignorée" in done.response, done.response
        after = _occurrences(real_db, need_id)
        assert after[0][0] == occ_before[0][0] and after[0][1] == "SKIPPED", "la livraison affichée est ignorée"
        assert _rows(real_db, account) == [("boeuf", 2.0, "TETE", "ACTIVE", "WEEKLY")], "le besoin durable est intact"
        assert [r[0] for r in _need_ids(real_db, account)] == [need_id]


def test_cancel_menu_option_2_needs_the_second_confirmation_then_cancels_the_need(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        ask = conv.send("2", llm=UNKNOWN)
        assert "définitive" in ask.response and _rows(real_db, account)[0][3] == "ACTIVE"
        done = conv.send("1", llm=UNKNOWN)
        assert "arrêté" in done.response, done.response
        assert _rows(real_db, account)[0][3] == "CANCELLED"
        assert all(o[1] in {"CANCELLED", "SKIPPED"} for o in _occurrences(real_db, need_id)), _occurrences(real_db, need_id)
        # restart-like : nouvelle invocation, mêmes valeurs lues de la base
        listing = conv.send("mes besoins", llm=UNKNOWN).response
        assert "Annulé" in listing or "annulé" in listing or "pas encore de besoin" in listing, listing


def test_cancel_menu_option_3_and_free_text_change_nothing(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        before = (_rows(real_db, account), _occurrences(real_db, need_id))
        conv.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        conv.send("arrête tout", llm=ScriptLLM(None, selection="fake_1"))  # texte libre + SELECTION 1 halluciné
        assert (_rows(real_db, account), _occurrences(real_db, need_id)) == before
        t = conv.send("3", llm=UNKNOWN)
        assert "Besoin :" in t.response and (_rows(real_db, account), _occurrences(real_db, need_id)) == before


def test_pause_then_resume_through_confirmed_snapshots(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        conv.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        assert _rows(real_db, account)[0][3] == "ACTIVE"
        conv.send("1", llm=UNKNOWN)
        assert _rows(real_db, account)[0][3] == "PAUSED"
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("1", llm=UNKNOWN)
        conv.send("reprends mes boeufs", llm=ScriptLLM(RESUME))
        assert _rows(real_db, account)[0][3] == "PAUSED", "pas de reprise avant la réponse fermée"
        conv.send("oui", llm=UNKNOWN)
        assert _rows(real_db, account)[0][3] == "ACTIVE"


def test_override_changes_one_delivery_only_and_skip_message_leaves_the_need(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        first_date = _occurrences(real_db, need_id)[0][0]
        conv.send("pour cette fois mets 10", llm=ScriptLLM(OVERRIDE_10))
        assert _occurrences(real_db, need_id)[0][2] == 2.0, "rien avant la confirmation"
        conv.send("1", llm=UNKNOWN)
        occ = _occurrences(real_db, need_id)[0]
        assert occ[0] == first_date and occ[2] == 10.0
        assert _rows(real_db, account) == [("boeuf", 2.0, "TETE", "ACTIVE", "WEEKLY")], "le besoin durable garde 2"
        # un UPDATE permanent ne piétine pas l'exception d'occurrence (contrat du service, prouvé de bout en bout)
        conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert _rows(real_db, account)[0][1] == 3.0 and _occurrences(real_db, need_id)[0][2] == 10.0


def test_ownership_a_forged_snapshot_for_another_buyers_need_is_refused_by_the_service(real_db):  # noqa: F811
    """Le menu snapshot porte l'id d'un besoin d'un AUTRE acheteur (menu périmé/forgé) : le service refuse, rien n'est écrit."""
    owner = _account(real_db, "BUYER")
    intruder = _account(real_db, "ADMIN")
    with Conv(real_db, owner, profile_role="BUYER") as conv:
        conv.send("boeuf 2 tete chaque semaine", llm=base.BOEUF_LLM)
        conv.send("Confirmer")
    foreign_id = _one_need(real_db, owner)
    before = (_rows(real_db, owner), _occurrences(real_db, foreign_id))
    with Conv(real_db, intruder, profile_role="ADMIN") as conv:
        conv.send("boeuf 5 tete chaque mois", llm=new_task("CREATE_RECURRING_NEED", product="Boeuf", quantity=5.0, unit="TETE",
                                                          recurrence_type="MONTHLY"))
        conv.send("Confirmer")
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("1", llm=UNKNOWN)
        conv.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        # on remplace l'id du snapshot par celui du besoin d'un autre acheteur, comme le ferait un menu forgé
        menu = conv.state()["working_memory"]["recurring_need_menu"]
        forged = {**menu["commands"]["1"], "recurring_need_id": foreign_id}
        conv.harness.seed({"working_memory": {"recurring_need_menu": {"commands": {"1": forged}}}})
        assert conv.state()["working_memory"]["recurring_need_menu"]["commands"]["1"]["recurring_need_id"] == foreign_id
        t = conv.send("1", llm=UNKNOWN)
        assert "Rien n'a été modifié" in t.response, t.response
    assert (_rows(real_db, owner), _occurrences(real_db, foreign_id)) == before, "aucune fuite cross-buyer"


def _more_needs(conv):
    for product, qty in (("chevre", 3.0), ("mouton", 4.0)):
        conv.send(f"{product} {qty:g} tete chaque semaine", llm=ScriptLLM(new_task(
            "CREATE_RECURRING_NEED", 0.95, product=product, quantity=qty, unit="TETE", recurrence_type="WEEKLY")))
        conv.send("Confirmer")


def test_a_menu_index_that_the_screen_does_not_show_never_resolves_to_an_older_screens_item(real_db):  # noqa: F811
    """`working_memory` fusionne en profondeur : l'index « 3 » de la LISTE ne doit pas survivre sur l'écran de détail (2 entrées)."""
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        _more_needs(conv)
        assert len(_rows(real_db, account)) == 3
        conv.send("mes besoins", llm=UNKNOWN)
        first = conv.send("1", llm=UNKNOWN)
        assert "Besoin :" in first.response and "2. Retour" in first.response and "3." not in first.response
        t = conv.send("3", llm=UNKNOWN)
        assert "Besoin : 3" not in t.response and "Mouton" not in t.response and "Chevre" not in t.response, t.response
        assert "refresh_recurring_need_matching" not in t.mcp_tools() and "update_recurring_need" not in t.mcp_tools()


def test_a_free_text_selection_on_the_list_needs_evidence_in_the_message(real_db):  # noqa: F811
    """`mets-en 3` + SELECTION 3 halluciné sur la LISTE : le « 3 » est une quantité, pas un numéro d'écran."""
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        _more_needs(conv)
        conv.send("mes besoins", llm=UNKNOWN)
        t = conv.send("mets-en 3", llm=ScriptLLM(None, selection="fake_1"))
        assert "Besoin :" not in t.response, "aucun écran de détail ouvert sur une sélection non étayée"
        assert "update_recurring_need" not in t.mcp_tools()
        assert [r[1] for r in _rows(real_db, account)] == [2.0, 3.0, 4.0]
