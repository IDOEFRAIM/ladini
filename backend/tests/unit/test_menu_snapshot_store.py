"""`services/menu_snapshot.py::MenuSnapshotStore` — idempotence (audit
ui_engine, 2026-09-09, mandat §8-11).

Chaque test utilise un `session_id` UNIQUE (le store est un singleton
module-level partagé par toute la suite, voir
`tests/nodes/test_memory_stale_menu_snapshot.py` pour le même principe)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.services.menu_snapshot import (
    MenuSnapshotStore,
)


class TestPreExistingBehaviorWithoutIdempotencyKeyIsUnchanged:
    def test_two_saves_without_idempotency_key_always_mint_distinct_snapshots(self):
        """Non-régression : TOUS les appelants existants (aucun ne passe
        `idempotency_key`) doivent garder le comportement EXACT d'avant —
        chaque `save()` mint une identité fraîche."""
        store = MenuSnapshotStore()
        a = store.save("s1", {"1": "x"}, kind="stock")
        b = store.save("s1", {"1": "x"}, kind="stock")
        assert a.menu_id != b.menu_id


class TestIdempotentSaveIsReplaySafe:
    def test_same_session_same_key_returns_the_same_snapshot(self):
        store = MenuSnapshotStore()
        first = store.save(
            "s1", {"1": "a", "2": "b"}, kind="stock", idempotency_key="turn5:stock:abc"
        )
        second = store.save(
            "s1", {"1": "a", "2": "b"}, kind="stock", idempotency_key="turn5:stock:abc"
        )
        assert first.menu_id == second.menu_id
        assert first.mapping == second.mapping

    def test_same_key_different_session_never_collides(self):
        """I3/multi-session (mandat §33) : deux sessions différentes avec le
        MÊME menu logique (même clé d'idempotence) doivent obtenir des
        identités de snapshot INDÉPENDANTES — le store scope
        l'idempotence par session, jamais globalement."""
        store = MenuSnapshotStore()
        a = store.save("session-A", {"1": "x"}, kind="stock", idempotency_key="turn1:stock:same")
        b = store.save("session-B", {"1": "x"}, kind="stock", idempotency_key="turn1:stock:same")
        assert a.menu_id != b.menu_id
        assert a.session_id != b.session_id

    def test_different_idempotency_key_same_session_mints_a_new_snapshot(self):
        """Deux menus RÉELLEMENT différents dans la même session (turn
        différent, ou contenu différent) ne doivent jamais partager une
        identité."""
        store = MenuSnapshotStore()
        a = store.save("s1", {"1": "x"}, kind="stock", idempotency_key="turn1:stock:aaa")
        b = store.save("s1", {"1": "y"}, kind="stock", idempotency_key="turn2:stock:bbb")
        assert a.menu_id != b.menu_id

    def test_replay_after_expiry_mints_a_fresh_snapshot_instead_of_crashing(self):
        """Si le snapshot original a expiré/été purgé entre-temps, un replay
        avec la même clé d'idempotence doit produire un NOUVEAU snapshot
        valide plutôt que de renvoyer une référence morte."""
        store = MenuSnapshotStore()
        first = store.save(
            "s1", {"1": "x"}, kind="stock", idempotency_key="turnN:stock:ccc",
            ttl_seconds=60,
        )
        # Simule l'expiration en la forçant directement (pas de sleep réel).
        store._store[store._key("s1", first.menu_id)].expires_at = 0.0
        second = store.save(
            "s1", {"1": "x"}, kind="stock", idempotency_key="turnN:stock:ccc"
        )
        assert second.menu_id != first.menu_id
        assert second is not None


class TestMultiSessionIsolation:
    def test_identical_menu_content_in_two_sessions_never_shares_a_snapshot_id(self):
        store = MenuSnapshotStore()
        a = store.save("phone-A", {"1": "same-value"}, kind="stock")
        b = store.save("phone-B", {"1": "same-value"}, kind="stock")
        assert a.menu_id != b.menu_id
        # Et la résolution reste isolée : une session ne peut jamais lire
        # le snapshot de l'autre.
        assert store.get("phone-A", b.menu_id) is None
        assert store.get("phone-B", a.menu_id) is None


class TestSuccessiveMenusInTheSameSession:
    def test_a_second_menu_does_not_corrupt_the_first_snapshots_internal_state(self):
        """Mandat §34 : turn N -> menu A, turn N+1 -> menu B, même session —
        les deux snapshots doivent rester consultables indépendamment tant
        qu'aucun n'a expiré."""
        store = MenuSnapshotStore()
        menu_a = store.save("s1", {"1": "a-1"}, kind="stock", idempotency_key="turn1:stock:x")
        menu_b = store.save("s1", {"1": "b-1"}, kind="auction", idempotency_key="turn2:auction:y")
        assert menu_a.menu_id != menu_b.menu_id
        assert store.get("s1", menu_a.menu_id).mapping == {"1": "a-1"}
        assert store.get("s1", menu_b.menu_id).mapping == {"1": "b-1"}
