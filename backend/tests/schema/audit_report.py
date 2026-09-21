"""CLI : génère la matrice de divergence Drizzle / SQLAlchemy / PostgreSQL.

    python tests/schema/audit_report.py --snapshot <drizzle/meta/NNNN_snapshot.json> \
        --pg "host=localhost port=5432 user=... password=... dbname=..." --out docs/schema/MATRIX.md
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))
warnings.filterwarnings("ignore")

import schema_model as m  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--pg", required=True, help="DSN libpq (base reconstruite depuis les migrations)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="Matrice de divergence de schéma")
    a = ap.parse_args()
    import psycopg2

    from ladini.domain.orm_base import Base
    import ladini.domain.models  # noqa: F401  (enregistre tous les modèles)

    for extra in ("ladini.services.memory.episodic_memory", "ladini.services.memory.user_profile"):
        try:  # modèles hors domain/ (supprimés par le nettoyage ; tolérés ici pour la baseline)
            __import__(extra)
        except ImportError:
            pass

    D = m.load_drizzle(a.snapshot)
    S = m.load_sqlalchemy(Base.metadata)
    P = {k: v for k, v in m.load_postgres(psycopg2.connect(a.pg)).items() if k[1] != "__drizzle_migrations"}
    rows, ok = m.build_matrix(D, S, P)
    Path(a.out).write_text(m.matrix_markdown(rows, ok, a.title), encoding="utf-8")
    print(f"{len(rows)} divergences, {ok} éléments identiques → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
