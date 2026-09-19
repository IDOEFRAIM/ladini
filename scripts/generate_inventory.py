#!/usr/bin/env python3
"""scripts/generate_inventory.py — produit infra/inventory.yml depuis les
outputs Terraform (chantier Hetzner scale-out, audit 2026-09-19).

Élimine l'étape manuelle « Terraform crée un node → un humain récupère son
IP → un humain édite infra/inventory.yml à la main » (source d'erreurs :
faute de frappe sur une IP, oubli de retirer `scheduler` d'un node qui ne
devrait plus le porter, etc.). Les RÔLES par node (dont le fait qu'un SEUL
node porte `scheduler`) sont déjà décidés côté Terraform
(`infra/providers/hetzner/servers.tf::local.app_node_roles` — la clé "1"
porte scheduler+admin sauf si `scheduler_on_dedicated_node = true`) : ce
script ne fait que RETRANSCRIRE cette décision, il n'en invente aucune.

Usage :
    python scripts/generate_inventory.py
        [--terraform-dir infra/providers/hetzner]
        [--var-file environments/production.tfvars]
        [--output infra/inventory.yml]
        [--dry-run]

Ne lance JAMAIS `terraform apply`/`plan` — lit uniquement `terraform output
-json`, qui ne fait AUCUN appel réseau (les outputs sont déjà dans le state
local suite au dernier `apply` réussi). Si `infra/inventory.yml` existant a
été édité à la main (rôles ajustés manuellement pour une raison
opérationnelle), ce script écrase ces changements — c'est le contrat :
toute décision de rôle doit vivre côté Terraform, jamais dans un fichier
généré (voir la contrainte scheduler singleton, scripts/validate_inventory.py).

Exit 0 si l'inventaire généré est valide, 1 sinon (message explicite sur
stderr — jamais un fichier invalide écrit silencieusement).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_inventory  # noqa: E402  (après sys.path, volontaire)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


class GenerationError(RuntimeError):
    pass


def run_terraform_output(terraform_dir: Path, var_file: Path | None) -> dict:
    """`terraform output -json` — lecture SEULE du state local, aucun appel
    réseau (contrairement à `plan`/`apply`, qui rafraîchissent depuis l'API)."""
    cmd = ["terraform", "output", "-json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=terraform_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GenerationError(
            "`terraform` introuvable dans le PATH — installez-le ou lancez "
            "ce script depuis un environnement qui l'a déjà (voir README de "
            f"{terraform_dir})."
        ) from exc

    if proc.returncode != 0:
        raise GenerationError(
            f"`terraform output -json` a échoué (code {proc.returncode}) dans "
            f"{terraform_dir} :\n{proc.stderr.strip()}\n"
            "→ avez-vous lancé `terraform apply` au moins une fois dans ce "
            "répertoire ? (var_file n'est pas nécessaire pour `output`, "
            "seulement pour `plan`/`apply`.)"
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GenerationError(
            f"sortie de `terraform output -json` illisible (JSON invalide) :\n{proc.stdout[:500]}"
        ) from exc


def _output_value(outputs: dict, name: str, *, required: bool = True):
    entry = outputs.get(name)
    if entry is None:
        # `terraform output -json` lit le state TEL QU'ÉCRIT PAR LE DERNIER
        # `apply` — si `outputs.tf` a gagné un output depuis (ex: ce
        # chantier lui-même), la clé est simplement ABSENTE tant qu'aucun
        # `apply` n'a encore tourné avec la nouvelle définition. Pour un
        # output optionnel (`scheduler_node`, `null` la plupart du temps),
        # c'est un état NORMAL, pas une erreur — seul un output REQUIS
        # (`app_nodes`) doit faire échouer la génération.
        if required:
            raise GenerationError(
                f"output Terraform requis '{name}' absent — infra/providers/hetzner/outputs.tf "
                "a-t-il changé sans qu'un `terraform apply` récent ait suivi ?"
            )
        return None
    return entry.get("value")


def build_nodes(outputs: dict) -> list[dict]:
    app_nodes = _output_value(outputs, "app_nodes")
    if not app_nodes:
        raise GenerationError(
            "output 'app_nodes' vide — aucun node app dans le state Terraform "
            "(app_node_count == 0 ? ce n'est normalement pas possible, "
            "variables.tf l'interdit)."
        )

    nodes: list[dict] = []
    for n in app_nodes:
        host = n.get("private_ip")
        if not host:
            raise GenerationError(
                f"node '{n.get('name')}' : private_ip absente/null dans l'output "
                "Terraform — le réseau privé n'a probablement pas fini de "
                "s'attribuer une IP (attendez quelques secondes après "
                "`terraform apply` et relancez ce script) ou l'`apply` est "
                "incomplet. AUCUN fichier écrit tant que ce n'est pas résolu — "
                "un inventaire avec un host manquant casserait le SSH de "
                "scripts/cluster_deploy.sh silencieusement plus tard."
            )
        roles = n.get("roles") or []
        if not roles:
            raise GenerationError(f"node '{n.get('name')}' : aucun rôle dans l'output Terraform.")
        nodes.append({"name": n["name"], "host": host, "roles": list(roles)})

    scheduler_node = _output_value(outputs, "scheduler_node", required=False)
    if scheduler_node:
        host = scheduler_node.get("private_ip")
        if not host:
            raise GenerationError(
                "node scheduler dédié : private_ip absente/null — mêmes causes "
                "possibles que pour un node app (voir ci-dessus)."
            )
        nodes.append(
            {
                "name": scheduler_node["name"],
                "host": host,
                "roles": list(scheduler_node.get("roles") or []),
            }
        )

    return nodes


def render_yaml(nodes: list[dict], *, terraform_dir: Path) -> str:
    # Format volontairement restreint (une ligne par champ, `roles:` en
    # liste flow) — c'est EXACTEMENT le sous-ensemble YAML que
    # scripts/validate_inventory.py et scripts/cluster_deploy.sh savent
    # parser SANS dépendance PyYAML (voir leurs docstrings respectives).
    # Ne PAS enrichir ce rendu (commentaires par node, styles YAML
    # alternatifs…) sans mettre à jour ces deux parseurs en même temps.
    lines = [
        "# ═════════════════════════════════════════════════════════════════",
        "# infra/inventory.yml — GÉNÉRÉ AUTOMATIQUEMENT, NE PAS ÉDITER À LA MAIN.",
        "#",
        f"# Produit par scripts/generate_inventory.py depuis "
        f"`terraform output -json` ({terraform_dir.as_posix()}).",
        "# Toute édition manuelle sera écrasée au prochain lancement de ce",
        "# script. Pour changer un rôle, changez la configuration Terraform",
        "# (servers.tf / variables.tf) et régénérez — jamais ce fichier",
        "# directement, voir scripts/validate_inventory.py pour la contrainte",
        "# dure (exactement un node 'scheduler' dans tout l'inventaire).",
        "# ═════════════════════════════════════════════════════════════════",
        "nodes:",
    ]
    for n in nodes:
        roles_csv = ", ".join(n["roles"])
        lines.append(f"  - name: {n['name']}")
        lines.append(f"    host: {n['host']}")
        lines.append(f"    roles: [{roles_csv}]")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--terraform-dir",
        default=str(REPO_ROOT / "infra" / "providers" / "hetzner"),
        help="Répertoire du module Terraform Hetzner (défaut : infra/providers/hetzner).",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "infra" / "inventory.yml"),
        help="Fichier à écrire (défaut : infra/inventory.yml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche le YAML généré sur stdout sans écrire le fichier.",
    )
    args = parser.parse_args()

    terraform_dir = Path(args.terraform_dir).resolve()
    output_path = Path(args.output).resolve()

    if not terraform_dir.is_dir():
        print(f"[generate_inventory] répertoire Terraform introuvable : {terraform_dir}", file=sys.stderr)
        return 1

    try:
        outputs = run_terraform_output(terraform_dir, None)
        nodes = build_nodes(outputs)
        rendered = render_yaml(nodes, terraform_dir=terraform_dir)
    except GenerationError as exc:
        print(f"[generate_inventory] ÉCHEC : {exc}", file=sys.stderr)
        return 1

    # Auto-validation AVANT écriture — un inventaire généré mais invalide ne
    # doit jamais atteindre le disque silencieusement (même garantie que
    # cluster_deploy.sh étape 1, appliquée ici en amont).
    parsed = validate_inventory.parse_nodes(rendered)
    errors = validate_inventory.validate(parsed)
    if errors:
        print("[generate_inventory] ÉCHEC : l'inventaire généré est INVALIDE :", file=sys.stderr)
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        print(
            "  → ceci indiquerait un bug de servers.tf::local.app_node_roles "
            "(ex: scheduler_on_dedicated_node mal configuré) — RIEN n'a été écrit.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(rendered)
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8", newline="\n")

    role_summary = ", ".join(f"{n['name']}={n['roles']}" for n in nodes)
    print(f"[generate_inventory] ✓ {output_path} écrit — {len(nodes)} node(s) : {role_summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
