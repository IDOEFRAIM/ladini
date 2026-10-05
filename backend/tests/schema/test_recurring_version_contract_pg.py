"""B26 — contrat de version de bout en bout (intention périmée), contre un VRAI PostgreSQL.

Contrat : docs/RECURRING_MUTATION_CONSISTENCY_CONTRACT.md § « Version Contract ». Seuls LLM (scripté), Redis et dispatcher sont
doublés ; liste, détail, mutation, matérialisation passent par le vrai `AgriDatabaseService`. « Autre session » = un second
appel de service, qui écrit réellement en base entre l'écran affiché et la réponse de l'acheteur.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import pathlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import ladini.services.database.recurring_supply as rs
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    MCPCallError,
    RecurringSupplyGateway,
)
from ladini.services.database.errors import BusinessRuleException
from tests.harness import ConversationHarness, new_task
from tests.integration.test_semantic_context_arbitration_e2e import (
    UPDATE_MONTHLY,
    UPDATE_QTY_3,
    ScriptLLM,
)
from tests.schema import test_recurring_entrypoint_pg as entry
from tests.schema import test_semantic_context_arbitration_pg as base
from tests.schema.test_recurring_entrypoint_pg import Conv, real_db  # noqa: F401
from tests.schema.test_recurring_mutation_consistency_pg import (  # noqa: F401
    D,
    _call,
    _cleanup,
    _fixed_today,
    _run,
    _sql,
    _two_sessions,
    alloc,
    cron,
    need_row,
    occ,
    rows,
    upd,
    version,
    w,
)
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
PAUSE = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="PAUSE")
RESUME = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="RESUME")
SKIP = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="SKIP_OCCURRENCE")
OVERRIDE_10 = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="OVERRIDE_OCCURRENCE", quantity=10.0)


def _one_need(real_db, account):  # noqa: F811
    (need_id,) = [r[0] for r in _need_ids(real_db, account)]
    return need_id


def _external(conv, account, need_id, action, **kw):
    """Une AUTRE session modifie le besoin (lit la version courante, puis écrit : une mutation légitime et fraîche)."""
    svc = conv._svc

    async def go():
        listing = await svc.list_my_recurring_needs(phone=account["phone"])
        version = next(i for i in listing["items"] if i["recurring_need_id"] == need_id)["need_version"]
        return await svc.update_recurring_need(
            phone=account["phone"], recurring_need_id=need_id, action=action, expected_version=version, **kw)

    return conv.harness._run(go())


def _need_state(real_db, need_id):  # noqa: F811
    status, quantity, rtype, updated = entry._sql(
        real_db, "select status, quantity, recurrence_type, updated_at from marketplace.recurring_needs where id = %s", (need_id,))[0]
    return status, float(quantity), rtype, updated


# ── R1 / R2 : correction contextuelle sur un écran périmé ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_r1_stale_detail_then_quantity_correction_is_a_conflict_and_the_db_keeps_the_newer_quantity(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        _boeuf_detail(conv)  # affiche : 2 TETE / semaine, version V1
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)  # V2
        before = _need_state(real_db, need_id)
        assert before[1] == 5.0
        t = conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert _need_state(real_db, need_id) == before, "jamais quantité=3 sans nouvelle confirmation sur V2"
        assert "modifié entre-temps" in t.response and "Rien n'a été changé" in t.response, t.response
        assert "5 tete" in t.response.lower() and "Besoin :" in t.response, "l'état courant est montré (écran à jour)"
        assert "version=" not in t.response.lower() and "2026" not in t.response, "jamais de jeton technique"


def test_r2_stale_detail_then_frequency_correction_is_a_conflict(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        t = conv.send("plutôt chaque mois", llm=ScriptLLM(UPDATE_MONTHLY))
        assert _need_state(real_db, need_id)[2] == "WEEKLY", "pas de passage silencieux en MONTHLY sur V2"
        assert "modifié entre-temps" in t.response, t.response


# ── R3 / R4 : confirmations ───────────────────────────────────────────────────────────────────────────────────────
def test_r3_pause_confirmation_built_on_v1_conflicts_when_the_need_changes_before_confirm(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        t = conv.send("1", llm=UNKNOWN)
        assert _need_state(real_db, need_id)[0] == "ACTIVE", "la confirmation de V1 ne suspend pas V2"
        assert "modifié entre-temps" in t.response, t.response


def test_r3b_pause_asked_from_a_stale_screen_is_a_conflict_at_confirmation(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)  # V1 affiché
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)  # V2 avant la demande
        conv.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        t = conv.send("1", llm=UNKNOWN)
        assert _need_state(real_db, need_id)[0] == "ACTIVE", "la commande est bâtie sur l'écran V1 : jamais exécutée sur V2"
        assert "modifié entre-temps" in t.response, t.response


def test_r4_cancel_final_confirmation_conflicts_when_the_need_changes_between_the_two_confirmations(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        ask = conv.send("2", llm=UNKNOWN)  # « Arrêter complètement » : seconde confirmation (snapshot V1)
        assert "définitive" in ask.response
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        t = conv.send("1", llm=UNKNOWN)
        assert _need_state(real_db, need_id)[0] == "ACTIVE", "CANCEL ne s'applique pas à la nouvelle version"
        assert "modifié entre-temps" in t.response, t.response


def test_r4b_cancel_asked_from_a_stale_screen_never_cancels_the_latest_state(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        conv.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        conv.send("2", llm=UNKNOWN)
        t = conv.send("1", llm=UNKNOWN)
        assert _need_state(real_db, need_id)[0] == "ACTIVE", t.response
        assert "modifié entre-temps" in t.response, t.response


# ── R6 : le snapshot survit (restart-like : chaque tour recharge l'état depuis le checkpoint) ─────────────────────
def test_r6_the_confirmation_snapshot_carries_the_displayed_version(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        presented = conv.state()["working_memory"]["recurring_need_menu"]["target"].get("need_version")
        assert presented is not None, "l'écran de détail enregistre la version affichée"
        conv.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        command = conv.state()["working_memory"]["recurring_need_menu"]["commands"]["1"]
        assert command["expected_version"] == presented and command["action"] == "PAUSE"


# ── R7 : un appelant qui oublie la version ne peut PAS passer (service + passerelle + chemins user-facing) ──────────
ALL_ACTIONS = [
    ("PERMANENT_QUANTITY", {"quantity": 3}), ("PERMANENT_FREQUENCY", {"recurrence_type": "WEEKLY_DAYS", "weekly_days": [3]}),
    ("PAUSE", {}), ("RESUME", {}), ("CANCEL", {}),
    ("OCCURRENCE_SKIP", {"occurrence_date": D(3).date()}), ("OCCURRENCE_OVERRIDE", {"occurrence_date": D(3).date(), "quantity": 5}),
]


def world(w, need=None):
    """TOUT ce qu'une mutation pourrait toucher : besoin (jeton compris), occurrences (versions comprises), allocations,
    commandes, événements métier et outbox."""
    need = need or w.need
    return {
        "need": _sql(w, "select status, quantity, recurrence_type, weekly_days, paused_until, updated_at from "
                        "marketplace.recurring_needs where id = %s", (need,)),
        "occurrences": _sql(w, "select id, occurrence_date, status, requested_quantity, quantity_matched, version from "
                               "marketplace.recurring_need_occurrences where recurring_need_id = %s order by occurrence_date", (need,)),
        "allocations": _sql(w, "select a.id, a.status, a.quantity from marketplace.need_allocations a join "
                               "marketplace.recurring_need_occurrences o on o.id = a.occurrence_id where o.recurring_need_id = %s "
                               "order by a.id", (need,)),
        "orders": _sql(w, "select count(*) from marketplace.orders where buyer_id = %s", (str(w.g.buyer),)),
        "business_events": _sql(w, "select count(*) from analytics.business_events"),
        "outbox": _sql(w, "select count(*) from analytics.event_outbox"),
    }


@pytest.mark.parametrize("action,kw", ALL_ACTIONS)
def test_r7_a_user_facing_caller_without_the_version_is_refused_and_nothing_is_touched(w, action, kw):
    occ(w, 3)
    before = world(w)
    with pytest.raises(BusinessRuleException) as exc:
        upd(w, action, unversioned=True, **kw)
    assert getattr(exc.value, "reason", None) == "version_required"
    assert world(w) == before, "aucune lecture ne devient une écriture : rien n'est touché"


def test_occurrence_actions_do_not_accept_the_need_version_as_a_substitute(w):
    occ(w, 3)
    with pytest.raises(BusinessRuleException) as exc:
        upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date(), unversioned=True, expected_version=version(w))
    assert getattr(exc.value, "reason", None) == "version_required"
    with pytest.raises(BusinessRuleException) as exc:
        upd(w, "PAUSE", unversioned=True, expected_occurrence_version=1)
    assert getattr(exc.value, "reason", None) == "version_required"


@pytest.mark.parametrize("action,kw", ALL_ACTIONS)
def test_the_gateway_refuses_a_call_without_the_version_before_any_mcp_call(action, kw):
    calls = []

    class _Runtime:
        async def call_db(self, tool, **kwargs):
            calls.append(tool)
            return {"status": "success"}

    gw = RecurringSupplyGateway(_Runtime())
    with pytest.raises(MCPCallError):
        asyncio.run(gw.update_recurring_need(phone="+226", recurring_need_id="n", action=action, **kw))
    assert calls == []


def test_every_user_facing_call_site_passes_a_version_keyword():
    """Garde de régression : tout `update_recurring_need(...)` du graphe porte `expected_version` /
    `expected_occurrence_version`, ou déplie le snapshot de commande confirmée (`_execute_command`, dont la passerelle
    refuse l'absence de version)."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "ladini" / "graphs"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                f = call.func
                if not (isinstance(f, ast.Attribute) and f.attr == "update_recurring_need") or fn.name == "update_recurring_need":
                    continue
                names = {k.arg for k in call.keywords}
                if not names & {"expected_version", "expected_occurrence_version"} and not (None in names and fn.name == "_execute_command"):
                    offenders.append(f"{path.name}:{fn.name}:{call.lineno}")
    assert offenders == []


# ── matrice NEED : frais / périmé ────────────────────────────────────────────────────────────────────────────────
FRESH = [
    ("PERMANENT_QUANTITY", {"quantity": 3}, None),
    ("PERMANENT_FREQUENCY", {"recurrence_type": "WEEKLY_DAYS", "weekly_days": [3]}, None),
    ("PAUSE", {}, None),
    ("RESUME", {}, "PAUSE"),
    ("CANCEL", {}, None),
]


@pytest.mark.parametrize("action,kw,prelude", FRESH)
def test_a_fresh_mutation_is_applied_and_bumps_the_need_version_exactly_once(w, action, kw, prelude, monkeypatch):
    if prelude:
        upd(w, prelude)
    bumps = []
    original = rs.RecurringSupplyMixin._bump_need_version

    async def spy(self, need):
        bumps.append(need.id)
        return await original(self, need)

    monkeypatch.setattr(rs.RecurringSupplyMixin, "_bump_need_version", spy)
    occ(w, 3)
    v1 = version(w)
    res = upd(w, action, expected_version=v1, **kw)
    assert res["outcome"] == "APPLIED" and len(bumps) == 1, "V1 -> V2 une seule fois, malgré la réconciliation des occurrences"
    assert version(w) == res["need_version"] != v1


@pytest.mark.parametrize("action,kw,prelude", FRESH)
def test_a_stale_mutation_is_a_conflict_and_changes_absolutely_nothing(w, action, kw, prelude):
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    occ(w, 4)
    if prelude:
        upd(w, prelude)
    v1 = version(w)
    other = "PERMANENT_QUANTITY" if action != "PERMANENT_QUANTITY" else "PERMANENT_FREQUENCY"
    other_kw = {"quantity": 9} if other == "PERMANENT_QUANTITY" else {"recurrence_type": "WEEKLY_DAYS", "weekly_days": [1, 2]}
    if prelude == "PAUSE":
        other, other_kw = "PAUSE", {"paused_until": D(9).date()}  # une autre session déplace la date de reprise
    upd(w, other, **other_kw)  # une AUTRE session : V2
    before = world(w)
    res = upd(w, action, expected_version=v1, **kw)
    assert res["status"] == "conflict" and res["outcome"] == "VERSION_CONFLICT" and res["scope"] == "NEED"
    assert res["current"]["need_version"] == version(w) and res["expected_version"] == v1
    assert world(w) == before, "besoin, version, occurrences, allocations, commandes, événements, outbox : tous intacts"


def test_a_conflict_triggers_no_reconciliation_before_the_check(w, monkeypatch):
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    v1 = version(w)
    upd(w, "PERMANENT_QUANTITY", quantity=4)
    touched = []
    for name in ("_reset_uncommitted_occurrences", "_ensure_window_for_need", "_bump_need_version"):
        original = getattr(rs.RecurringSupplyMixin, name)

        def make(orig, label):
            async def spy(self, *a, **k):
                touched.append(label)
                return await orig(self, *a, **k)
            return spy

        monkeypatch.setattr(rs.RecurringSupplyMixin, name, make(original, name))
    assert upd(w, "PERMANENT_FREQUENCY", expected_version=v1, recurrence_type="WEEKLY_DAYS", weekly_days=[3])["outcome"] == "VERSION_CONFLICT"
    assert touched == [], "la comparaison de version précède tout effet de bord métier"


def test_a_conflict_logs_the_check_without_the_raw_token_or_any_personal_data(w, caplog):
    v1 = version(w)
    upd(w, "PERMANENT_QUANTITY", quantity=4)
    with caplog.at_level(logging.INFO, logger="ladini.services.database.recurring_supply"):
        upd(w, "PERMANENT_QUANTITY", expected_version=v1, quantity=5)
    lines = [r.getMessage() for r in caplog.records if "RECURRING_VERSION_CHECK" in r.getMessage()]
    assert len(lines) == 1 and "version_match=False" in lines[0] and "outcome=VERSION_CONFLICT" in lines[0]
    assert str(v1) not in lines[0] and "+226" not in lines[0]
    assert not [r for r in caplog.records if "recurring_need.mutation" in r.getMessage()], "aucun audit de mutation sur un conflit"


def test_an_idempotent_no_op_never_bumps_the_version(w):
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    v = version(w)
    res = upd(w, "PERMANENT_QUANTITY", expected_version=v, quantity=3)
    assert res["outcome"] == "ALREADY_APPLIED" and version(w) == v == res["need_version"]


def test_replaying_an_applied_command_is_stale_never_a_second_mutation(w):
    """Rejeu exact d'une commande déjà appliquée : sa version est périmée (le besoin est passé à V2) -> conflit net, jamais
    une seconde mutation. (Un rejeu bâti sur la version COURANTE est, lui, `ALREADY_APPLIED` : voir le test précédent.)"""
    v1 = version(w)
    assert upd(w, "CANCEL", expected_version=v1)["outcome"] == "APPLIED"
    v2 = version(w)
    replay = upd(w, "CANCEL", expected_version=v1)
    assert replay["outcome"] == "VERSION_CONFLICT" and version(w) == v2
    assert upd(w, "CANCEL", expected_version=v2)["outcome"] == "ALREADY_APPLIED" and version(w) == v2


def test_an_old_pause_confirmation_after_a_different_mutation_is_a_conflict_not_a_replay(w):
    v1 = version(w)
    upd(w, "PERMANENT_QUANTITY", quantity=5)
    assert upd(w, "PAUSE", expected_version=v1)["outcome"] == "VERSION_CONFLICT"
    assert need_row(w)[0] == "ACTIVE" and need_row(w)[1] == 5


def test_stale_intent_after_cancel_or_pause_is_a_version_conflict_never_a_reactivation(w):
    v1 = version(w)
    upd(w, "CANCEL")
    res = upd(w, "PERMANENT_QUANTITY", expected_version=v1, quantity=3)
    assert res["outcome"] == "VERSION_CONFLICT" and res["current"]["status"] == "CANCELLED"  # la priorité : version d'abord
    with pytest.raises(BusinessRuleException) as exc:  # à jour mais arrêté : l'état, pas la version, refuse
        upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert getattr(exc.value, "reason", None) == "need_not_active" and need_row(w)[0] == "CANCELLED"


def test_stale_intent_after_pause_is_a_version_conflict(w):
    v1 = version(w)
    upd(w, "PAUSE")
    assert upd(w, "PERMANENT_QUANTITY", expected_version=v1, quantity=3)["outcome"] == "VERSION_CONFLICT"
    assert need_row(w)[0] == "PAUSED" and need_row(w)[1] == 2


def test_two_independent_sessions_a_quantity_then_b_frequency_never_merge(w):
    snapshot_a = snapshot_b = version(w)  # deux écrans ouverts sur V1
    assert upd(w, "PERMANENT_QUANTITY", expected_version=snapshot_b, quantity=5)["outcome"] == "APPLIED"
    res = upd(w, "PERMANENT_FREQUENCY", expected_version=snapshot_a, recurrence_type="MONTHLY")
    assert res["outcome"] == "VERSION_CONFLICT"
    status, quantity, rtype, *_ = need_row(w)
    assert (float(quantity), rtype) == (5.0, "DAILY"), "aucune fusion automatique des champs"


# ── CRON / AUTO-RESUME / paused_until ────────────────────────────────────────────────────────────────────────────
def test_auto_resume_bumps_the_need_version_and_invalidates_the_old_snapshot(w, monkeypatch):
    upd(w, "PAUSE", paused_until=D(2).date())
    v1 = version(w)
    monkeypatch.setattr(rs, "_today", lambda: D(2).date())
    cron(w)
    assert need_row(w)[0] == "ACTIVE" and version(w) != v1
    for action, kw in (("RESUME", {}), ("PERMANENT_QUANTITY", {"quantity": 3}), ("PAUSE", {})):
        assert upd(w, action, expected_version=v1, **kw)["outcome"] == "VERSION_CONFLICT", action
    assert need_row(w)[0] == "ACTIVE"


def test_a_manual_resume_confirmed_after_the_auto_resume_does_no_second_mutation(w, monkeypatch):
    upd(w, "PAUSE", paused_until=D(2).date())
    v1 = version(w)  # confirmation « reprendre » bâtie sur V1
    monkeypatch.setattr(rs, "_today", lambda: D(2).date())
    cron(w)
    v2 = version(w)
    assert upd(w, "RESUME", expected_version=v1)["outcome"] == "VERSION_CONFLICT" and version(w) == v2
    assert upd(w, "RESUME", expected_version=v2)["outcome"] == "ALREADY_APPLIED" and version(w) == v2  # même état désiré


def test_changing_paused_until_bumps_the_version_and_replaying_the_same_date_does_not(w):
    upd(w, "PAUSE", paused_until=D(3).date())
    v1 = version(w)
    assert upd(w, "PAUSE", paused_until=D(6).date())["outcome"] == "APPLIED" and version(w) != v1
    v2 = version(w)
    assert upd(w, "PAUSE", paused_until=D(6).date())["outcome"] == "ALREADY_APPLIED" and version(w) == v2


def test_cancel_bumps_once_and_the_old_active_snapshot_is_invalid(w):
    v1 = version(w)
    assert upd(w, "CANCEL", expected_version=v1)["outcome"] == "APPLIED"
    assert version(w) != v1 and upd(w, "PERMANENT_FREQUENCY", expected_version=v1, recurrence_type="DAILY")["outcome"] == "VERSION_CONFLICT"


def test_cron_materialization_leaves_the_need_version_untouched(w):
    v1 = version(w)
    cron(w)
    assert rows(w) and version(w) == v1, "les occurrences ne font pas partie de la version du besoin"


# ── version d'OCCURRENCE : distincte de celle du besoin ──────────────────────────────────────────────────────────
def test_occurrence_mutations_use_the_occurrence_version_and_never_bump_the_need(w):
    occ(w, 3)
    occ(w, 4)
    need_v = version(w)
    res = upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date())
    assert res["outcome"] == "APPLIED" and version(w) == need_v, "un skip ne change pas le contrat durable"
    res = upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(4).date(), quantity=7)
    assert res["outcome"] == "APPLIED" and version(w) == need_v


