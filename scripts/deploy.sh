#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/deploy.sh — déploiement d'une RELEASE IMMUABLE, single-VPS.
#
#   ./scripts/deploy.sh <release>          # ex: ./scripts/deploy.sh sha-a83f6c1
#   AUTO_ROLLBACK=0 ./scripts/deploy.sh …  # désactive le rollback auto
#
# (2026-09-16, chantier Hetzner scale-out) — WRAPPER FIN autour de
# `scripts/node_deploy.sh --single-node`. La logique de déploiement réelle
# (préflight → pull → migration → up → santé → smoke → enregistrement,
# rollback applicatif auto) vit maintenant dans node_deploy.sh, généralisée
# pour accepter des RÔLES (app/scheduler/admin — voir les `profiles:` de
# docker-compose.prod.yml). `--single-node` = les 3 rôles à la fois +
# migrations incluses = EXACTEMENT le comportement historique de ce script.
#
# Ce fichier reste séparé (plutôt que de simplement documenter
# "utilisez node_deploy.sh") pour ne RIEN casser chez un opérateur qui a
# `./scripts/deploy.sh <release>` dans un runbook, un alias shell, ou une
# CI existante (voir .github/workflows/deploy.yml) — zéro changement
# requis côté appelant. Pour un déploiement multi-node piloté par rôle,
# voir scripts/node_deploy.sh et scripts/cluster_deploy.sh.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "${HERE}/node_deploy.sh" "${1:-}" --single-node
