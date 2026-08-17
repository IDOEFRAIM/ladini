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
    """In-memory TTL store keyed by (session_id, menu_id)."""

    def __init__(self) -> None:
        self._store: Dict[str, MenuSnapshot] = {}

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
    ) -> MenuSnapshot:
        """Persist a new snapshot and returns it."""
        if not session_id:
            session_id = "anonymous"
        self._purge_expired()
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


menu_snapshot_store = MenuSnapshotStore()

__all__ = ["MenuSnapshot", "menu_snapshot_store", "MenuSnapshotStore"]
