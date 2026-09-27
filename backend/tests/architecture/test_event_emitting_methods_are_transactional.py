"""Une méthode de service qui écrit une intention d'event analytics (`BusinessEventEmitter`) doit être
exécutée sous `@transactional(write=True)` : `AgriDatabaseService._READ_ONLY_METHODS` ne commit
jamais, donc l'INSERT outbox serait annulé en silence (bug réel trouvé sur `search_products`)."""
from __future__ import annotations

import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2] / "src" / "ladini" / "services" / "database"


def _emitting_methods() -> set[str]:
    names: set[str] = set()
    for path in SERVICES.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                src_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if "BusinessEventEmitter" in src_names:
                    names.add(node.name)
    return names


def test_no_event_emitting_method_is_declared_read_only():
    from ladini.services.database.d import AgriDatabaseService

    emitting = _emitting_methods()
    assert "search_products" in emitting  # garde-fou : le scan voit bien les call-sites connus
    assert emitting.isdisjoint(AgriDatabaseService._READ_ONLY_METHODS), emitting & AgriDatabaseService._READ_ONLY_METHODS