def test_a_stale_skip_or_override_is_a_conflict_with_no_side_effect(w):
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    o_v1 = int(_sql(w, "select version from marketplace.recurring_need_occurrences where id = %s", (str(o),))[0][0])
    upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=6)  # une autre session : version d'occurrence + 1
    before = world(w)
    for action, kw in (("OCCURRENCE_SKIP", {}), ("OCCURRENCE_OVERRIDE", {"quantity": 9})):
        res = upd(w, action, occurrence_date=D(3).date(), expected_occurrence_version=o_v1, **kw)
        assert res["outcome"] == "VERSION_CONFLICT" and res["scope"] == "OCCURRENCE"
        assert res["current"]["occurrence_version"] == o_v1 + 1 and res["current"]["requested_quantity"] == 6.0
    assert world(w) == before


def test_a_replayed_skip_is_stale_and_a_fresh_replay_is_already_applied_with_one_event(w):
    occ(w, 3)
    o_v1 = _sql(w, "select version from marketplace.recurring_need_occurrences where recurring_need_id = %s", (w.need,))[0][0]
    events_before = _sql(w, "select count(*) from analytics.event_outbox")[0][0]
    assert upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date(), expected_occurrence_version=o_v1)["outcome"] == "APPLIED"
    assert upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date(), expected_occurrence_version=o_v1)["outcome"] == "VERSION_CONFLICT"
    assert upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date())["outcome"] == "ALREADY_APPLIED"
    assert _sql(w, "select count(*) from analytics.event_outbox")[0][0] == events_before + 1


