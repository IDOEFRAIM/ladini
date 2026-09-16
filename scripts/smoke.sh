#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/smoke.sh — vérifications RAPIDES post-déploiement (< ~20 s).
#
# N'exécute PAS la suite pytest. Vérifie que les fonctions critiques
# répondent, SANS action mutante/destructive. Sortie ≠ 0 → deploy.sh /
# node_deploy.sh considère le déploiement en échec et rollback.
#
#   ./scripts/smoke.sh
#   EXPECT_RELEASE=sha-a83f6c1 ./scripts/smoke.sh   # vérifie aussi /version
#
# (2026-09-16, chantier Hetzner scale-out) — GARDES PAR RÔLE : depuis que
# docker-compose.prod.yml sépare app/scheduler/admin par `profiles:`, un
# node donné n'a plus forcément TOUS les conteneurs (api/mcp/worker/beat/
# flower) en local — un node "scheduler" seul n'a ni api ni worker, par
# exemple. Faire tourner les sections qui en dépendent produirait de FAUX
# échecs. `scripts/node_deploy.sh` exporte SMOKE_CHECK_APP/SCHEDULER/ADMIN
# selon les rôles réellement présents sur CE node ; par défaut (script
# lancé seul, comme avant) tout est actif = comportement historique
# inchangé pour le mode single-node.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

API="${SMOKE_API_URL:-http://127.0.0.1:8000}"
EXPECT_RELEASE="${EXPECT_RELEASE:-}"
SMOKE_CHECK_APP="${SMOKE_CHECK_APP:-1}"             # api/mcp/worker + webhook
SMOKE_CHECK_SCHEDULER="${SMOKE_CHECK_SCHEDULER:-1}" # beat
SMOKE_CHECK_ADMIN="${SMOKE_CHECK_ADMIN:-0}"         # flower (nouveau, off par défaut :
                                                     # absent du smoke historique, n'active
                                                     # que si explicitement demandé)
FAIL=0
pass() { log "  ✓ $1"; }
ko() { err "  ✗ $1"; FAIL=1; }
skip() { log "  · $1 — SAUTÉ (rôle absent sur ce node)"; }

log "Smoke tests → ${API} (app=${SMOKE_CHECK_APP} scheduler=${SMOKE_CHECK_SCHEDULER} admin=${SMOKE_CHECK_ADMIN})"

