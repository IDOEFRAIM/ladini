"""Menu Snapshot Store — persistant mapping between UI menus and business ids.

This module provides a lightweight, in-process registry that keeps a durable
mapping between the AG-UI menus rendered for a session and the corresponding
business identifiers. By centralising the mapping we make numeric selections
idempotent: even if ``available_mapping`` is wiped out of the LangGraph state,
we can rebuild the association from the snapshot id.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(slots=True)
class MenuSnapshot:
    menu_id: str
    session_id: str
    mapping: Dict[str, str]
    kind: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    expires_at: float = field(default_factory=lambda: time.time() + 1800)

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class MenuSnapshotStore:
    """In-memory TTL store keyed by (session_id, menu_id).

    Idempotency (2026-09-09, audit ui_engine) : `save(..., idempotency_key=...)`
    is the ONLY mechanism in this store that guarantees replay-safety — a
    LangGraph node retry/resume that calls `save()` again for the exact same
    logical menu (same session, same turn, same content) must NOT mint a
    second, incompatible snapshot identity. Callers that never pass
    `idempotency_key` (all pre-existing call sites) get the EXACT prior
    behavior — always a fresh `menu_id` — this is purely additive."""

    def __init__(self) -> None:
        self._store: Dict[str, MenuSnapshot] = {}
        # (session_id, idempotency_key) -> menu_id. Reverse index used only
        # to short-circuit `save()` into returning the SAME snapshot for a
        # replayed call — never consulted by `get`/`resolve`/`clear`.
        self._idempotency: Dict[tuple[str, str], str] = {}

    def _key(self, session_id: str, menu_id: str) -> str:
        return f"{session_id}:{menu_id}"

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [key for key, snap in self._store.items() if snap.expires_at <= now]
        for key in expired:
            self._store.pop(key, None)

    def save(
        self,
        session_id: str,
        mapping: Dict[str, str],
        *,
        kind: str,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: int = 1800,
        idempotency_key: Optional[str] = None,
    ) -> MenuSnapshot:
        """Persist a new snapshot and returns it — or, when `idempotency_key`
        is given and a LIVE snapshot already exists for
        `(session_id, idempotency_key)`, return that SAME snapshot instead
        of minting a new identity (replay-safe, see class docstring)."""
        if not session_id:
            session_id = "anonymous"
        self._purge_expired()

        if idempotency_key:
            idem_lookup = (session_id, idempotency_key)
            existing_menu_id = self._idempotency.get(idem_lookup)
            if existing_menu_id is not None:
                existing = self._store.get(self._key(session_id, existing_menu_id))
                if existing is not None:
                    return existing
                # Snapshot expired/purged since the idempotency entry was
                # recorded — fall through and mint a fresh one below.
                self._idempotency.pop(idem_lookup, None)

        menu_id = uuid.uuid4().hex[:12]
        snapshot = MenuSnapshot(
            menu_id=menu_id,
            session_id=session_id,
            mapping=dict(mapping or {}),
            kind=kind,
            metadata=dict(metadata or {}),
            expires_at=time.time() + max(60, ttl_seconds),
        )
        self._store[self._key(session_id, menu_id)] = snapshot
        if idempotency_key:
            self._idempotency[(session_id, idempotency_key)] = menu_id
        return snapshot

    def get(self, session_id: str, menu_id: str) -> Optional[MenuSnapshot]:
        if not session_id or not menu_id:
            return None
        self._purge_expired()
        return self._store.get(self._key(session_id, menu_id))

    def resolve(self, session_id: str, menu_id: str, selection: Any) -> Optional[str]:
        snapshot = self.get(session_id, menu_id)
        if not snapshot:
            return None
        return snapshot.mapping.get(str(selection))

    def clear(self, session_id: str, menu_id: str) -> None:
        if not session_id or not menu_id:
            return
        self._store.pop(self._key(session_id, menu_id), None)


# ---------------------------------------------------------------------------
# Identité du menu ACTIF (BLOC 2, passe finale 2026-09-09 — Invariant B)
# ---------------------------------------------------------------------------


def snapshot_belongs_to_active_menu(
    snapshot: Optional[MenuSnapshot],
    *,
    active_mapping: Optional[Dict[str, Any]],
    active_kind: Optional[str],
    selection_is_pending: bool,
) -> tuple[bool, str]:
    """Le `snapshot` est-il celui du menu ACTUELLEMENT à l'écran ?

    Retourne `(autorisé, raison)` — la raison est destinée au log de
    l'appelant, jamais à l'utilisateur.

    Pourquoi cette fonction existe (et pourquoi le seul `kind` ne suffit
    pas) : deux menus SUCCESSIFS du même type (deux listes de stock, deux
    listes de vendeurs…) partagent par construction le même `kind`. Un
    garde `snapshot.kind == active_kind` laisse donc passer un snapshot du
    menu PRÉCÉDENT — et « 2 » se résout alors contre un menu qui n'est
    plus affiché.

    Cet état incohérent (mapping du menu B + `menu_snapshot_id` du menu A)
    est ATTEIGNABLE en production, pas théorique : `nodes/ui_engine.py`
    écrit `available_mapping` inconditionnellement mais n'écrit
    `menu_snapshot_id` QUE si `MenuSnapshotStore.save()` a réussi et qu'une
    identité de session existe (branche `if snapshot_id is not None`). Un
    échec du store sur le menu B laisse donc vivre l'id du menu A.

    Condition PRÉALABLE (micro-passe finale 2026-09-09, Sujet A) :
    `selection_is_pending` — une interaction de sélection doit être active
    (`PendingInteraction.kind` ∈ {SELECTION_MENU} ∪ CART_TUNNEL_KINDS, voir
    `core/pending_interaction.py`). L'ensemble est très légèrement plus
    large que le minimum formulé par le mandat (SELECTION_MENU seul) parce
    que `get_pending_interaction` DÉRIVE en priorité les kinds du tunnel
    panier (`SELECT_PRODUCER`/`SELECT_PRICING_TIER`/…) depuis
    `vendor_selection_context`/`tier_selection_context` : un menu panier
    rendu par `ui_engine` (seul émetteur de snapshots, qui pose bien
    SELECTION_MENU au moment du rendu) se présente donc au tour suivant
    sous un kind panier. Les deux formulations INTERDISENT exactement le
    même cas dangereux — `NONE`, `CONFIRM_ACTION`, `PROVIDE_LOCATION`,
    `ENTER_FIELD`, `VERIFY_OTP`, `CLARIFY_INTENT` — c.-à-d. « aucune
    sélection attendue ».

    Preuve d'identité utilisée, SANS nouvelle infrastructure (mandat §12/§14
    /§15) — par ordre de force :

    1. `active_mapping` non vide ⇒ c'est LUI le menu à l'écran (écrit
       atomiquement avec `menu_snapshot_id` par `ui_engine`, dans le même
       patch de nœud). Le snapshot n'est alors accepté que s'il porte le
       MÊME mapping — c'est-à-dire s'il EST ce menu. Un mapping différent
       prouve que le snapshot appartient à un autre menu : refusé.
    2. `active_mapping` vide/absent ⇒ aucun menu concurrent en état ; le
       snapshot est la seule source (c'est exactement sa raison d'être :
       survivre au nettoyage de `available_mapping` en fin de tour). On
       retombe alors sur le signal plus faible mais toujours utile du
       `kind`.

    Conséquence volontaire du cas 1 : quand un mapping actif existe, le
    fallback snapshot ne peut JAMAIS résoudre un token que le mapping actif
    ne résout pas déjà lui-même. C'est le comportement recherché — une
    telle résolution serait, par définition, une résolution contre un autre
    menu que celui affiché.
    """
    if snapshot is None:
        return False, "snapshot_absent"

    # (2026-09-09, micro-passe finale — Sujet A) : PREMIÈRE condition, avant
    # toute comparaison de contenu. Un snapshot survit à son menu (TTL de 30
    # min côté store, `menu_snapshot_id` persisté dans le checkpoint) : sans
    # ce garde, un « 2 » envoyé APRÈS la fin du menu ressuscitait celui-ci
    # (bug reproduit : `seller_B` / `ended-1` / `legacy-1` résolus alors
    # qu'aucune interaction n'attendait de sélection). Le filet de secours
    # n'a de sens que si une sélection est RÉELLEMENT attendue ce tour-ci.
    if not selection_is_pending:
        return False, "no_active_selection"

    if active_mapping:
        if dict(snapshot.mapping or {}) == dict(active_mapping):
            return True, "snapshot_is_active_menu"
        return False, "mapping_identity_mismatch"

    if active_kind and snapshot.kind and snapshot.kind != active_kind:
        return False, "kind_mismatch"

    return True, "no_active_mapping_snapshot_is_sole_source"


menu_snapshot_store = MenuSnapshotStore()

__all__ = [
    "MenuSnapshot",
    "menu_snapshot_store",
    "MenuSnapshotStore",
    "snapshot_belongs_to_active_menu",
]