def test_a_need_quantity_change_that_rewrites_the_occurrence_invalidates_its_old_override_snapshot(w):
    o = occ(w, 3)
    o_v1 = _sql(w, "select version from marketplace.recurring_need_occurrences where id = %s", (str(o),))[0][0]
    upd(w, "PERMANENT_QUANTITY", quantity=4)  # l'occurrence suivait le contrat : réécrite (version + 1)
    assert upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=9, expected_occurrence_version=o_v1)["outcome"] == "VERSION_CONFLICT"


def _respond(w, action, occurrence, expected):
    return _run(w, lambda svc, s: svc.accept_match_proposal(
        phone="+226", recurring_need_id=w.need, action=action, occurrence_id=str(occurrence), expected_version=expected))


def test_reject_and_accept_keep_their_occurrence_version_contract(w):
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    o_v = int(_sql(w, "select version from marketplace.recurring_need_occurrences where id = %s", (str(o),))[0][0])
    before = world(w)
    for action in ("ACCEPT", "REJECT"):  # proposition périmée (digest/écran d'une autre version)
        res = _respond(w, action, o, o_v + 1)
        assert res["outcome"] == "PROPOSAL_CHANGED", (action, res)
    assert world(w) == before
    assert _respond(w, "REJECT", o, o_v).get("outcome") is None
    assert rows(w)[3][0] == "REJECTED"


