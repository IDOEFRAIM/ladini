"""`nodes/memory.py`'s generic `mapping_kind` selection-resolution machinery
— real incident (2026-08-30), see [[pricing-tiers-catalog-listing-display-2026-08]].

A buyer replying to the NEW tier-selection menu ("2") had that reply
silently resolved against a STALE `menu_snapshot_id`/`available_mapping_kind`
left over from an earlier, unrelated menu shown earlier in the SAME
conversation (e.g. a stock/vendor list). That stale resolution path popped
`selection_index` from the payload before `cart_management` ever got to read
it — the tier menu re-displayed forever, no error anywhere, because nothing
downstream knew a numeric reply had even been "consumed" by the wrong menu.

Root cause: `_interpret_fast_path`/interpreter set `selection_index`
correctly every time; the loss happened INSIDE `memory_update`'s generic
selection-resolution block, which only protects `selection_index` from being
popped for `mapping_kind == "product_vendor"` — every other kind (including
"no kind at all, but a truthy stale snapshot") got it popped unconditionally
once ANY snapshot happened to resolve the same index to SOMETHING.

Fix: `"pricing_tier"` is now an equally protected `mapping_kind` (memory.py),
and `cart.py`'s tier-menu responses explicitly claim it (overwriting
whatever was there) and clear the stale snapshot/mapping every time they
show the menu — so by the time the buyer's reply is processed, there is
nothing stale left to misresolve against.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)
from tests.conftest import StubRuntime, make_state, run


class TestPricingTierMappingKindIsProtected:
    def test_selection_index_survives_even_with_a_resolvable_stale_snapshot(self):
        """The exact live failure mode: a snapshot from an EARLIER, unrelated
        menu (kind='stock') still resolves index '2' to SOMETHING. Before the
        fix, memory.py didn't know `mapping_kind='pricing_tier'` should be
        protected the same way `product_vendor` is, so it popped
        `selection_index` after "helpfully" resolving it against the wrong
        menu entirely."""
        stale_snapshot = menu_snapshot_store.save(
            "session-live-repro",
            {"1": "stale-stock-id-1", "2": "stale-stock-id-2"},
            kind="stock",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id="session-live-repro",
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait", "quantity": 40},
            # This is the crux: mapping_kind is now "pricing_tier" (what
            # cart.py's tier menu sets), but a stale snapshot from an OLDER
            # "stock" menu is still sitting in menu_snapshot_id from before
            # cart.py started clearing it — the fix must protect
            # selection_index regardless of what that stale snapshot resolves to.
            working_memory={
                "available_mapping_kind": "pricing_tier",
                "menu_snapshot_id": stale_snapshot.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        assert result["transaction_payload"].get("selection_index") == 2, (
            "selection_index was popped/lost even though mapping_kind='pricing_tier' "
            "should protect it exactly like 'product_vendor' does"
        )

    def test_unprotected_mapping_kind_still_loses_selection_index_to_a_stale_snapshot(self):
        """Documents the OTHER half of the real incident: if `cart.py` had
        NOT started claiming `available_mapping_kind='pricing_tier'` (i.e. if
        the stale value from an earlier menu were still sitting there
        unclaimed), the generic resolver DOES still silently consume
        `selection_index` — proving the fix must actively CLAIM the kind
        every time the tier menu is shown, not just add it to the protected
        set. If this test ever starts failing, it means the protection
        became unconditional (a different, larger footgun), not that this
        specific bug reproduction stopped applying."""
        stale_snapshot = menu_snapshot_store.save(
            "session-live-repro-2",
            {"1": "stale-stock-id-1", "2": "stale-stock-id-2"},
            kind="stock",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id="session-live-repro-2",
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait", "quantity": 40},
            working_memory={
                # Stale kind from the OLDER menu, never overwritten — this is
                # what happened live BEFORE cart.py started claiming
                # "pricing_tier" on every tier-menu response.
                "available_mapping_kind": "stock",
                "menu_snapshot_id": stale_snapshot.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        assert "selection_index" not in result["transaction_payload"], (
            "this documents the bug mechanism itself — an unprotected/stale "
            "mapping_kind DOES lose selection_index to an unrelated snapshot"
        )
        # And it gets resolved into the WRONG field for the WRONG entity kind.
        assert result["transaction_payload"].get("stock_id") == "stale-stock-id-2"


class TestMenuAToMenuBIsolation:
    """Audit Bloc 2, Blocker D (2026-09-09), mandat §33-34 : garantit qu'une
    sélection est TOUJOURS résolue contre LE menu actif actuel — jamais un
    mélange `mapping` d'un menu + `snapshot`/`kind` d'un autre."""

    def test_active_menu_b_resolves_only_through_its_own_snapshot(self):
        """Menu A (product_vendor) affiché puis remplacé par Menu B
        (pricing_tier) — `available_mapping` a été perdu entre-temps (le
        cas réel que le filet de secours snapshot couvre, voir
        ui_engine.py), donc la résolution retombe sur le snapshot. Elle
        doit utiliser EXCLUSIVEMENT le snapshot B (kind cohérent avec le
        menu annoncé actif), jamais un vieux snapshot A qui traînerait."""
        session_id = "session-menu-a-then-b"
        snapshot_a = menu_snapshot_store.save(
            session_id, {"1": "vendor-A", "2": "vendor-B"}, kind="product_vendor"
        )
        snapshot_b = menu_snapshot_store.save(
            session_id, {"1": "tier-X", "2": "tier-Y"}, kind="pricing_tier"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id=session_id,
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait", "quantity": 40},
            available_mapping={},
            working_memory={
                "available_mapping_kind": "pricing_tier",
                "menu_snapshot_id": snapshot_b.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        # `pricing_tier` garde `selection_index` intact pour cart_management
        # (voir la classe ci-dessus) — la preuve d'isolation porte donc sur
        # le fait qu'AUCUNE valeur de snapshot A ("vendor-B") n'apparaît
        # nulle part dans le payload résultant.
        assert "vendor-B" not in result["transaction_payload"].values()
        assert snapshot_a.menu_id != snapshot_b.menu_id  # sanity du test lui-même

    def test_mismatched_mapping_kind_and_snapshot_is_never_silently_resolved(self):
        """État délibérément invalide (mandat §34) : `available_mapping_kind`
        annonce un menu B mais `menu_snapshot_id` référence encore un
        snapshot A — la résolution ne doit PAS mélanger les deux ; elle doit
        échouer proprement (aucune valeur de A dans le résultat) plutôt que
        de résoudre silencieusement contre le mauvais menu."""
        session_id = "session-mismatched-kind"
        snapshot_a = menu_snapshot_store.save(
            session_id, {"1": "stale-stock-id-1", "2": "stale-stock-id-2"}, kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id=session_id,
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait", "quantity": 40},
            available_mapping={},
            working_memory={
                # Le menu ANNONCÉ actif est "pricing_tier" — mais le
                # snapshot référencé est encore celui de l'ANCIEN menu
                # "stock". Incohérence délibérée.
                "available_mapping_kind": "pricing_tier",
                "menu_snapshot_id": snapshot_a.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        payload = result["transaction_payload"]
        assert "stale-stock-id-2" not in payload.values(), (
            "un snapshot dont le kind ne correspond pas au menu annoncé actif "
            "ne doit jamais être utilisé pour résoudre la sélection"
        )
        assert payload.get("stock_id") is None


# =====================================================================
# BLOC 2 — passe finale (2026-09-09), Invariant B : SAME-KIND stale snapshot
# =====================================================================
#
# Le garde posé le 2026-09-09 ne compare que `snapshot.kind` vs
# `working_memory.available_mapping_kind`. C'est insuffisant : deux menus
# SUCCESSIFS du MÊME kind (deux listes de stock, deux listes de vendeurs…)
# ont par construction le même `kind`. Le garde laisse alors passer un
# snapshot appartenant au menu PRÉCÉDENT.
#
# Reachable en production, pas théorique : `nodes/ui_engine.py` n'écrit
# `menu_snapshot_id` QUE si `menu_snapshot_store.save()` a réussi ET qu'une
# identité de session existe (voir sa branche `if snapshot_id is not None`),
# alors qu'il écrit `available_mapping` INCONDITIONNELLEMENT. Un échec du
# store (ou une session sans identité) sur le menu B laisse donc le
# `menu_snapshot_id` du menu A vivant à côté du mapping B — exactement
# l'état incohérent reproduit ici.


class TestSameKindStaleSnapshotIsNeverResolved:
    """Mandat §10-13 : le fallback snapshot n'est légitime QUE si le
    snapshot appartient au menu ACTIF — pas seulement s'il partage son
    `kind`."""

    def test_same_kind_stale_snapshot_must_not_resolve_a_token_absent_from_the_active_menu(
        self,
    ):
        # Menu A (tour précédent) — kind "stock", 2 entrées.
        stale_a = menu_snapshot_store.save(
            "session-same-kind",
            {"1": "stock-A1", "2": "stock-A2"},
            kind="stock",
        )
        # Menu B (menu réellement à l'écran) — MÊME kind, une seule entrée.
        active_mapping_b = {"1": "stock-B1"}

        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-same-kind",
            extracted_entities={"selection_index": 2},
            transaction_payload={},
            available_mapping=active_mapping_b,
            menu_snapshot_id=stale_a.menu_id,
            working_memory={"available_mapping_kind": "stock"},
        )
        result = run(memory_update(st, StubRuntime()))
        payload = result.get("transaction_payload") or {}

        assert payload.get("stock_id") != "stock-A2", (
            "« 2 » a été résolu contre le menu A (snapshot périmé) alors que "
            "le menu ACTIF (B) ne contient pas d'entrée 2 — le seul garde "
            "existant (kind identique) ne protège pas ce cas."
        )
        assert "stock_id" not in payload, (
            "aucune résolution ne doit avoir lieu : le token n'existe pas "
            "dans le menu actif et le snapshot n'appartient pas à ce menu"
        )

    def test_snapshot_belonging_to_the_active_menu_still_resolves(self):
        """Non-régression : le filet de secours garde sa raison d'être —
        `available_mapping` effacé de l'état (nettoyage de fin de tour) et
        snapshot du menu actif ⇒ résolution normale."""
        snap = menu_snapshot_store.save(
            "session-live-menu",
            {"1": "stock-live-1", "2": "stock-live-2"},
            kind="stock",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-live-menu",
            extracted_entities={"selection_index": 2},
            transaction_payload={},
            available_mapping={},
            menu_snapshot_id=snap.menu_id,
            working_memory={"available_mapping_kind": "stock"},
        )
        result = run(memory_update(st, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("stock_id") == "stock-live-2"

    def test_snapshot_identical_to_the_active_mapping_is_accepted(self):
        """Cas limite : mapping actif ET snapshot présents et IDENTIQUES
        (même menu, double source) — la résolution reste permise."""
        mapping = {"1": "stock-same-1", "2": "stock-same-2"}
        snap = menu_snapshot_store.save(
            "session-identical", dict(mapping), kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-identical",
            extracted_entities={"selection_index": 2},
            transaction_payload={},
            available_mapping=dict(mapping),
            menu_snapshot_id=snap.menu_id,
            working_memory={"available_mapping_kind": "stock"},
        )
        result = run(memory_update(st, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("stock_id") == "stock-same-2"

    def test_canonical_snapshot_id_beats_legacy_working_memory_copy(self):
        """Mandat §18 : `state.menu_snapshot_id` (canonique) l'emporte
        TOUJOURS sur `working_memory.menu_snapshot_id` (legacy)."""
        legacy_a = menu_snapshot_store.save(
            "session-canon", {"1": "legacy-A1"}, kind="stock"
        )
        canonical_b = menu_snapshot_store.save(
            "session-canon", {"1": "canonical-B1"}, kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-canon",
            extracted_entities={"selection_index": 1},
            transaction_payload={},
            available_mapping={},
            menu_snapshot_id=canonical_b.menu_id,
            working_memory={
                "available_mapping_kind": "stock",
                "menu_snapshot_id": legacy_a.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("stock_id") == "canonical-B1"

    def test_legacy_working_memory_snapshot_is_used_when_no_canonical_id_exists(self):
        """Compat : un checkpoint ANTÉRIEUR à la déclaration du champ
        canonique n'a que la copie `working_memory` — elle doit encore
        servir de filet."""
        legacy = menu_snapshot_store.save(
            "session-legacy-only", {"1": "legacy-only-1"}, kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-legacy-only",
            extracted_entities={"selection_index": 1},
            transaction_payload={},
            available_mapping={},
            working_memory={
                "available_mapping_kind": "stock",
                "menu_snapshot_id": legacy.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        assert (result.get("transaction_payload") or {}).get("stock_id") == "legacy-only-1"


class TestSnapshotIdentityHelper:
    """Unités directes de `snapshot_belongs_to_active_menu` — la preuve
    d'identité elle-même (mandat §12/§13)."""

    def _snap(self, mapping, kind="stock"):
        return menu_snapshot_store.save("session-helper", dict(mapping), kind=kind)

    def test_absent_snapshot_is_refused(self):
        from ladini.graphs.agents.market_coach.services.menu_snapshot import (
            snapshot_belongs_to_active_menu,
        )
        ok, reason = snapshot_belongs_to_active_menu(
            None,
            active_mapping={"1": "a"},
            active_kind="stock",
            selection_is_pending=True,
        )
        assert ok is False and reason == "snapshot_absent"

    def test_same_kind_but_different_mapping_is_refused(self):
        from ladini.graphs.agents.market_coach.services.menu_snapshot import (
            snapshot_belongs_to_active_menu,
        )
        snap = self._snap({"1": "A1", "2": "A2"})
        ok, reason = snapshot_belongs_to_active_menu(
            snap,
            active_mapping={"1": "B1"},
            active_kind="stock",
            selection_is_pending=True,
        )
        assert ok is False and reason == "mapping_identity_mismatch"

    def test_identical_mapping_is_accepted(self):
        from ladini.graphs.agents.market_coach.services.menu_snapshot import (
            snapshot_belongs_to_active_menu,
        )
        snap = self._snap({"1": "A1"})
        ok, reason = snapshot_belongs_to_active_menu(
            snap,
            active_mapping={"1": "A1"},
            active_kind="stock",
            selection_is_pending=True,
        )
        assert ok is True and reason == "snapshot_is_active_menu"

    def test_without_active_mapping_the_kind_still_filters(self):
        from ladini.graphs.agents.market_coach.services.menu_snapshot import (
            snapshot_belongs_to_active_menu,
        )
        snap = self._snap({"1": "A1"}, kind="stock")
        ok, reason = snapshot_belongs_to_active_menu(
            snap,
            active_mapping={},
            active_kind="product_vendor",
            selection_is_pending=True,
        )
        assert ok is False and reason == "kind_mismatch"

    def test_without_active_mapping_a_matching_kind_is_the_sole_source(self):
        from ladini.graphs.agents.market_coach.services.menu_snapshot import (
            snapshot_belongs_to_active_menu,
        )
        snap = self._snap({"1": "A1"}, kind="stock")
        ok, reason = snapshot_belongs_to_active_menu(
            snap,
            active_mapping=None,
            active_kind="stock",
            selection_is_pending=True,
        )
        assert ok is True and reason == "no_active_mapping_snapshot_is_sole_source"


