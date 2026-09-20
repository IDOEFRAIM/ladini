#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/check_celery_broker_state.sh — état du broker Celery ACTUEL
# (avant/après bascule Upstash → Valkey, 2026-09-20). Affiche :
#   - active / reserved / scheduled (control bus Celery, `inspect`)
#   - longueur de chaque file (LLEN sur le broker Redis/Valkey, exécuté
#     DANS le conteneur worker — jamais besoin d'exposer REDIS_URL en
#     dehors du conteneur, jamais affiché)
#
# Usage :
#   bash scripts/check_celery_broker_state.sh                  # service "worker", app par défaut
#   bash scripts/check_celery_broker_state.sh --service worker --app ladini.api.celery_app
#
# N'affiche JAMAIS REDIS_URL ni un mot de passe — la longueur de file est
# lue DEPUIS le conteneur worker (qui a déjà REDIS_URL dans son propre
# environnement), le script hôte ne le manipule jamais lui-même.
#
# Sortie : 0 si au moins le control bus Celery a répondu (broker
# joignable) ; non-zéro si le broker est totalement injoignable — utile
# comme gate scriptable (ex: "queues=0 avant bascule worker", §14 du
# runbook de migration).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

SERVICE="worker"
APP_MODULE="ladini.api.celery_app"
QUEUES=(interactive background scheduled celery)

while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE="${2:?}"; shift 2 ;;
    --app) APP_MODULE="${2:?}"; shift 2 ;;
    --queue) QUEUES+=("${2:?}"); shift 2 ;;
    *) echo "Option inconnue : $1" >&2; exit 1 ;;
  esac
done

CONTROL_BUS_OK=0

echo "═══ Celery inspect active (tâches en cours d'exécution) ═══"
if dc exec -T "$SERVICE" celery -A "$APP_MODULE" inspect active --timeout 8 2>&1; then
  CONTROL_BUS_OK=1
else
  echo "  (échec — broker injoignable ou aucun worker connecté)"
fi
echo

echo "═══ Celery inspect reserved (tâches préfetchées, pas encore démarrées) ═══"
dc exec -T "$SERVICE" celery -A "$APP_MODULE" inspect reserved --timeout 8 2>&1 || echo "  (échec)"
echo

echo "═══ Celery inspect scheduled (ETA/countdown en attente) ═══"
dc exec -T "$SERVICE" celery -A "$APP_MODULE" inspect scheduled --timeout 8 2>&1 || echo "  (échec)"
echo

echo "═══ Longueur des files (LLEN broker, exécuté dans le conteneur ${SERVICE}) ═══"
# Python exécuté DANS le conteneur worker : REDIS_URL vient de SON propre
# environnement (docker-compose.prod.yml x-full-app-env), jamais lu ni
# affiché par ce script hôte. `redis.from_url` est déjà une dépendance du
# projet (core/idempotency.py, etc.) — aucune nouvelle dépendance.
QUEUES_PY_LIST="$(printf "'%s'," "${QUEUES[@]}")"
PY_SNIPPET="
import redis
from ladini.core.settings import settings
r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=3)
for q in [${QUEUES_PY_LIST}]:
    try:
        print(f'  {q}: {r.llen(q)}')
    except Exception as exc:
        print(f'  {q}: ERREUR ({type(exc).__name__})')
"
if dc exec -T "$SERVICE" python -c "$PY_SNIPPET" 2>&1; then
  :
else
  echo "  (échec — impossible de lire les longueurs de file, broker injoignable depuis le conteneur ${SERVICE})"
fi
echo

echo "═══ Résumé ═══"
if [ "$CONTROL_BUS_OK" -eq 1 ]; then
  echo "OK — control bus Celery a répondu (broker joignable depuis ${SERVICE})."
  exit 0
else
  echo "ÉCHEC — control bus Celery injoignable (broker down, ou aucun worker connecté à ce broker)."
  exit 1
fi
