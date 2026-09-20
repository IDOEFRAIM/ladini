#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/celery_maintenance_mode.sh — bascule instantanée du mode
# maintenance/drain des producteurs Celery (2026-09-20, cutover Upstash →
# Valkey). Voir backend/src/ladini/core/maintenance.py pour le mécanisme
# (fichier drapeau dans le conteneur api, vérifié par chaque webhook AVANT
# de publier une tâche).
#
#   bash scripts/celery_maintenance_mode.sh on       # active la pause (503 sur les webhooks producteurs)
#   bash scripts/celery_maintenance_mode.sh off       # désactive
#   bash scripts/celery_maintenance_mode.sh status    # affiche l'état actuel
#
# Touche/retire un fichier DANS le conteneur `api` déjà en cours
# d'exécution (`docker compose exec`) — AUCUN redémarrage de conteneur,
# effet immédiat sur la PROCHAINE requête entrante. C'est ce qui permet une
# fenêtre de maintenance de 1-3 minutes plutôt qu'un cycle complet de
# recréation de conteneur.
#
# Limite connue (Phase 1, un seul node app — voir infra/inventory.example.yml) :
# ce script agit sur LE conteneur `api` local. En scale-out (plusieurs
# nodes `app`), il doit être exécuté sur CHAQUE node qui porte le service
# `api` — pas encore automatisé en multi-node (YAGNI tant qu'un seul node
# suffit, même discipline que scripts/cluster_deploy.sh).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

SERVICE="api"
FLAG_PATH="${MAINTENANCE_FLAG_PATH:-/tmp/ladini_celery_maintenance}"
ACTION="${1:-}"

case "$ACTION" in
  on)
    if dc exec -T "$SERVICE" touch "$FLAG_PATH"; then
      echo "✓ Mode maintenance ACTIVÉ — les webhooks producteurs (Twilio/WhatsApp/Paydunya/market.py) renvoient désormais 503 (retry côté fournisseur)."
      echo "  Rappel : Beat doit être stoppé séparément (docker compose --profile scheduler stop beat) — ce script ne gère QUE la pause des producteurs API."
    else
      echo "✗ Échec — le conteneur '${SERVICE}' est-il démarré ?" >&2
      exit 1
    fi
    ;;
  off)
    if dc exec -T "$SERVICE" rm -f "$FLAG_PATH"; then
      echo "✓ Mode maintenance DÉSACTIVÉ — trafic métier normal rétabli."
    else
      echo "✗ Échec — le conteneur '${SERVICE}' est-il démarré ?" >&2
      exit 1
    fi
    ;;
  status)
    if dc exec -T "$SERVICE" test -f "$FLAG_PATH" 2>/dev/null; then
      echo "MAINTENANCE ACTIVE (${FLAG_PATH} présent dans le conteneur ${SERVICE})"
      exit 0
    else
      echo "normal (pas de maintenance active)"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {on|off|status}" >&2
    exit 1
    ;;
esac
