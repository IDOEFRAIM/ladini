#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/deploy.sh — déploiement d'une RELEASE IMMUABLE.
#
#   ./scripts/deploy.sh <release>          # ex: ./scripts/deploy.sh sha-a83f6c1
#   AUTO_ROLLBACK=0 ./scripts/deploy.sh …  # désactive le rollback auto
#
# Ne CONSTRUIT rien (build-once). Ne fait PAS `git pull`. Séquence :
#   lock → preflight → snapshot release courante → pull → migration →
#   up -d → attente santé → smoke → enregistrement release.
#
# Échec AVANT `up` : la prod actuelle continue (rien n'a bougé).
# Échec APRÈS `up` (santé/smoke KO) : rollback APPLICATIF auto vers la
#   release précédente si elle existe (jamais de rollback DB — §43).
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

TARGET_RELEASE="${1:-}"
AUTO_ROLLBACK="${AUTO_ROLLBACK:-1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
ROLLBACK_HEALTH_TIMEOUT="${ROLLBACK_HEALTH_TIMEOUT:-90}"

[ -n "$TARGET_RELEASE" ] || die "usage: $0 <release>  (ex: sha-a83f6c1)"

# ── Verrou (un seul deploy/rollback à la fois) ────────────────────
acquire_lock

CURRENT_RELEASE="$(current_release || true)"
STAGE="init"
FAILED_REASON=""

fail() {  # fail <stage> <reason>
  STAGE="$1"; FAILED_REASON="$2"
  err "ÉCHEC à l'étape « $STAGE » : $FAILED_REASON"

  if [ "${STAGE_REACHED_UP:-0}" = "1" ] && [ "$AUTO_ROLLBACK" = "1" ] && [ -n "$CURRENT_RELEASE" ] && [ "$CURRENT_RELEASE" != "$TARGET_RELEASE" ]; then
    warn "Rollback APPLICATIF automatique → ${CURRENT_RELEASE} (la DB n'est PAS touchée)."
    if RELEASE_VERSION="$CURRENT_RELEASE" dc pull "${APP_SERVICES[@]}" >/dev/null 2>&1 \
       && RELEASE_VERSION="$CURRENT_RELEASE" dc up -d --remove-orphans >/dev/null 2>&1 \
       && wait_http "http://127.0.0.1:8000/health/ready" "$ROLLBACK_HEALTH_TIMEOUT" 200 >/dev/null 2>&1; then
      warn "Rollback applicatif OK — la prod tourne de nouveau sur ${CURRENT_RELEASE}."
      history_append "deploy-failed-autorollback" "$TARGET_RELEASE" "→ $CURRENT_RELEASE ; stage=$STAGE ; $FAILED_REASON"
    else
      err "Rollback automatique INCERTAIN — intervention manuelle requise."
      history_append "deploy-failed-rollback-uncertain" "$TARGET_RELEASE" "stage=$STAGE ; $FAILED_REASON"
    fi
  else
    history_append "deploy-failed" "$TARGET_RELEASE" "stage=$STAGE ; $FAILED_REASON"
  fi

  cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════════╗
  DEPLOYMENT FAILED
  Stage           : ${STAGE}
  Reason          : ${FAILED_REASON}
  Target release  : ${TARGET_RELEASE}
  Current good    : ${CURRENT_RELEASE:-<aucune enregistrée>}
  Rollback command: ./scripts/rollback.sh ${CURRENT_RELEASE:-<release-precedente>}
╚══════════════════════════════════════════════════════════════════╝
EOF
  exit 1
}
trap 'fail "${STAGE}" "commande inattendue (ligne $LINENO)"' ERR

# ── 1. Préflight (échoue sans rien toucher) ───────────────────────
STAGE="preflight"
log "1/7 · préflight…"
"${HERE}/preflight.sh" "$TARGET_RELEASE" || fail "preflight" "préconditions non satisfaites (voir ci-dessus)"

# ── 2. Résolution des métadonnées (avant tout changement) ─────────
STAGE="resolve-metadata"
export RELEASE_VERSION="$TARGET_RELEASE"
export COMPOSE_VERSION="compose-$(sha1sum "$COMPOSE_FILE" | cut -c1-12)"
# valeurs provisoires ; affinées après le pull via les labels OCI de l'image
export GIT_SHA="${GIT_SHA:-$TARGET_RELEASE}"
export BUILD_TIMESTAMP="${BUILD_TIMESTAMP:-unknown}"

