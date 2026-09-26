"""Runner de migrations Drizzle réutilisable — production, bootstrap E2E, tests.

Voir `runner.py` pour le détail. Point d'entrée public :

    from ladini.schema_migrations import apply_pending_migrations
"""
from __future__ import annotations

from .runner import (
    MigrationFailure,
    MigrationRunReport,
    apply_pending_migrations,
    migration_entries,
)

__all__ = [
    "MigrationFailure",
    "MigrationRunReport",
    "apply_pending_migrations",
    "migration_entries",
]
