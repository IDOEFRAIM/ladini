"""B22 — une confirmation interactive par (draft, version), sur le VRAI graphe et un VRAI PostgreSQL.

Smoke prod : « Boeufs : 2 TETE / Chaque semaine » -> le MÊME récapitulatif « Confirmez-vous cette opération pour 2 TETE de
boeufs ? » est parti DEUX fois avant `Confirmer`. Cause prouvée ici : chaque message entrant qui ne change pas le draft
(message rejoué sous un autre identifiant, relance « à partir de demain » non comprise, rejeu de la création) repasse par
`_create_flow` -> `DRAFT_UNCHANGED` -> le récapitulatif est REGÉNÉRÉ et ré-émis, identique au premier. La couche
d'envoi (`ResponseDispatcher`, clé `{event_id}:{index}`) déduplique correctement UN événement : elle ne peut pas voir que
deux événements DIFFÉRENTS portent la même confirmation. L'invariant est donc posé au niveau du draft.
"""
from __future__ import annotations

from typing import List

import pytest
from test_recurring_entrypoint_pg import (  # noqa: F401  (fixture)
    Conv,
    _needs_of,
    _user,
    real_db,
)
from test_recurring_entrypoint_pg import (
    real_db as entrypoint_real_db,  # noqa: F401  (fixture, importée sous alias)
)

from tests.harness import new_task

pytestmark = pytest.mark.integration


@pytest.fixture()
def real_db(entrypoint_real_db):  # noqa: F811
    """PostgreSQL de test branché sur `core.database` (fixture du test d'entrée B21.1)."""
    return entrypoint_real_db

UNKNOWN = {"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}}
BOEUF = new_task("CREATE_RECURRING_NEED", product="Boeufs", quantity=2.0, unit="TETE", recurrence_type="WEEKLY")
CONFIRM_MARK = "Confirmez-vous"


def _outbound_texts(conv: Conv) -> List[str]:
    out: List[str] = []
    for _phone, plan in conv.harness.dispatcher.sent:
        out += [getattr(item, "text", "") or "" for item in getattr(plan, "items", ())]
    return out


def _confirmation_prompts(conv: Conv) -> int:
    return sum(1 for t in _outbound_texts(conv) if CONFIRM_MARK in t)


def _admin(dsn):
    return _user(dsn, role="ADMIN", name="Admin Ido")


# ── la baseline : un seul envoi, un seul besoin ────────────────────────────────────────────────────────────────────
def test_smoke_prod_creation_sends_one_confirmation_one_success_and_lists_the_need(real_db):
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        t1 = conv.send("Boeufs : 2 TETE\nChaque semaine\nÀ partir de demain", llm=BOEUF)
        assert CONFIRM_MARK in t1.response and len(t1.dispatched) == 1
        t2 = conv.send("Confirmer")
        assert "C'est noté" in t2.response and len(t2.dispatched) == 1
        assert _confirmation_prompts(conv) == 1
        assert [(r[0].lower(), float(r[1]), r[2]) for r in _needs_of(real_db, admin["phone"])] == [("boeufs", 2.0, "TETE")]
        t3 = conv.send("mes besoins", llm=UNKNOWN)
        assert "Boeufs" in t3.response and "2 TETE chaque semaine" in t3.response


# ── ROUGE avant correctif : la même confirmation part deux fois ────────────────────────────────────────────────────
def test_the_creation_message_delivered_twice_emits_one_confirmation(real_db):
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF)
        again = conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF)  # autre wamid : rejeu / double envoi de l'utilisateur
        assert _confirmation_prompts(conv) == 1, _outbound_texts(conv)
        assert again.error is None and again.response  # l'utilisateur n'est pas laissé sans réponse
        conv.send("Confirmer")
        assert len(_needs_of(real_db, admin["phone"])) == 1


def test_a_message_that_does_not_change_the_draft_does_not_replay_the_confirmation(real_db):
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF)
        for _ in range(2):
            t = conv.send("à partir de demain", llm=UNKNOWN)
            assert t.error is None and t.response
        assert _confirmation_prompts(conv) == 1, _outbound_texts(conv)
        done = conv.send("Confirmer")
        assert "C'est noté" in done.response
        assert len(_needs_of(real_db, admin["phone"])) == 1


def test_the_reminder_keeps_the_original_confirmation_actionable(real_db):
    """Le rappel n'est PAS une seconde confirmation : le draft et le CONFIRM_ACTION en attente restent ceux du 1er envoi."""
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        t1 = conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF)
        t2 = conv.send("à partir de demain", llm=UNKNOWN)
        assert t2.pending_after.kind.value == "CONFIRM_ACTION"
        assert t2.draft()["draft_id"] == t1.draft()["draft_id"] and t2.draft()["version"] == t1.draft()["version"]
        assert not t2.interactive, "pas de nouveaux boutons : les premiers restent valides"
        assert "Confirmer" in t2.response
        conv.send("Confirmer")
        assert len(_needs_of(real_db, admin["phone"])) == 1


def test_a_real_change_of_the_draft_is_a_new_confirmation(real_db):
    """La déduplication porte sur (draft, version) : un draft MODIFIÉ doit être re-confirmé."""
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF)
        t2 = conv.send(
            "plutôt 3 têtes",
            llm={"disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.9,
                 "entities": {"product": "Boeufs", "quantity": 3.0, "unit": "TETE", "recurrence_type": "WEEKLY"}},
        )
        assert "3 TETE" in t2.response, t2.response
        assert _confirmation_prompts(conv) == 2


# ── création unique ────────────────────────────────────────────────────────────────────────────────────────────────
def test_confirmer_replayed_creates_one_need_and_answers_idempotently(real_db):
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF)
        first = conv.send("Confirmer")
        second = conv.send("Confirmer")
        assert "C'est noté" in first.response
        assert "noté" not in second.response.lower() or "déjà" in second.response.lower()
        assert second.error is None
        assert len(_needs_of(real_db, admin["phone"])) == 1
        assert conv.runtime.calls.count  # le graphe n'a pas planté
        creates = [c for c in conv.runtime.calls if c[0] in ("create_recurring_need", "create_recurring_needs")]
        assert len(creates) <= 1


def test_the_same_inbound_message_redelivered_is_skipped_by_the_task_guard(real_db):
    """Même identifiant de message (retry broker / webhook rejoué) : `process_agent_task` saute le tour, un seul envoi."""
    admin = _admin(real_db)
    with Conv(real_db, admin) as conv:
        first = conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF, message_id="wamid.FIXED")
        replay = conv.send("Boeufs 2 tete chaque semaine", llm=BOEUF, message_id="wamid.FIXED")
        assert first.dispatched and not replay.dispatched
        assert _confirmation_prompts(conv) == 1
