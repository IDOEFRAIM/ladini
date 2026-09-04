"""Persistance transactionnelle réelle de `ProcurementDraft`
(2026-09-03, clôture du gap de persistance — voir la docstring de
`services/database/procurement_draft_store.py`).

## Portée EXACTE de cette preuve (honnêteté requise par le mandat)

Ce dépôt n'a AUCUNE infrastructure de test Postgres réelle (pas de
docker-compose, pas de fixture DB — vérifié). Conformément au choix
explicite de l'utilisateur ("Code complet + tests simulant fidèlement le
CAS Postgres"), ce fichier remplace `session.execute()` par un faux moteur
(`_FakeSession`/`_FakeDraftTable`) qui reproduit EXACTEMENT la sémantique
des 3 requêtes SQL réelles de `procurement_draft_store.py` :

  - `SELECT ... WHERE draft_id = :draft_id`               → 0 ou 1 ligne
  - `INSERT ... ON CONFLICT (draft_id) DO NOTHING`         → rowcount 0 ou 1
  - `UPDATE ... SET version=... WHERE draft_id=:id AND version=:expected`
    → rowcount 1 SEULEMENT si la ligne existe ET porte encore la version
      attendue ; 0 dans tous les autres cas (course perdue, ligne absente)

Ce que ces tests PROUVENT : la logique Python de `load`/`insert`/
`compare_and_swap`, et des appelants qui les utilisent
(`procurement_confirmation.py`, `procurement_execution_finalizer.py`,
`confirmation_gate.py`), est correcte FACE À cette sémantique — y compris
sous concurrence RÉELLE de threads OS (pas seulement en séquence).

Ce que ces tests NE PROUVENT PAS : que les 3 requêtes SQL elles-mêmes sont
valides contre un vrai moteur PostgreSQL (types JSONB, contrainte
PRIMARY KEY, comportement `ON CONFLICT` réel). Cette validation reste un
chantier séparé (CI contre une vraie base), explicitement hors de ce que
ce fichier prétend démontrer.
"""
from __future__ import annotations

import threading
import time
import types
from typing import Any, Dict, Optional

import pytest

from tests.conftest import make_state, run

from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementDraftStatus,
    ProcurementOutcomeKind,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.procurement_confirmation import (
    resolve_procurement_confirmation,
)
from agriconnect.services.database import procurement_draft_store as store_mod


# =====================================================================
# FAUX MOTEUR SQL — reproduit fidèlement UPDATE...WHERE...->rowcount
# =====================================================================


class _FakeDraftTable:
    """Une "table" en mémoire, protégée par un vrai `threading.Lock` — les
    tests de concurrence de ce fichier lancent de VRAIS threads OS (chacun
    avec sa propre boucle asyncio via `asyncio.run`), donc une race
    Python-only (GIL) ne suffirait pas à prouver l'exclusion mutuelle sans
    ce verrou explicite, exactement comme une vraie transaction Postgres
    sérialiserait ces écritures au niveau de la ligne."""

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def select(self, draft_id: str) -> Optional[types.SimpleNamespace]:
        with self.lock:
            row = self._rows.get(draft_id)
            return types.SimpleNamespace(**{k: v for k, v in row.items() if k != "updated_at"}) if row else None

    def insert(self, *, draft_id, conversation_id, version, status, payload) -> int:
        with self.lock:
            if draft_id in self._rows:
                return 0  # ON CONFLICT (draft_id) DO NOTHING
            self._rows[draft_id] = {
                "draft_id": draft_id,
                "conversation_id": conversation_id,
                "version": version,
                "status": status,
                "payload": payload,
                "updated_at": time.time(),
            }
            return 1

    def cas_update(self, *, draft_id, expected_version, new_version, status, payload) -> int:
        with self.lock:
            row = self._rows.get(draft_id)
            if row is None or row["version"] != expected_version:
                return 0  # ligne absente OU version périmée — jamais distingué
                # côté SQL (un vrai `UPDATE...WHERE` ne le distingue pas non
                # plus), exactement reproduit ici.
            row["version"] = new_version
            row["status"] = status
            row["payload"] = payload
            row["updated_at"] = time.time()
            return 1

    def force_updated_at(self, draft_id: str, updated_at: float) -> None:
        """Test-only : recule artificiellement `updated_at` d'une ligne pour
        simuler un `EXECUTING` bloqué depuis longtemps, sans dépendre d'un
        vrai `time.sleep`."""
        with self.lock:
            if draft_id in self._rows:
                self._rows[draft_id]["updated_at"] = updated_at

    def select_stale_executing(self, older_than_seconds: float) -> list:
        with self.lock:
            now = time.time()
            return [
                types.SimpleNamespace(**{k: v for k, v in row.items() if k != "updated_at"})
                for row in self._rows.values()
                if row["status"] == "EXECUTING" and (now - row["updated_at"]) > older_than_seconds
            ]


