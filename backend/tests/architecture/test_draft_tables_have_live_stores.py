"""Une table de draft migrée doit avoir un store qui l'écrit, lui-même utilisé par le moteur.

Incident (audit 2026-09-24, P0) : `marketplace.recurring_need_drafts` existait (migration
0003, miroir SQLAlchemy) sans AUCUN code d'écriture — la documentation laissait croire à une
persistance qui n'existait pas. Ce test rend ce décalage impossible à réintroduire."""
from __future__ import annotations

from pathlib import Path

import pytest

from ladini.domain import runtime_tables

_SRC = Path(__file__).resolve().parents[2] / "src" / "ladini"
_DRAFT_TABLES = sorted(t for t in runtime_tables.__all__ if t.endswith("_drafts"))


def test_draft_tables_are_discovered():
    assert "recurring_need_drafts" in _DRAFT_TABLES and len(_DRAFT_TABLES) >= 4


@pytest.mark.parametrize("table", _DRAFT_TABLES)
def test_every_draft_table_is_written_by_a_store_used_by_the_engine(table):
    store_module = f"{table[:-1]}_store"  # recurring_need_drafts -> recurring_need_draft_store
    store_path = _SRC / "services" / "database" / f"{store_module}.py"
    assert store_path.exists(), f"aucun store pour marketplace.{table}"
    source = store_path.read_text(encoding="utf-8")
    assert f"INSERT INTO marketplace.{table}" in source and f"UPDATE marketplace.{table}" in source
    engine_sources = "\n".join(p.read_text(encoding="utf-8") for p in (_SRC / "graphs").rglob("*.py"))
    assert f"import {store_module}" in engine_sources, f"{store_module} n'est utilisé par aucun flow"