if [ "$SMOKE_CHECK_APP" = 1 ]; then
  # 1. API vivante
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${API}/health/live" || echo 000)"
  [ "$code" = 200 ] && pass "/health/live = 200" || ko "/health/live = $code"

  # 2. /version répond et matche la release attendue.
  #    Tolérant aux espaces JSON (`json.dumps` met `"release": "..."`).
  vjson="$(curl -s --max-time 5 "${API}/version" || true)"
  vrel="$(printf '%s' "$vjson" | tr -d ' \n\t' | sed -n 's/.*"release":"\([^"]*\)".*/\1/p')"
  if [ -n "$vrel" ]; then
    pass "/version répond (release=${vrel})"
    if [ -n "$EXPECT_RELEASE" ]; then
      [ "$vrel" = "$EXPECT_RELEASE" ] \
        && pass "/version.release == ${EXPECT_RELEASE}" \
        || ko "/version.release = '${vrel}' ≠ ${EXPECT_RELEASE} (réponse: $vjson)"
    fi
  else
    ko "/version ne renvoie pas de champ 'release' (réponse: $vjson)"
  fi

  # 3. Readiness réelle : DB + Redis vus par l'API
  rjson="$(curl -s -w '\n%{http_code}' --max-time 8 "${API}/health/ready" || true)"
  rcode="$(echo "$rjson" | tail -n1)"
  rbody="$(echo "$rjson" | sed '$d')"
  if [ "$rcode" = 200 ]; then
    pass "/health/ready = 200"
    echo "$rbody" | grep -q '"database":[ ]*"ok"' && pass "  DB: ok"    || ko "  DB pas 'ok' dans /health/ready"
    echo "$rbody" | grep -q '"redis":[ ]*"ok"'    && pass "  Redis: ok" || ko "  Redis pas 'ok' dans /health/ready"
  else
    ko "/health/ready = $rcode ($rbody)"
  fi

  # 4. MCP daemon : /health interne + authentification effective
  if dc exec -T mcp curl -fsS --max-time 5 http://localhost:8003/health >/dev/null 2>&1; then
    pass "MCP /health (interne) OK"
  else
    ko "MCP /health interne KO"
  fi
  # le port MCP ne doit PAS être joignable sans le token depuis l'hôte
  if curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:8003/mcp" 2>/dev/null | grep -qE '^(000|401|404)$'; then
    pass "MCP non exposé/authentifié sur l'hôte"
  else
    warn "  MCP semble joignable sur 127.0.0.1:8003 — vérifier qu'aucun port n'est publié"
  fi

  # 5. Celery worker répond au broker (lecture seule) — worker est du rôle
  #    "app" (voir docker-compose.prod.yml : profiles: ["app"]), pas un rôle
  #    à part.
  if dc exec -T worker celery -A ladini.api.celery_app inspect ping --timeout 8 >/dev/null 2>&1; then
    pass "Celery worker répond (inspect ping)"
  else
    ko "Celery worker ne répond pas au broker"
  fi

  # 7. Webhook joignable (SANS payload — on veut juste que la route existe).
  #    GET sur le webhook Twilio → 405 (méthode non autorisée) = la route est là.
  wcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${API}/api/webhook/twilio" || echo 000)"
  case "$wcode" in
    405|403|422) pass "webhook /api/webhook/twilio présent (HTTP $wcode)" ;;
    *)           ko  "webhook /api/webhook/twilio → HTTP $wcode inattendu" ;;
  esac
else
  skip "API/MCP/worker/webhook (SMOKE_CHECK_APP=0)"
fi

if [ "$SMOKE_CHECK_SCHEDULER" = 1 ]; then
  # 6. Beat tourne — sonde le PROCESSUS (pgrep), pas le pidfile.
  #
  # (2026-09-16, validation E2E locale) : `--pidfile=/tmp/celerybeat.pid`
  # n'écrit JAMAIS ce fichier dans ce conteneur — EXACT même incident déjà
  # documenté et corrigé côté `docker-compose.prod.yml` (service `beat`,
  # commentaire "incident réel 2026-09-15") : le process est bien vivant
  # (confirmé par `pgrep` en E2E) mais le test `-f .../celerybeat.pid`
  # échoue systématiquement, faisant croire à `smoke.sh` que Beat est mort.
  # `node_deploy.sh`/`cluster_deploy.sh` rollback automatiquement sur un
  # échec smoke — ce faux négatif aurait annulé CHAQUE déploiement portant
  # le rôle scheduler, même quand Beat tourne parfaitement. Le fix
  # `docker-compose.prod.yml` (healthcheck du service) n'avait jamais été
  # répercuté ici, où le même bug pidfile subsistait.
  if dc exec -T beat sh -c "pgrep -f 'celery.*beat' >/dev/null" >/dev/null 2>&1; then
    pass "Celery beat actif"
  else
    ko "Celery beat inactif"
  fi
else
  skip "Celery beat (SMOKE_CHECK_SCHEDULER=0)"
fi

if [ "$SMOKE_CHECK_ADMIN" = 1 ]; then
  # 8. Flower répond (200 ou 401 = serveur up, l'auth n'est pas le sujet ici)
  fcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:5555/" || echo 000)"
  case "$fcode" in
    200|401) pass "Flower répond (HTTP $fcode)" ;;
    *)       ko  "Flower → HTTP $fcode inattendu" ;;
  esac
fi

echo
if [ "$FAIL" -ne 0 ]; then err "SMOKE TESTS KO"; exit 1; fi
log "SMOKE TESTS OK"