# ── cross-buyer : la propriété d'abord, la version ensuite ───────────────────────────────────────────────────────
def test_cross_buyer_with_the_right_or_a_wrong_version_learns_nothing_on_need_and_occurrence(w):
    occ(w, 3)
    before = world(w)
    intruder = SimpleNamespace(id=uuid.uuid4())

    async def attempt(svc, s, **kw):
        svc._user_profile = intruder
        try:
            return await svc.update_recurring_need(phone="+226", recurring_need_id=w.need, **kw)
        except BusinessRuleException as exc:
            return str(exc)

    right, wrong = version(w), 12345
    answers = {
        _run(w, lambda svc, s, e=e: attempt(svc, s, action="PAUSE", expected_version=e)) for e in (right, wrong)
    } | {
        _run(w, lambda svc, s, e=e: attempt(svc, s, action="OCCURRENCE_SKIP", occurrence_date=D(3).date(),
                                            expected_occurrence_version=e)) for e in (1, 99)
    }
    assert answers == {"Besoin introuvable."}, answers
    assert world(w) == before


# ── sérialisation du jeton ───────────────────────────────────────────────────────────────────────────────────────
def test_the_version_survives_postgres_python_json_python_postgres_without_loss(w):
    listing = _run(w, lambda svc, s: svc.list_my_recurring_needs("+226"))
    wire = json.loads(json.dumps(listing))  # MCP : JSON
    token = wire["items"][0]["need_version"]
    assert isinstance(token, int) and token == version(w) and token < 2 ** 53, "entier exact, sûr pour un client JSON"
    assert upd(w, "PERMANENT_QUANTITY", expected_version=token, quantity=3)["outcome"] == "APPLIED"
    detail = json.loads(json.dumps(_run(w, lambda svc, s: svc.get_recurring_need_detail("+226", w.need))))
    assert upd(w, "PERMANENT_QUANTITY", expected_version=detail["need_version"], quantity=4)["outcome"] == "APPLIED"


