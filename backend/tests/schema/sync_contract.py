"""Synchronise le CONTRAT DE SCHÉMA depuis le dépôt frontend (Drizzle = source de vérité).

Le schéma PostgreSQL est défini et migré par Drizzle (dépôt frontend). Le backend n'a
pas accès à ce dépôt en CI : il embarque donc une copie versionnée du contrat
(`backend/schema_contract/`) — dernier snapshot + migrations SQL + journal — sur laquelle
les tests de cohérence tournent.

    python tests/schema/sync_contract.py --frontend ../../frontag           # met à jour la copie
    python tests/schema/sync_contract.py --frontend ../../frontag --check   # échoue si elle est périmée

La copie ne doit JAMAIS être éditée à la main : modifier le schéma Drizzle, `drizzle-kit generate`,
puis re-synchroniser.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "schema_contract"


def _sources(frontend: Path) -> dict[str, Path]:
    drizzle = frontend / "drizzle"
    journal = json.loads((drizzle / "meta" / "_journal.json").read_text(encoding="utf-8"))
    entries = journal["entries"]
    out = {"migrations/_journal.json": drizzle / "meta" / "_journal.json"}
    for e in entries:
        out[f"migrations/{e['tag']}.sql"] = drizzle / f"{e['tag']}.sql"
    last = entries[-1]["idx"]
    out["drizzle_snapshot.json"] = drizzle / "meta" / f"{last:04d}_snapshot.json"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend", required=True, type=Path)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    src = _sources(a.frontend.resolve())
    stale: list[str] = []
    for rel, path in src.items():
        dst = CONTRACT / rel
        same = dst.exists() and dst.read_bytes().replace(b"\r\n", b"\n") == path.read_bytes().replace(b"\r\n", b"\n")
        if not same:
            stale.append(rel)
            if not a.check:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dst)
    # fichiers de migration retirés côté frontend
    keep = {CONTRACT / r for r in src}
    for p in (CONTRACT / "migrations").glob("*.sql") if (CONTRACT / "migrations").exists() else []:
        if p not in keep:
            stale.append(f"{p.name} (obsolète)")
            if not a.check:
                p.unlink()
    if a.check:
        if stale:
            print("Contrat de schéma PÉRIMÉ — relancer sync_contract.py :\n  " + "\n  ".join(stale))
            return 1
        print("Contrat de schéma à jour.")
        return 0
    print("Synchronisé :", ", ".join(stale) if stale else "rien à faire")
    return 0


if __name__ == "__main__":
    sys.exit(main())