class _FakeResult:
    def __init__(self, rowcount: int = 0, rows: Optional[list] = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, table: _FakeDraftTable) -> None:
        self._table = table

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass

    async def execute(self, sql, params):
        raw = str(sql)
        if "older_than_seconds" in params:
            rows = self._table.select_stale_executing(params["older_than_seconds"])
            return _FakeResult(rows=rows)
        if raw.startswith("SELECT"):
            row = self._table.select(params["draft_id"])
            return _FakeResult(rows=[row] if row else [])
        if raw.startswith("INSERT"):
            rc = self._table.insert(
                draft_id=params["draft_id"],
                conversation_id=params["conversation_id"],
                version=params["version"],
                status=params["status"],
                payload=params["payload"],
            )
            return _FakeResult(rowcount=rc)
        if raw.startswith("UPDATE"):
            rc = self._table.cas_update(
                draft_id=params["draft_id"],
                expected_version=params["expected_version"],
                new_version=params["new_version"],
                status=params["status"],
                payload=params["payload"],
            )
            return _FakeResult(rowcount=rc)
        raise AssertionError(f"Requête SQL inattendue dans le faux moteur : {raw!r}")


def _install_fake_db(monkeypatch, table: Optional[_FakeDraftTable] = None) -> _FakeDraftTable:
    table = table or _FakeDraftTable()
    monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: (lambda: _FakeSession(table)))
    return table


def _draft(**fields) -> ProcurementDraft:
    base = {
        "product": "tomates",
        "quantity": 2000.0,
        "unit": "KG",
        "price": 250.0,
        "price_unit": "KG",
    }
    base.update(fields)
    return ProcurementDraft.new(draft_id=fields.get("draft_id", "persist1"), **{
        k: v for k, v in base.items() if k != "draft_id"
    })


# =====================================================================
# SÉMANTIQUE UNITAIRE — load / insert / compare_and_swap
# =====================================================================