def test_the_token_keeps_the_microseconds_and_ignores_the_timezone_representation():
    naive = datetime(2026, 10, 5, 12, 34, 56, 123457)
    aware = naive.replace(tzinfo=timezone.utc)
    other_tz = naive.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=2)))
    assert rs.recurring_need_version_of(naive) == rs.recurring_need_version_of(aware) == rs.recurring_need_version_of(other_tz)
    assert rs.recurring_need_version_of(naive) - rs.recurring_need_version_of(naive - timedelta(microseconds=1)) == 1
    assert rs.recurring_need_version_of(naive) % 1_000_000 == 123457, "ni millisecondes tronquées, ni arrondi"


def test_the_database_stores_the_updated_at_with_microsecond_precision(w):
    _sql(w, "update marketplace.recurring_needs set updated_at = %s where id = %s", (datetime(2026, 10, 5, 12, 34, 56, 123457), w.need))
    assert version(w) % 1_000_000 == 123457
    assert upd(w, "PERMANENT_QUANTITY", expected_version=version(w), quantity=3)["outcome"] == "APPLIED"


def test_rapid_successive_mutations_get_distinct_increasing_versions(w):
    seen = [version(w)]
    for q in range(3, 43):
        assert upd(w, "PERMANENT_QUANTITY", expected_version=seen[-1], quantity=q)["outcome"] == "APPLIED"
        seen.append(version(w))
    assert len(set(seen)) == len(seen) and seen == sorted(seen), "aucune collision de jeton, même en rafale"


