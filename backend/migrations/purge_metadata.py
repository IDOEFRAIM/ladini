"""Purge script to minimize workspace metadata.

Usage (from repo root):
    python -m migrations.purge_metadata
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import text

from ladini.core.database import get_db
from ladini.workspace.metadata import filter_metadata_dict

logger = logging.getLogger("Ladini.MetadataPurge")
logging.basicConfig(level=logging.INFO)

SELECT_ALL = "SELECT workspace_id, metadata FROM agri_workspaces"
UPDATE_ONE = "UPDATE agri_workspaces SET metadata = CAST(:metadata AS JSONB) WHERE workspace_id = :workspace_id"


async def purge_metadata() -> None:
    async with get_db() as session:
        rows = (await session.execute(text(SELECT_ALL))).mappings().all()

    logger.info("Loaded %s workspace rows", len(rows))

    async with get_db() as session:
        for row in rows:
            raw_meta = row.get("metadata")
            if isinstance(raw_meta, str):
                raw_meta = json.loads(raw_meta or "{}")
            filtered = filter_metadata_dict(raw_meta)
            await session.execute(
                text(UPDATE_ONE),
                {
                    "workspace_id": row["workspace_id"],
                    "metadata": json.dumps(filtered),
                },
            )
        await session.commit()

    logger.info("Workspace metadata purge completed")


def main() -> None:
    asyncio.run(purge_metadata())


if __name__ == "__main__":
    main()