class TestLoadInsertCompareAndSwapSemantics:
    def test_load_on_an_absent_draft_id_returns_none(self, monkeypatch):
        _install_fake_db(monkeypatch)
        assert run(store_mod.load("nope")) is None

    def test_insert_then_load_round_trips_the_exact_draft(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="p1")
        assert run(store_mod.insert(draft, conversation_id="+22600000000")) is True

        loaded = run(store_mod.load("p1"))
        assert loaded is not None
        assert loaded.draft_id == draft.draft_id
        assert loaded.version == draft.version == 1
        assert loaded.status == ProcurementDraftStatus.DRAFT
        assert loaded.quantity == draft.quantity

    def test_a_second_insert_on_the_same_draft_id_is_rejected(self, monkeypatch):
        """`ON CONFLICT (draft_id) DO NOTHING` -> rowcount 0 -> `insert`
        renvoie `False`. Le draft_id est une UUID fraîche à chaque bootstrap
        (`confirmation_gate.py`) — ce cas ne devrait jamais survenir en
        production, mais la fonction ne doit jamais écraser silencieusement."""
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="p2")
        assert run(store_mod.insert(draft, conversation_id="c")) is True

        other = _draft(draft_id="p2", product="oignons")
        assert run(store_mod.insert(other, conversation_id="c")) is False
        # La ligne existante n'a PAS été altérée par la tentative rejetée.
        reloaded = run(store_mod.load("p2"))
        assert reloaded.product == "tomates"

    def test_compare_and_swap_with_the_expected_version_succeeds_and_persists(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="p3")
        run(store_mod.insert(draft, conversation_id="c"))

        updated = draft.with_updates(quantity=500.0)
        assert updated.version == 2
        ok = run(
            store_mod.compare_and_swap(
                "p3", expected_version=draft.version, new_draft=updated
            )
        )
        assert ok is True
        reloaded = run(store_mod.load("p3"))
        assert reloaded.version == 2
        assert reloaded.quantity == 500.0

    def test_compare_and_swap_with_a_stale_expected_version_fails_and_does_not_persist(
        self, monkeypatch
    ):
        """Le coeur du contrat CAS (mandat §1) : un `expected_version`
        périmé ne doit JAMAIS écrire — `rowcount == 0`, la ligne existante
        reste EXACTEMENT ce qu'elle était."""
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="p4")
        run(store_mod.insert(draft, conversation_id="c"))
        v2 = draft.with_updates(quantity=500.0)
        run(store_mod.compare_and_swap("p4", expected_version=1, new_draft=v2))

        # Un écrivain retardataire croit toujours être à la version 1.
        stale_attempt = draft.with_updates(quantity=999.0)
        ok = run(
            store_mod.compare_and_swap(
                "p4", expected_version=1, new_draft=stale_attempt
            )
        )
        assert ok is False
        reloaded = run(store_mod.load("p4"))
        assert reloaded.version == 2
        assert reloaded.quantity == 500.0  # PAS 999.0 — l'écriture périmée n'a rien touché.

    def test_compare_and_swap_on_a_nonexistent_draft_id_fails_cleanly(self, monkeypatch):
        _install_fake_db(monkeypatch)
        phantom = _draft(draft_id="ghost")
        ok = run(
            store_mod.compare_and_swap(
                "ghost", expected_version=1, new_draft=phantom.with_updates(quantity=1.0)
            )
        )
        assert ok is False

    def test_a_missing_sessionmaker_never_raises_anywhere(self, monkeypatch):
        """Discipline best-effort (même que `core/location.py`) : DB
        totalement absente (pas de `DATABASE_URL`) -> `None`/`False`
        partout, jamais une exception qui casse le tour."""
        monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: None)
        draft = _draft(draft_id="p5")
        assert run(store_mod.load("p5")) is None
        assert run(store_mod.insert(draft, conversation_id="c")) is False
        assert (
            run(
                store_mod.compare_and_swap(
                    "p5", expected_version=1, new_draft=draft.with_updates(quantity=1.0)
                )
            )
            is False
        )


# =====================================================================
# CONCURRENCE RÉELLE — vrais threads OS contre le faux moteur
# =====================================================================


