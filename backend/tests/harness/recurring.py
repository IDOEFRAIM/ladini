"""Doublures de la persistance des besoins récurrents (sans PostgreSQL).

- `InMemoryDraftTable` : même contrat que `services/database/recurring_need_draft_store.py`
  (une ligne par draft, CAS sur `version`, aller-retour JSON du payload).
- `RecurringSupplyServerDouble` : même CONTRAT que la garde de
  `services/database/recurring_supply.py` (registre de confirmation dans la ligne du draft :
  rejeu si EXECUTED, exécution + passage à EXECUTED sinon). La garde RÉELLE est prouvée contre
  PostgreSQL dans `tests/schema/test_recurring_need_confirmation_ledger.py` ; ce double
  permet de tester le moteur conversationnel sans base.

Ces doublures rendent visibles les pannes étudiées par la Phase 2 : écriture refusée
(`fail_writes`), exécution qui commit puis perd sa réponse (`lose_response_after_commit`).
"""
from __future__ import annotations

import copy
import itertools
import json
import time
from typing import Any, Dict, List, Optional

from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
)


class InMemoryDraftTable:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.fail_writes = False

    # -- contrat du store ---------------------------------------------
    def _to_draft(self, draft_id: str) -> Optional[RecurringNeedDraft]:
        row = self.rows.get(draft_id)
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload.update({"draft_id": draft_id, "version": row["version"], "status": row["status"]})
        return RecurringNeedDraft.from_dict(payload)

    async def load(self, draft_id: str) -> Optional[RecurringNeedDraft]:
        return self._to_draft(draft_id)

    async def insert(self, draft: RecurringNeedDraft, *, conversation_id: str) -> bool:
        if self.fail_writes or draft.draft_id in self.rows:
            return False
        self.rows[draft.draft_id] = {
            "conversation_id": conversation_id,
            "version": draft.version,
            "status": draft.status.value,
            "payload": json.dumps(draft.to_dict()),
            "updated_at": time.time(),
        }
        return True

    async def compare_and_swap(self, draft_id: str, *, expected_version: int, new_draft: RecurringNeedDraft) -> bool:
        row = self.rows.get(draft_id)
        if self.fail_writes or row is None or row["version"] != expected_version:
            return False
        row.update(
            version=new_draft.version,
            status=new_draft.status.value,
            payload=json.dumps(new_draft.to_dict()),
            updated_at=time.time(),
        )
        return True

    async def find_in_doubt(self, *, older_than_seconds: float) -> List[tuple]:
        return self._older(("EXECUTING", "EXECUTION_UNKNOWN"), older_than_seconds)

    async def find_abandoned(self, *, older_than_seconds: float) -> List[tuple]:
        return self._older(("DRAFT",), older_than_seconds)

    def _older(self, statuses, older_than_seconds: float) -> List[tuple]:
        cutoff = time.time() - older_than_seconds
        return [
            (self._to_draft(draft_id), row["conversation_id"])
            for draft_id, row in self.rows.items()
            if row["status"] in statuses and row["updated_at"] < cutoff
        ]

    # -- aides de test --------------------------------------------------
    def age(self, draft_id: str, seconds: float) -> None:
        self.rows[draft_id]["updated_at"] -= seconds

    def status_of(self, draft_id: str) -> Optional[str]:
        row = self.rows.get(draft_id)
        return row["status"] if row else None


_STORE_FUNCTIONS = ("load", "insert", "compare_and_swap", "find_in_doubt", "find_abandoned")


def install_draft_table(table: InMemoryDraftTable, patcher) -> InMemoryDraftTable:
    """Branche `table` à la place des fonctions du store (patcher = monkeypatch.setattr
    ou `lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value))`)."""
    from ladini.services.database import recurring_need_draft_store as store

    for name in _STORE_FUNCTIONS:
        patcher(store, name, getattr(table, name))
    return table


class RecurringSupplyServerDouble:
    """Contrat de `create_recurring_need(s)` avec registre de confirmation."""

    def __init__(self, table: InMemoryDraftTable) -> None:
        self.table = table
        self.created: List[Dict[str, Any]] = []  # besoins RÉELLEMENT créés (lignes)
        self.executions = 0  # transactions métier réellement commitées
        self.lose_response_after_commit = 0  # nb d'appels : commit OK puis réponse perdue
        self.fail_with: Optional[Dict[str, Any]] = None  # réponse d'erreur métier (rollback)
        self._ids = itertools.count(1)

    def _guard(self, draft_id: Optional[str], draft_version: Optional[int]):
        if not draft_id:
            return None, None
        row = self.table.rows.get(draft_id)
        if row is None:
            return {"status": "error", "message": "Demande introuvable : confirmation impossible."}, None
        payload = json.loads(row["payload"])
        if payload.get("execution_version") != draft_version:
            return {"status": "error", "message": "Cette confirmation n'est plus à jour."}, None
        if row["status"] == "EXECUTED":
            return {"status": "success", **payload["execution_result"], "replayed": True}, None
        if row["status"] not in ("EXECUTING", "EXECUTION_UNKNOWN"):
            return {"status": "error", "message": f"Demande non confirmable (statut {row['status']})."}, None
        return None, (row, payload)

    def _commit(self, ledger, result: Dict[str, Any]) -> None:
        self.executions += 1
        if ledger is not None:
            row, payload = ledger
            row["version"] += 1
            row["status"] = "EXECUTED"
            payload.update(status="EXECUTED", version=row["version"], execution_result=result)
            row["payload"] = json.dumps(payload)

    def _run(self, items: List[Dict[str, Any]], shared: Dict[str, Any], batch: bool):
        replay, ledger = self._guard(shared.get("draft_id"), shared.get("draft_version"))
        if replay is not None:
            return replay
        if self.fail_with is not None:
            return copy.deepcopy(self.fail_with)
        created = []
        for item in items:
            need_id = f"need-{next(self._ids)}"
            self.created.append({"recurring_need_id": need_id, **item, **shared})
            created.append({"recurring_need_id": need_id, "sub_category": item.get("product_query")})
        result = {"items": created} if batch else created[0]
        self._commit(ledger, result)
        if self.lose_response_after_commit > 0:
            self.lose_response_after_commit -= 1
            raise TimeoutError("réponse MCP perdue après COMMIT")
        return {"status": "success", **result}

    def create_recurring_need(self, **kw: Any) -> Dict[str, Any]:
        item = {k: kw.get(k) for k in ("product_query", "quantity", "unit")}
        return self._run([item], {k: v for k, v in kw.items() if k not in item}, batch=False)

    def create_recurring_needs(self, **kw: Any) -> Dict[str, Any]:
        items = list(kw.get("items") or [])
        return self._run(items, {k: v for k, v in kw.items() if k != "items"}, batch=True)


__all__ = ["InMemoryDraftTable", "RecurringSupplyServerDouble", "install_draft_table"]