# =====================================================================
# BLOC 2 — micro-passe finale (2026-09-09), Sujet A : le fallback snapshot
# exige une INTERACTION DE SÉLECTION ACTIVE
# =====================================================================
#
# La règle « `available_mapping` vide ⇒ le snapshot est la seule source »
# était trop permissive : un snapshot d'un menu TERMINÉ survit au
# nettoyage de fin de tour (le store a un TTL de 30 min, et
# `menu_snapshot_id` peut rester dans un checkpoint), si bien qu'un « 2 »
# envoyé plus tard, sans aucun menu à l'écran, ressuscitait le menu A.


class TestSnapshotFallbackRequiresAnActiveSelection:
    def test_stale_snapshot_is_never_used_when_nothing_is_pending(self):
        """Mandat §3 : mapping vide + snapshot survivant + AUCUNE
        interaction en attente ⇒ le menu A ne doit pas ressusciter."""
        snap_a = menu_snapshot_store.save(
            "session-no-pending",
            {"1": "seller_A", "2": "seller_B"},
            kind="product_vendor",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="NONE",          # -> pending_interaction = NONE
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id="session-no-pending",
            extracted_entities={"selection_index": 2},
            transaction_payload={},
            available_mapping={},
            menu_snapshot_id=snap_a.menu_id,
            # PAS de `available_mapping_kind` : le menu est termine. Toute
            # resolution atterrirait donc dans `resolved_id` (branche
            # generique de memory.py) — signal OBSERVABLE, contrairement a
            # `product_vendor` qui n'injecte rien par design.
            working_memory={},
        )
        result = run(memory_update(st, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("resolved_id") != "seller_B"
        assert "resolved_id" not in payload, (
            "un snapshot d'un menu terminé a été ressuscité alors qu'aucune "
            "sélection n'était attendue"
        )

    def test_snapshot_fallback_still_works_when_a_selection_menu_is_pending(self):
        """Mandat §6 : le vrai filet de secours ne doit pas être cassé."""
        snap_a = menu_snapshot_store.save(
            "session-pending-menu",
            {"1": "seller_A", "2": "seller_B"},
            kind="product_vendor",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",     # -> SELECTION_MENU actif
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-pending-menu",
            extracted_entities={"selection_index": 2},
            transaction_payload={},
            available_mapping={},
            menu_snapshot_id=snap_a.menu_id,
            working_memory={"available_mapping_kind": "product_vendor"},
        )
        result = run(memory_update(st, StubRuntime()))
        # `product_vendor` n'injecte pas d'id (cart_management résout par
        # position) : la preuve que le snapshot A a bien été consulté est
        # que `selection_index` est PRÉSERVÉ pour cart_management.
        payload = result.get("transaction_payload") or {}
        assert payload.get("selection_index") == 2

    def test_a_generic_kind_resolves_through_the_snapshot_when_menu_is_pending(self):
        snap = menu_snapshot_store.save(
            "session-pending-stock", {"1": "s1", "2": "s2"}, kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="SALES_UPDATE_PRODUCT",
            detected_intent="UNKNOWN",
            session_id="session-pending-stock",
            extracted_entities={"selection_index": 2},
            transaction_payload={},
            available_mapping={},
            menu_snapshot_id=snap.menu_id,
            working_memory={"available_mapping_kind": "stock"},
        )
        result = run(memory_update(st, StubRuntime()))
        assert (result.get("transaction_payload") or {}).get("stock_id") == "s2"

    def test_after_the_menu_ended_the_surviving_snapshot_is_ignored(self):
        """Mandat §7 : menu terminé (pending effacé, mapping effacé), le
        snapshot survit par checkpoint — « 1 » ne doit rien ressusciter."""
        snap = menu_snapshot_store.save(
            "session-ended", {"1": "ended-1", "2": "ended-2"}, kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="NONE",
            current_goal=None,
            detected_intent="UNKNOWN",
            session_id="session-ended",
            extracted_entities={"selection_index": 1},
            transaction_payload={},
            available_mapping={},
            menu_snapshot_id=snap.menu_id,
            working_memory={},
        )
        result = run(memory_update(st, StubRuntime()))
        assert "resolved_id" not in (result.get("transaction_payload") or {})

    def test_legacy_working_memory_snapshot_also_requires_an_active_selection(self):
        """Mandat §8 : le repli legacy ne doit pas non plus ressusciter une
        interaction terminée."""
        legacy = menu_snapshot_store.save(
            "session-legacy-nopending", {"1": "legacy-1"}, kind="stock"
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="NONE",
            current_goal=None,
            detected_intent="UNKNOWN",
            session_id="session-legacy-nopending",
            extracted_entities={"selection_index": 1},
            transaction_payload={},
            available_mapping={},
            working_memory={
                "available_mapping_kind": "stock",
                "menu_snapshot_id": legacy.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        assert "stock_id" not in (result.get("transaction_payload") or {})

    def test_no_active_selection_is_refused_before_any_other_check(self):
        """La condition PRÉALABLE : même un snapshot parfaitement cohérent
        (mapping identique, kind identique) est refusé si aucune sélection
        n'est attendue ce tour-ci."""
        from ladini.graphs.agents.market_coach.services.menu_snapshot import (
            snapshot_belongs_to_active_menu,
        )
        snap = menu_snapshot_store.save(
            "session-no-active-selection", {"1": "A1"}, kind="stock"
        )
        ok, reason = snapshot_belongs_to_active_menu(
            snap,
            active_mapping={"1": "A1"},
            active_kind="stock",
            selection_is_pending=False,
        )
        assert ok is False and reason == "no_active_selection"
