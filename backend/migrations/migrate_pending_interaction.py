"""One-shot migration — backfill `pending_interaction` on checkpoints
persisted BEFORE the `PendingInteraction` refonte (2026-09-02).

Contexte (voir core/pending_interaction.py::legacy_confirmation_bridge,
mandat de refonte §4) : `expected_input`/`waiting_for_confirmation` ne sont
plus des sources de vérité runtime — tout le chemin de production lit
`pending_interaction` exclusivement. Un checkpoint persisté AVANT ce
déploiement n'a pas ce champ ; s'il représentait une conversation au milieu
d'une confirmation, `get_pending_interaction()` retomberait sur `NONE` au
tour suivant (le pont `legacy_confirmation_bridge` compense déjà cela EN
LECTURE à chaque tour — mais un pont runtime permanent est exactement ce
que le mandat interdit comme état final).

Ce script applique la MÊME traduction, UNE FOIS, directement aux
checkpoints déjà stockés (table `agri_workspaces`, colonne JSONB
`metadata[_langgraph_state]`), en réutilisant l'API haut niveau du
`WorkspaceCheckpointer` (pas de parsing JSON à la main — le format interne
des checkpoints, y compris leur encodage `serde`, reste un détail
d'implémentation du checkpointer). Après un run réussi (0 checkpoint
modifié), `legacy_confirmation_bridge` peut être supprimée de
`get_pending_interaction()`.

Usage (from repo root):
    python -m migrations.migrate_pending_interaction            # dry-run (défaut)
    python -m migrations.migrate_pending_interaction --apply     # écrit réellement
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import text

from ladini.core.database import get_db
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    legacy_confirmation_bridge,
)
from ladini.workspace.checkpointer import WorkspaceCheckpointer
from ladini.workspace.store import WorkspaceStore

logger = logging.getLogger("Ladini.MigratePendingInteraction")
logging.basicConfig(level=logging.INFO)

SELECT_WORKSPACE_IDS = "SELECT workspace_id FROM agri_workspaces"


async def _workspace_ids() -> list[str]:
    async with get_db() as session:
        rows = (await session.execute(text(SELECT_WORKSPACE_IDS))).mappings().all()
    return [str(r["workspace_id"]) for r in rows]


async def migrate(*, apply: bool) -> None:
    store = WorkspaceStore()
    checkpointer = WorkspaceCheckpointer(store=store)

    ids = await _workspace_ids()
    logger.info("Loaded %s workspace ids", len(ids))

    migrated = 0
    inspected = 0
    for thread_id in ids:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        tup = await checkpointer.aget_tuple(config)
        if tup is None:
            continue
        inspected += 1
        channel_values = tup.checkpoint.get("channel_values")
        if not isinstance(channel_values, dict):
            continue
        if channel_values.get("pending_interaction"):
            continue  # déjà migré (écrit par le nouveau contrat)

        bridged = legacy_confirmation_bridge(channel_values)
        if bridged is None:
            continue  # rien à traduire pour ce checkpoint

        migrated += 1
        logger.info(
            "[%s] legacy confirmation detected -> pending_interaction=%s (goal=%s)",
            thread_id,
            bridged.kind.value,
            bridged.goal,
        )
        if not apply:
            continue

        new_checkpoint = dict(tup.checkpoint)
        new_channel_values = dict(channel_values)
        new_channel_values["pending_interaction"] = bridged.to_dict()
        new_checkpoint["channel_values"] = new_channel_values
        await checkpointer.aput(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": tup.checkpoint.get("id"),
                }
            },
            new_checkpoint,
            tup.metadata or {},
            {},
        )

    logger.info(
        "Done — inspected=%s, translated=%s, mode=%s",
        inspected,
        migrated,
        "APPLIED" if apply else "DRY-RUN (pass --apply to write)",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the translated pending_interaction (default: dry-run, log only).",
    )
    args = parser.parse_args()
    asyncio.run(migrate(apply=args.apply))


if __name__ == "__main__":
    main()
