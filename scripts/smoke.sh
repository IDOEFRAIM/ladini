#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/smoke.sh — vérifications RAPIDES post-déploiement (< ~20 s).
#
# N'exécute PAS la suite pytest. Vérifie que les fonctions critiques
# répondent, SANS action mutante/destructive. Sortie ≠ 0 → deploy.sh
# considère le déploiement en échec et rollback.
#
#   ./scripts/smoke.sh
#   EXPECT_RELEASE=sha-a83f6c1 ./scripts/smoke.sh   # vérifie aussi /version
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

API="${SMOKE_API_URL:-http://127.0.0.1:8000}"
EXPECT_RELEASE="${EXPECT_RELEASE:-}"
FAIL=0
pass() { log "  ✓ $1"; }
f=()   ; ko() { err "  ✗ $1"; FAIL=1; }

log "Smoke tests → ${API}"

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

# 5. Celery worker répond au broker (lecture seule)
if dc exec -T worker celery -A ladini.api.celery_app inspect ping --timeout 8 >/dev/null 2>&1; then
  pass "Celery worker répond (inspect ping)"
else
  ko "Celery worker ne répond pas au broker"
fi

# 6. Beat tourne (le pid existe et le process vit)
if dc exec -T beat sh -c 'test -f /tmp/celerybeat.pid && kill -0 "$(cat /tmp/celerybeat.pid)"' >/dev/null 2>&1; then
  pass "Celery beat actif"
else
  ko "Celery beat inactif"
fi

# 7. Webhook joignable (SANS payload — on veut juste que la route existe).
#    GET sur le webhook Twilio → 405 (méthode non autorisée) = la route est là.
wcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${API}/api/webhook/twilio" || echo 000)"
case "$wcode" in
  405|403|422) pass "webhook /api/webhook/twilio présent (HTTP $wcode)" ;;
  *)           ko  "webhook /api/webhook/twilio → HTTP $wcode inattendu" ;;
esac

echo
if [ "$FAIL" -ne 0 ]; then err "SMOKE TESTS KO"; exit 1; fi
log "SMOKE TESTS OK"