# ── effets de bord d'un conflit : outbox ─────────────────────────────────────────────────────────────────────────
def test_a_conflicting_skip_emits_no_event_and_no_outbox_row(w):
    occ(w, 3)
    stale = _sql(w, "select version from marketplace.recurring_need_occurrences where recurring_need_id = %s", (w.need,))[0][0]
    upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=5)
    before = world(w)
    assert upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date(), expected_occurrence_version=stale)["outcome"] == "VERSION_CONFLICT"
    assert world(w) == before


# ── flux : réponse tardive, navigation, enchaînement, démarrage à froid, digest ─────────────────────────────────────
def _age_the_menu(conv, seconds):
    """Réponse tardive : l'écran et son attente datent de `seconds` secondes (le TTL du menu est de 10 minutes)."""
    state = conv.state()
    pending = dict(state.get("pending_interaction") or {})
    patch = {"working_memory": {"recurring_need_menu": {"created_at": time.time() - seconds}}}
    if pending:
        pending["created_at"] = time.time() - seconds
        patch["pending_interaction"] = pending
    conv.harness.seed(patch)


def test_a_delayed_reply_is_compared_to_the_screen_it_answers_even_after_the_menu_ttl(real_db):  # noqa: F811
    """10:00 détail V1 · 10:30 le besoin devient V2 · 11:00 « mets-en 3 » : jamais quantité=3 sur V2, quel que soit le sort du
    menu. Le TTL (validité conversationnelle) et la version (validité métier) sont deux protections indépendantes."""
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        before = _need_state(real_db, need_id)
        _age_the_menu(conv, 3 * 3600)
        t = conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert _need_state(real_db, need_id) == before, t.response
        assert not [1 for name, kw in t.mcp_calls if name == "update_recurring_need" and kw.get("expected_version") is None]


