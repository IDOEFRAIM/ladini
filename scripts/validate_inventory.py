#!/usr/bin/env python3
"""scripts/validate_inventory.py — garde l'inventaire de déploiement cohérent
(chantier Hetzner scale-out, §9/§44).

Vérifie, sans dépendance externe (stdlib seule — pas de PyYAML requis en CI
minimale) :
  1. chaque `roles:` ne contient que des valeurs connues (app/scheduler/admin) ;
  2. au moins un node porte `app` (sinon rien ne sert jamais de trafic) ;
  3. EXACTEMENT un node, dans tout l'inventaire, porte `scheduler` — jamais
     zéro (aucun cron planifié), jamais deux+ (double-exécution des tâches
     planifiées, voir infra/inventory.example.yml pour le detail du risque) ;
  4. les noms de node sont uniques.

Usage :
    python scripts/validate_inventory.py [infra/inventory.yml]
    (défaut : infra/inventory.yml ; utile en CI : infra/inventory.example.yml)

Exit 0 si valide, 1 sinon (message explicite sur stderr).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows console par défaut (cp1252) ne sait pas encoder ✓/✗ — force UTF-8
# sur stdout/stderr plutôt que de renoncer aux symboles (lisibilité CI/terminal
# Linux inchangée, où c'est déjà UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_VALID_ROLES = {"app", "scheduler", "admin"}

# Parseur minimal, volontairement TRÈS restreint au sous-ensemble YAML utilisé
# par infra/inventory.example.yml — jamais un parseur YAML général (évite une
# dépendance PyYAML pour un script de garde qui doit rester exécutable
# n'importe où, y compris sur un runner CI minimal). Si l'inventaire réel
# devient plus riche, migrer vers PyYAML explicitement plutôt que d'étendre
# ce mini-parseur.
_NODE_NAME_RE = re.compile(r"^\s*-\s*name:\s*(\S+)")
_ROLES_RE = re.compile(r"^\s*roles:\s*\[([^\]]*)\]")


def parse_nodes(text: str) -> list[dict]:
    nodes: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        m = _NODE_NAME_RE.match(line)
        if m:
            if current is not None:
                nodes.append(current)
            current = {"name": m.group(1), "roles": []}
            continue
        m = _ROLES_RE.match(line)
        if m and current is not None:
            current["roles"] = [r.strip() for r in m.group(1).split(",") if r.strip()]
    if current is not None:
        nodes.append(current)
    return nodes


def validate(nodes: list[dict]) -> list[str]:
    errors: list[str] = []
    if not nodes:
        errors.append("aucun node trouvé dans l'inventaire.")
        return errors

    names = [n["name"] for n in nodes]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        errors.append(f"noms de node dupliqués : {sorted(dupes)}")

    scheduler_nodes = []
    app_nodes = []
    for n in nodes:
        unknown = set(n["roles"]) - _VALID_ROLES
        if unknown:
            errors.append(f"node '{n['name']}' : rôle(s) inconnu(s) {sorted(unknown)} (valides: {sorted(_VALID_ROLES)})")
        if "scheduler" in n["roles"]:
            scheduler_nodes.append(n["name"])
        if "app" in n["roles"]:
            app_nodes.append(n["name"])

    if not app_nodes:
        errors.append("aucun node ne porte le rôle 'app' — rien ne servirait jamais de trafic.")

    if len(scheduler_nodes) == 0:
        errors.append(
            "aucun node ne porte le rôle 'scheduler' — Celery Beat ne tournerait nulle part "
            "(les tâches planifiées ne se déclencheraient jamais)."
        )
    elif len(scheduler_nodes) > 1:
        errors.append(
            f"PLUSIEURS nodes portent le rôle 'scheduler' ({scheduler_nodes}) — "
            "Beat DOIT être un singleton (§9) : deux schedulers actifs déclenchent chaque "
            "tâche planifiée deux fois (double notification, double réconciliation…). "
            "Retirez 'scheduler' de tous ces nodes sauf un."
        )

    return errors


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "infra/inventory.yml")
    if not path.exists():
        print(f"[validate_inventory] {path} introuvable.", file=sys.stderr)
        print(
            "  Copiez infra/inventory.example.yml vers infra/inventory.yml et adaptez-le.",
            file=sys.stderr,
        )
        return 1

    nodes = parse_nodes(path.read_text(encoding="utf-8"))
    errors = validate(nodes)

    if errors:
        print(f"[validate_inventory] {path} INVALIDE :", file=sys.stderr)
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        return 1

    role_summary = ", ".join(f"{n['name']}={n['roles']}" for n in nodes)
    print(f"[validate_inventory] ✓ {path} valide — {len(nodes)} node(s) : {role_summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
