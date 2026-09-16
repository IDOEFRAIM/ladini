#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/smoke_observability.sh — vérifications RAPIDES de la couche
# observabilité (< ~20 s). Complète scripts/smoke.sh (qui couvre santé
# applicative/DB/Redis/Celery) SANS le dupliquer — ce script-ci ne regarde
# QUE : /metrics expose du texte Prometheus réel, Alloy (s'il est présent
# sur ce node) tourne et écoute son port OTLP, et — best-effort — que les
# données arrivent bien jusqu'à Grafana Cloud.
#
# Sortie ≠ 0 → à traiter comme un avertissement d'observabilité, PAS un
# incident applicatif (§ "RÈGLE D'OR" de telemetry.py : l'observabilité ne
# doit jamais casser l'application — ce script ne doit donc jamais être
# câblé comme gate de rollback dans deploy.sh, contrairement à smoke.sh).
#
#   ./scripts/smoke_observability.sh
#
# (2026-09-16, chantier Hetzner scale-out) — même garde par rôle que
# scripts/smoke.sh : SMOKE_CHECK_APP (api/metrics) est actif par défaut ; sur
# un node qui ne porte pas le rôle `app` (voir infra/inventory.yml), exporter
# SMOKE_CHECK_APP=0 avant d'appeler ce script pour éviter un faux échec.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

API="${SMOKE_API_URL:-http://127.0.0.1:8000}"
SMOKE_CHECK_APP="${SMOKE_CHECK_APP:-1}"
ALLOY_COMPOSE_FILE="${LADINI_ROOT}/infra/alloy/docker-compose.alloy.yml"
# Port OTLP gRPC standard Alloy/Collector — voir infra/alloy/*.river une fois
# ce chantier livré ; 4317 est la convention OTLP officielle, gardée ici en
# repli si le fichier n'est pas encore présent pour l'inspecter.
ALLOY_OTLP_PORT="${ALLOY_OTLP_PORT:-4317}"
FAIL=0
pass() { log "  ✓ $1"; }
ko() { err "  ✗ $1"; FAIL=1; }
skip() { log "  · $1 — SAUTÉ"; }

log "Smoke observabilité → ${API}"

# ── 1. /metrics expose du VRAI texte Prometheus (pas vide, pas une 404) ────
if [ "$SMOKE_CHECK_APP" = 1 ]; then
  mbody="$(curl -s --max-time 5 "${API}/metrics" || true)"
  if [ -z "$mbody" ]; then
    ko "/metrics vide ou injoignable"
  elif printf '%s' "$mbody" | grep -q '^# prometheus_client indisponible'; then
    ko "/metrics répond mais prometheus_client est absent côté API (voir telemetry.py::prometheus_asgi_response)"
  elif printf '%s' "$mbody" | grep -qE '^(# HELP|# TYPE) ladini_'; then
    n_series="$(printf '%s' "$mbody" | grep -cE '^ladini_' || true)"
    pass "/metrics expose des métriques ladini_* (${n_series} lignes de séries)"
  else
    ko "/metrics répond mais ne contient aucune métrique ladini_* reconnue (PROMETHEUS_ENABLED=false ?)"
  fi
else
  skip "/metrics (SMOKE_CHECK_APP=0)"
fi

# ── 2. Alloy — TOLÉRANT : un autre chantier peut ne pas encore avoir livré
#    infra/alloy/. Ne jamais faire échouer ce script pour une absence de
#    fichier qui n'est peut-être simplement pas encore du ressort de cette
#    machine/cette étape du projet.
if [ -f "$ALLOY_COMPOSE_FILE" ]; then
  if docker compose -f "$ALLOY_COMPOSE_FILE" ps --status running 2>/dev/null | grep -q alloy; then
    pass "conteneur Alloy en cours d'exécution (${ALLOY_COMPOSE_FILE})"
    # Port OTLP en écoute LOCALEMENT — Alloy tourne sur l'hôte (pas dans
    # agri_net), donc un simple TCP connect sur 127.0.0.1 suffit, pas
    # besoin de dc exec.
    if (exec 3<>"/dev/tcp/127.0.0.1/${ALLOY_OTLP_PORT}") 2>/dev/null; then
      exec 3>&- 3<&-
      pass "port OTLP Alloy (127.0.0.1:${ALLOY_OTLP_PORT}) en écoute"
    else
      ko "Alloy tourne mais le port OTLP 127.0.0.1:${ALLOY_OTLP_PORT} ne répond pas"
    fi
  else
    ko "${ALLOY_COMPOSE_FILE} présent mais aucun conteneur Alloy actif (docker compose -f ... up -d ?)"
  fi
else
  skip "Alloy (${ALLOY_COMPOSE_FILE} absent — chantier observabilité pas encore livré sur ce node, ou rôle sans Alloy)"
fi

# ── 3. Grafana Cloud — best-effort, s'arrête proprement si pas d'identifiants ─
# Ne fait JAMAIS échouer le script (FAIL reste inchangé dans cette section) :
# c'est une vérification de confort, pas une garantie — voir la docstring en
# tête de fichier sur le statut "avertissement, pas incident" de ce script.
GRAFANA_CLOUD_API_KEY="${GRAFANA_CLOUD_API_KEY:-}"
GRAFANA_CLOUD_PROM_URL="${GRAFANA_CLOUD_PROM_URL:-}"  # ex: https://prometheus-prod-XX.grafana.net/api/prom
if [ -n "$GRAFANA_CLOUD_API_KEY" ] && [ -n "$GRAFANA_CLOUD_PROM_URL" ]; then
  # Requête minimale : dernière valeur de ladini_webhooks_received_total tous
  # labels confondus, sur les 5 dernières minutes — juste "y a-t-il une série
  # récente", pas une analyse de valeur.
  q='ladini_webhooks_received_total'
  gcode="$(curl -s -o /tmp/grafana_check.$$ -w '%{http_code}' --max-time 8 \
    -H "Authorization: Bearer ${GRAFANA_CLOUD_API_KEY}" \
    -G --data-urlencode "query=${q}" \
    "${GRAFANA_CLOUD_PROM_URL%/}/api/v1/query" 2>/dev/null || echo 000)"
  gbody="$(cat /tmp/grafana_check.$$ 2>/dev/null || true)"
  rm -f /tmp/grafana_check.$$ 2>/dev/null || true
  if [ "$gcode" = 200 ] && printf '%s' "$gbody" | grep -q '"result":\['; then
    if printf '%s' "$gbody" | grep -qE '"result":\[\s*\]'; then
      warn "  Grafana Cloud joignable mais AUCUNE série '${q}' trouvée — Alloy pousse-t-il vraiment ?"
    else
      pass "Grafana Cloud : série '${q}' trouvée (données arrivent bien)"
    fi
  else
    warn "  GRAFANA CHECK NOT RUN — reason = requête API échouée (HTTP ${gcode})"
  fi
else
  log "  GRAFANA CHECK NOT RUN — reason = credentials unavailable (GRAFANA_CLOUD_API_KEY/GRAFANA_CLOUD_PROM_URL non définis)"
fi

echo
if [ "$FAIL" -ne 0 ]; then err "SMOKE OBSERVABILITÉ KO"; exit 1; fi
log "SMOKE OBSERVABILITÉ OK"