def test_menu_ttl_and_business_version_are_independent_an_aged_menu_with_an_unchanged_need_is_not_a_conflict(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        _age_the_menu(conv, 3 * 3600)
        t = conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert "modifié entre-temps" not in t.response, t.response  # la version affichée est toujours la bonne


def test_after_a_conflict_the_screen_is_redisplayed_and_the_user_decides_again(real_db):  # noqa: F811
    account = _account(real_db, "BUYER")
    with Conv(real_db, account, profile_role="BUYER") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        conflict = conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert "Besoin : 5" in conflict.response and "Rien n'a été changé" in conflict.response
        assert _need_state(real_db, need_id)[1] == 5.0, "jamais de rejeu automatique sur la nouvelle version"
        done = conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))  # il redécide, sur l'écran à jour
        assert _need_state(real_db, need_id)[1] == 3.0 and "modifié entre-temps" not in done.response, done.response


def test_a_successful_mutation_refreshes_the_presented_version_for_the_next_request(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert _need_state(real_db, need_id)[1] == 3.0
        conv.send("plutôt chaque mois", llm=ScriptLLM(UPDATE_MONTHLY))  # 2e demande légitime : pas de faux conflit
        assert _need_state(real_db, need_id)[2] == "MONTHLY"
        target = conv.state()["working_memory"]["recurring_need_menu"]["target"]
        assert target["need_version"] == rs.recurring_need_version_of(_need_state(real_db, need_id)[3])


def test_list_then_select_may_refresh_but_the_mutation_compares_to_the_displayed_detail(real_db):  # noqa: F811
    """Frontière documentée : la navigation (liste -> détail) rafraîchit ; la mutation compare à l'état AFFICHÉ."""
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("mes besoins", llm=UNKNOWN)  # liste affichée à V1
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)  # V2 avant la sélection
        detail = conv.send("1", llm=UNKNOWN)
        assert "Besoin : 5" in detail.response, "le détail charge l'état courant (navigation)"
        conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert _need_state(real_db, need_id)[1] == 3.0, "écran V2 affiché puis demande : appliquée"


def test_a_stale_list_is_also_a_conflict_for_a_correction_by_name(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("mes besoins", llm=UNKNOWN)  # liste V1 (la version de chaque besoin est mémorisée)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        t = conv.send("mets les boeufs à 3", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.93, product="boeufs", quantity=3.0)))
        assert _need_state(real_db, need_id)[1] == 5.0 and "modifié entre-temps" in t.response, t.response


def test_a_cold_request_without_any_presented_screen_carries_the_version_read_this_turn(real_db):  # noqa: F811
    """Sans écran présenté, il n'y a pas d'intention périmée : la version lue CE tour est transmise (jamais omise) et la
    mutation reste protégée contre une écriture concurrente par le compare-and-swap du service."""
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        conv.send("boeuf 2 tete chaque semaine", llm=base.BOEUF_LLM)
        conv.send("Confirmer")
        need_id = _one_need(real_db, account)
        t = conv.send("mets les boeufs à 3", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.93, product="boeufs", quantity=3.0)))
        calls = [kw for name, kw in t.mcp_calls if name == "update_recurring_need"]
        assert len(calls) == 1 and isinstance(calls[0].get("expected_version"), int), calls
        assert _need_state(real_db, need_id)[1] == 3.0