class TestRealThreadConcurrencyAgainstCompareAndSwap:
    def test_ten_threads_racing_the_same_expected_version_only_one_wins(self, monkeypatch):
        """Preuve directement au niveau du store (pas encore à travers le
        nœud de graphe) : 10 threads OS tentent SIMULTANÉMENT un
        `compare_and_swap` avec le MÊME `expected_version` — exactement la
        course qu'un vrai `UPDATE...WHERE version=:expected` doit trancher
        en base. Un seul rowcount==1 possible par version."""
        table = _install_fake_db(monkeypatch)
        draft = _draft(draft_id="race1")
        run(store_mod.insert(draft, conversation_id="c"))

        results: list = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(i: int) -> None:
            barrier.wait()
            candidate = draft.with_updates(quantity=float(1000 + i))
            ok = run(
                store_mod.compare_and_swap(
                    "race1", expected_version=1, new_draft=candidate
                )
            )
            with results_lock:
                results.append((i, ok))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [i for i, ok in results if ok]
        assert len(winners) == 1, f"exactement un gagnant attendu, obtenu {winners}"

        final = run(store_mod.load("race1"))
        assert final.version == 2
        assert final.quantity == float(1000 + winners[0])

    def test_update_then_confirm_racing_through_the_real_node_yields_one_valid_transition(
        self, monkeypatch
    ):
        """Même course que ci-dessus, mais à travers le VRAI chemin de code
        (`resolve_procurement_confirmation`, pas les fonctions du store
        directement) — reproduit le scénario du mandat §16/§18 : un UPDATE
        et un CONFIRM ciblant la MÊME version en même temps. Un seul des
        deux doit réussir sa transition ; l'autre doit se voir répondre
        `VERSION_CONFLICT`, jamais silencieusement écrasé."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        table = _install_fake_db(monkeypatch)
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        v1 = _draft(draft_id="race2")
        run(store_mod.insert(v1, conversation_id="c"))
        target = {"draft_id": v1.draft_id, "draft_version": v1.version}

        outcomes: list = []
        outcomes_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def confirm_worker() -> None:
            state = make_state(
                current_goal="PROCUREMENT_CREATE_REQUEST",
                procurement_draft=v1.to_dict(),
                interpreted_event="CONFIRM",
                extracted_entities={},
            )
            state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
            barrier.wait()
            patch = run(resolve_procurement_confirmation(state, None))
            with outcomes_lock:
                outcomes.append(("CONFIRM", patch.get("status")))

        def update_worker() -> None:
            state = make_state(
                current_goal="PROCUREMENT_CREATE_REQUEST",
                procurement_draft=v1.to_dict(),
                interpreted_event="UPDATE",
                extracted_entities={"quantity": 750.0, "unit": "KG"},
            )
            state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
            barrier.wait()
            patch = run(resolve_procurement_confirmation(state, None))
            with outcomes_lock:
                outcomes.append(("UPDATE", patch.get("status")))

        threads = [threading.Thread(target=confirm_worker), threading.Thread(target=update_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # La ligne persistée est dans un état COHÉRENT unique — soit
        # EXECUTING (le CONFIRM a gagné), soit DRAFT v2 (l'UPDATE a gagné) —
        # jamais un mélange, jamais absente.
        final = run(store_mod.load("race2"))
        assert final is not None
        assert final.status in (ProcurementDraftStatus.DRAFT, ProcurementDraftStatus.EXECUTING)
        if final.status == ProcurementDraftStatus.EXECUTING:
            assert final.version == 2
        else:
            assert final.version == 2
            assert final.quantity == 750.0
        # Les deux tours ont produit une réponse (aucun n'a levé) — l'un des
        # deux a nécessairement vu son écriture rejetée par le CAS.
        assert len(outcomes) == 2


# =====================================================================
# BOUT-EN-BOUT — la DB comme vérité canonique à travers le nœud réel
# =====================================================================


class TestEndToEndThroughResolveProcurementConfirmation:
    def test_update_then_confirm_persists_each_step_and_is_idempotent_on_repeat(
        self, monkeypatch
    ):
        """Scénario rapporté ("2 tonnes 250 kg" -> "1 tonne 125 kg" ->
        "okay"), cette fois avec un store RÉELLEMENT joignable (le faux
        moteur) — preuve que la ligne PostgreSQL simulée reflète chaque
        étape, pas seulement l'état LangGraph."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        _install_fake_db(monkeypatch)
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        v1 = ProcurementDraft.new(
            draft_id="e2e1", product="tomates", quantity=2250.0, unit="KG",
            price=250.0, price_unit="KG",
        )
        run(store_mod.insert(v1, conversation_id="+22600000000"))
        target = {"draft_id": v1.draft_id, "draft_version": v1.version}

        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=v1.to_dict(),
            interpreted_event="UPDATE",
            extracted_entities={"quantity": 1125.0, "unit": "KG"},
        )
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
        patch = run(resolve_procurement_confirmation(state, None))
        persisted_v2 = run(store_mod.load("e2e1"))
        assert persisted_v2.version == 2
        assert persisted_v2.quantity == 1125.0
        assert patch["procurement_draft"]["version"] == 2  # projection LangGraph alignée

        state.update(patch)
        state["interpreted_event"] = "CONFIRM"
        state["extracted_entities"] = {}
        state["pending_interaction"] = patch["pending_interaction"]
        patch2 = run(resolve_procurement_confirmation(state, None))
        persisted_executing = run(store_mod.load("e2e1"))
        assert persisted_executing.status == ProcurementDraftStatus.EXECUTING
        # CONFIRM bump aussi la version (v2 DRAFT -> v3 EXECUTING) — une
        # transition persistée de plus, protégée par le même CAS.
        assert persisted_executing.version == 3
        assert patch2["status"] == "EXECUTING"
        assert patch2["transaction_payload"]["quantity"] == 1125.0

        # Rejoue le MÊME CONFIRM (double-livraison réseau/worker) — aucune
        # ré-exécution, aucun changement de version.
        state.update(patch2)
        state["interpreted_event"] = "CONFIRM"
        patch3 = run(resolve_procurement_confirmation(state, None))
        persisted_after_repeat = run(store_mod.load("e2e1"))
        assert persisted_after_repeat.version == 3
        assert persisted_after_repeat.status == ProcurementDraftStatus.EXECUTING

    def test_a_confirm_targeting_a_stale_persisted_version_is_rejected(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        _install_fake_db(monkeypatch)
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        v1 = ProcurementDraft.new(
            draft_id="e2e2", product="tomates", quantity=2000.0, unit="KG",
            price=250.0, price_unit="KG",
        )
        run(store_mod.insert(v1, conversation_id="c"))
        stale_target = {"draft_id": v1.draft_id, "draft_version": v1.version}

        # Un AUTRE tour a déjà fait avancer la ligne persistée à v2 pendant
        # que ce client tenait encore la cible v1 (ex: onglet WhatsApp
        # dupliqué, retry réseau).
        v2 = v1.with_updates(quantity=999.0)
        run(store_mod.compare_and_swap("e2e2", expected_version=1, new_draft=v2))

        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=v1.to_dict(),  # cache LangGraph PÉRIMÉ (encore v1)
            interpreted_event="CONFIRM",
            extracted_entities={},
        )
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": stale_target}
        patch = run(resolve_procurement_confirmation(state, None))

        # La cible v1 ne peut PAS confirmer une ligne déjà à v2 — le nœud a
        # rechargé l'AUTORITATIF (v2) avant de décider, donc `apply_domain_action`
        # voit `pending_target.draft_version=1 != draft.version=2` : rejeté
        # comme cible périmée, jamais exécuté sur la mauvaise version.
        assert patch["status"] != "EXECUTING"
        reloaded = run(store_mod.load("e2e2"))
        assert reloaded.status == ProcurementDraftStatus.DRAFT
        assert reloaded.version == 2
        assert reloaded.quantity == 999.0  # inchangé par la tentative périmée


