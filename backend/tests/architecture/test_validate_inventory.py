"""`scripts/validate_inventory.py` — garde "exactement un scheduler" côté
inventaire de nodes (chantier Hetzner scale-out, §9/§44).

Importe le script directement (stdlib seule, pas de dépendance PyYAML côté
script — voir sa docstring) plutôt que de le lancer en sous-process, pour un
test rapide et sans dépendance sur l'environnement d'exécution (PATH, python3
vs python...)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "validate_inventory.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_inventory", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["validate_inventory"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


mod = _load_module()


class TestParseNodes:
    def test_parses_name_and_roles(self):
        text = """
nodes:
  - name: node-a
    host: 10.20.1.10
    roles: [app, scheduler, admin]
  - name: node-b
    host: 10.20.1.11
    roles: [app]
"""
        nodes = mod.parse_nodes(text)
        assert nodes == [
            {"name": "node-a", "roles": ["app", "scheduler", "admin"]},
            {"name": "node-b", "roles": ["app"]},
        ]

    def test_the_shipped_example_file_parses_and_is_valid(self):
        example = _SCRIPT_PATH.parents[1] / "infra" / "inventory.example.yml"
        nodes = mod.parse_nodes(example.read_text(encoding="utf-8"))
        assert nodes, "infra/inventory.example.yml doit contenir au moins un node"
        assert mod.validate(nodes) == []


class TestValidateExactlyOneScheduler:
    def test_zero_schedulers_is_rejected(self):
        nodes = [{"name": "a", "roles": ["app"]}]
        errors = mod.validate(nodes)
        assert any("scheduler" in e for e in errors)

    def test_two_schedulers_is_rejected(self):
        """LE cas central du chantier : deux `beat` actifs en même temps
        doubleraient chaque tâche planifiée (notifications, réconciliation)."""
        nodes = [
            {"name": "a", "roles": ["app", "scheduler"]},
            {"name": "b", "roles": ["app", "scheduler"]},
        ]
        errors = mod.validate(nodes)
        assert any("PLUSIEURS" in e or "scheduler" in e for e in errors)

    def test_exactly_one_scheduler_is_accepted(self):
        nodes = [
            {"name": "a", "roles": ["app", "scheduler"]},
            {"name": "b", "roles": ["app"]},
        ]
        assert mod.validate(nodes) == []

    def test_no_app_node_is_rejected(self):
        nodes = [{"name": "a", "roles": ["scheduler"]}]
        errors = mod.validate(nodes)
        assert any("app" in e for e in errors)

    def test_unknown_role_is_rejected(self):
        nodes = [{"name": "a", "roles": ["app", "scheduler", "bogus"]}]
        errors = mod.validate(nodes)
        assert any("bogus" in e for e in errors)

    def test_duplicate_node_names_are_rejected(self):
        nodes = [
            {"name": "a", "roles": ["app", "scheduler"]},
            {"name": "a", "roles": ["app"]},
        ]
        errors = mod.validate(nodes)
        assert any("dupliqu" in e for e in errors)

    def test_empty_inventory_is_rejected(self):
        assert mod.validate([]) != []