def test_admin_with_the_buyer_capability_gets_the_same_conflict_on_pause(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        _external(conv, account, need_id, "PERMANENT_QUANTITY", quantity=5)
        conv.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        t = conv.send("1", llm=UNKNOWN)
        assert _need_state(real_db, need_id)[0] == "ACTIVE" and "modifié entre-temps" in t.response


def test_a_stale_skip_confirmation_is_a_conflict_on_the_occurrence_version(real_db):  # noqa: F811
    account = _account(real_db, "BUYER")
    with Conv(real_db, account, profile_role="BUYER") as conv:
        _boeuf_detail(conv)
        need_id = _one_need(real_db, account)
        conv.send("pas cette semaine", llm=ScriptLLM(SKIP))
        command = conv.state()["working_memory"]["recurring_need_menu"]["commands"]["1"]
        assert command["action"] == "OCCURRENCE_SKIP" and command["expected_occurrence_version"] is not None
        assert "expected_version" not in command, "un ordre de livraison ne porte pas la version du besoin"
        entry._sql(real_db, "update marketplace.recurring_need_occurrences set version = version + 1 where recurring_need_id = %s",
                   (need_id,))
        before = entry._sql(real_db, "select status from marketplace.recurring_need_occurrences where recurring_need_id = %s "
                                     "order by occurrence_date", (need_id,))
        t = conv.send("1", llm=UNKNOWN)
        assert "modifiée entre-temps" in t.response, t.response
        assert entry._sql(real_db, "select status from marketplace.recurring_need_occurrences where recurring_need_id = %s "
                                   "order by occurrence_date", (need_id,)) == before


# ── digest : l'occurrence vue dans le message, jamais relue au moment d'écrire ──────────────────────────────────────
_DIGEST_ITEM = {
    "recurring_need_id": "need-oignon", "product": "oignon", "quantity": 75.0, "unit": "KG", "recurrence_type": "DAILY",
    "status": "ACTIVE", "need_version": 7, "next_occurrence_id": "occ-oignon", "next_occurrence_date": "2026-09-27",
    "next_occurrence_notified": True, "requested_quantity": 75.0, "matched_quantity": 0.0,
    "in_latest_digest": True, "digest_occurrence_version": 5, "next_occurrence_version": 6,  # rematch depuis le digest : 5 -> 6
}


def _digest_conv(response):
    conv = ConversationHarness(role="BUYER", channel="whatsapp")
    h = conv.__enter__()
    h.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": [_DIGEST_ITEM]}
    h.runtime.responses["update_recurring_need"] = response
    return conv, h


def test_digest_skip_carries_the_occurrence_version_of_the_digest_not_the_one_read_now():
    conv, h = _digest_conv({"status": "success", "outcome": "APPLIED", "need_version": 7})
    try:
        t = h.send("pas demain pour l'oignon", llm=UNKNOWN)
        calls = [kw for name, kw in t.mcp_calls if name == "update_recurring_need"]
        assert len(calls) == 1 and calls[0]["expected_occurrence_version"] == 5 and "expected_version" not in calls[0], calls
    finally:
        conv.__exit__(None, None, None)


def test_digest_override_carries_the_digest_occurrence_version_and_a_conflict_is_reported_not_applied():
    conflict = {"status": "conflict", "outcome": "VERSION_CONFLICT", "scope": "OCCURRENCE", "expected_version": 5, "current": {}}
    conv, h = _digest_conv(conflict)
    try:
        h.send("modifier", llm=UNKNOWN)
        t = h.send("40 kg", llm=UNKNOWN)
        calls = [kw for name, kw in t.mcp_calls if name == "update_recurring_need"]
        assert len(calls) == 1 and calls[0]["action"] == "OCCURRENCE_OVERRIDE" and calls[0]["expected_occurrence_version"] == 5
        assert "modifiée depuis ce message" in t.response and "C'est noté" not in t.response, t.response
    finally:
        conv.__exit__(None, None, None)


def test_a_digest_without_any_known_occurrence_version_mutates_nothing():
    item = {k: v for k, v in _DIGEST_ITEM.items() if k not in ("digest_occurrence_version", "next_occurrence_version")}
    conv = ConversationHarness(role="BUYER", channel="whatsapp")
    h = conv.__enter__()
    try:
        h.runtime.responses["list_my_recurring_needs"] = {"status": "success", "items": [item]}
        t = h.send("pas demain pour l'oignon", llm=UNKNOWN)
        assert not [1 for name, _ in t.mcp_calls if name == "update_recurring_need"], "la passerelle refuse : aucun appel émis"
    finally:
        conv.__exit__(None, None, None)