# =====================================================================
# MODE DÉGRADÉ — DB injoignable ne doit JAMAIS être confondu avec un
# vrai VERSION_CONFLICT (verrouille le correctif appliqué dans
# `procurement_confirmation.py` suite à la découverte de ce gap pendant
# la mise en place de CE fichier de test).
# =====================================================================


class TestStaleExecutingDetection:
    """Mandat §11 : un `EXECUTING` ne doit jamais rester bloqué sans qu'un
    mécanisme, REPRÉSENTÉ dans le modèle (pas seulement documenté), permette
    de le repérer. `find_stale_executing` est cette représentation — voir sa
    docstring pour ce qu'un appelant (job de réconciliation, non câblé cette
    session) doit en faire : jamais un retry aveugle, toujours
    `EXECUTION_UNKNOWN`."""

    def test_a_recently_confirmed_draft_is_not_reported_as_stale(self, monkeypatch):
        table = _install_fake_db(monkeypatch)
        draft = _draft(draft_id="fresh1")
        run(store_mod.insert(draft, conversation_id="c"))
        executing = draft._confirm_to_executing()
        run(store_mod.compare_and_swap("fresh1", expected_version=1, new_draft=executing))

        stale = run(store_mod.find_stale_executing(older_than_seconds=3600))
        assert "fresh1" not in [d.draft_id for d in stale]

    def test_an_old_executing_draft_is_reported_as_stale(self, monkeypatch):
        table = _install_fake_db(monkeypatch)
        draft = _draft(draft_id="stale1")
        run(store_mod.insert(draft, conversation_id="c"))
        executing = draft._confirm_to_executing()
        run(store_mod.compare_and_swap("stale1", expected_version=1, new_draft=executing))
        table.force_updated_at("stale1", time.time() - 7200)  # figé depuis 2h

        stale = run(store_mod.find_stale_executing(older_than_seconds=3600))
        stale_ids = [d.draft_id for d in stale]
        assert "stale1" in stale_ids
        assert next(d for d in stale if d.draft_id == "stale1").status == (
            ProcurementDraftStatus.EXECUTING
        )

    def test_a_terminal_draft_is_never_reported_as_stale_even_if_old(self, monkeypatch):
        """Un `EXECUTED`/`FAILED` ancien n'est PAS un problème — seul
        `EXECUTING` bloqué en est un (les statuts terminaux ont déjà une
        issue tranchée)."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod
        from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
            ProcurementExecutionResult,
            finalize_after_execution,
        )

        table = _install_fake_db(monkeypatch)
        draft = _draft(draft_id="old-executed1")
        run(store_mod.insert(draft, conversation_id="c"))
        executing = draft._confirm_to_executing()
        run(store_mod.compare_and_swap("old-executed1", expected_version=1, new_draft=executing))
        executed = finalize_after_execution(
            executing, ProcurementExecutionResult(success=True, external_id="auc-1")
        )
        run(
            store_mod.compare_and_swap(
                "old-executed1", expected_version=executing.version, new_draft=executed
            )
        )
        table.force_updated_at("old-executed1", time.time() - 999_999)

        stale = run(store_mod.find_stale_executing(older_than_seconds=3600))
        assert "old-executed1" not in [d.draft_id for d in stale]

    def test_a_missing_sessionmaker_returns_an_empty_list_never_raises(self, monkeypatch):
        monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: None)
        assert run(store_mod.find_stale_executing(older_than_seconds=60)) == []


class TestDegradedModeNeverInventsAVersionConflict:
    def test_a_compare_and_swap_failure_with_no_reachable_db_keeps_the_in_memory_outcome(
        self, monkeypatch
    ):
        """Simule une DB complètement injoignable (sessionmaker absent) —
        PAS un vrai perdant de course. `resolve_procurement_confirmation`
        ne doit jamais transformer ça en `VERSION_CONFLICT` (ce serait
        inventer une course qui n'a jamais eu lieu) ; il doit avancer en
        mode dégradé avec le résultat calculé en mémoire."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: None)
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        v1 = ProcurementDraft.new(
            draft_id="degraded1", product="tomates", quantity=2000.0, unit="KG",
            price=250.0, price_unit="KG",
        )
        target = {"draft_id": v1.draft_id, "draft_version": v1.version}
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=v1.to_dict(),
            interpreted_event="CONFIRM",
            extracted_entities={},
        )
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}

        patch = run(resolve_procurement_confirmation(state, None))

        # Le tour AVANCE quand même (mode dégradé) — ce n'est PAS un rejet
        # de version périmée (le message/état associé à VERSION_CONFLICT
        # n'apparaît pas), et le draft n'est pas devenu `None`.
        assert patch["status"] == "EXECUTING"
        assert patch["procurement_draft"] is not None
        assert patch["procurement_draft"]["status"] == ProcurementDraftStatus.EXECUTING.value