# ── 3. Pull (atomique : si ça casse, rien n'a changé) ─────────────
STAGE="pull"
log "2/7 · pull des images ${TARGET_RELEASE}…"
dc pull "${APP_SERVICES[@]}" redis pgbouncer autoheal \
  || fail "pull" "impossible de tirer les images ${TARGET_RELEASE} — la prod actuelle continue"

# métadonnées affinées depuis l'image tirée (source de vérité)
_gs="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.revision)"
_ba="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.created)"
[ -n "$_gs" ] && [ "$_gs" != "unknown" ] && export GIT_SHA="$_gs"
[ -n "$_ba" ] && [ "$_ba" != "unknown" ] && export BUILD_TIMESTAMP="$_ba"

# ── 4. Migrations DB — stratégie sûre (offline, AVANT la bascule) ──
STAGE="migrate"
log "3/7 · migrations base de données…"
RELEASE_VERSION="$TARGET_RELEASE" dc up -d --wait --wait-timeout 60 pgbouncer \
  || fail "migrate" "PgBouncer non healthy — DB injoignable (DB_HOST/USER/PASSWORD/NAME ?)"

MIG_CLASS="ROLLBACK_SAFE"
if [ -f "${LADINI_ROOT}/backend/alembic.ini" ]; then
  if [ -n "$CURRENT_RELEASE" ]; then
    MIG_CLASS="$(migration_class_between "${GIT_SHA:-HEAD~1}" HEAD || echo MIGRATION_REQUIRES_MANUAL_RECOVERY)"
  fi
  log "   classification migration : ${MIG_CLASS}"
  RELEASE_VERSION="$TARGET_RELEASE" dc run --rm --no-deps -w /app/backend \
    api alembic upgrade head \
    || fail "migrate" "alembic upgrade head a échoué — bascule annulée, la prod tourne toujours sur l'ancien code"
  log "   migrations appliquées."
else
  warn "   backend/alembic.ini absent → migrations Alembic SAUTÉES (schéma géré hors-Alembic : SCHEMA_COLUMN_DDL + ensure_performance_indexes au démarrage worker)."
fi

# ── 5. Bascule applicative ────────────────────────────────────────
STAGE="up"
log "4/7 · bascule des services (${TARGET_RELEASE})…"
STAGE_REACHED_UP=1
RELEASE_VERSION="$TARGET_RELEASE" dc up -d --remove-orphans \
  || fail "up" "docker compose up a échoué"

# ── 6. Attente de la SANTÉ RÉELLE (pas juste 'up') ───────────────
STAGE="health"
log "5/7 · attente santé (timeout ${HEALTH_TIMEOUT}s)…"
for svc in mcp api worker beat redis pgbouncer; do
  wait_healthy_container "$svc" "$HEALTH_TIMEOUT" \
    || fail "health" "conteneur '$svc' n'est jamais passé healthy"
  log "   ✓ $svc healthy"
done
# readiness applicative réelle (DB + Redis vus par l'API)
wait_http "http://127.0.0.1:8000/health/ready" "$HEALTH_TIMEOUT" 200 \
  || fail "health" "/health/ready ≠ 200 (DB ou Redis non joignables depuis l'API)"
log "   ✓ /health/ready = 200"

# ── 7. Smoke tests ───────────────────────────────────────────────
STAGE="smoke"
log "6/7 · smoke tests…"
EXPECT_RELEASE="$TARGET_RELEASE" "${HERE}/smoke.sh" \
  || fail "smoke" "smoke tests KO"

# ── 8. Enregistrement de la release réussie ──────────────────────
STAGE="record"
log "7/7 · enregistrement de la release…"
if [ -n "$CURRENT_RELEASE" ] && [ "$CURRENT_RELEASE" != "$TARGET_RELEASE" ]; then
  cp -f "$CURRENT_FILE" "$PREVIOUS_FILE"
fi
write_release_file "$CURRENT_FILE" "$TARGET_RELEASE" "$GIT_SHA" "$BUILD_TIMESTAMP"
history_append "deploy-success" "$TARGET_RELEASE" "prev=${CURRENT_RELEASE:-none} ; mig=${MIG_CLASS}"
trap - ERR

cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  DEPLOYMENT SUCCESS
  Release   : ${TARGET_RELEASE}
  Previous  : ${CURRENT_RELEASE:-<aucune>}
  Git SHA   : ${GIT_SHA}
  Built at  : ${BUILD_TIMESTAMP}
  Migration : ${MIG_CLASS}
  Health    : OK   (/health/ready = 200)
  Smoke     : OK
  Rollback  : ./scripts/rollback.sh            # → ${CURRENT_RELEASE:-<none>}
╚══════════════════════════════════════════════════════════════════╝
EOF
dc ps
