#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/rollback.sh — revenir à une release APPLICATIVE connue.
#
#   ./scripts/rollback.sh                  # → deploy/releases/previous
#   ./scripts/rollback.sh sha-b921ea7      # → release explicite
#
# ⚠️  ROLLBACK APPLICATIF UNIQUEMENT (§43). Ce script ne touche JAMAIS la
#     base de données. Si la release qu'on quitte a appliqué une migration
#     destructive (DROP/RENAME/type incompatible), revenir à l'ancien CODE
#     ne suffit PAS — voir la sortie du script et docs/runbooks/incident.md
#     → "migration failed". Le script détecte ce cas et le signale.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

acquire_lock

CURRENT_RELEASE="$(current_release || true)"
TARGET_RELEASE="${1:-$(previous_release || true)}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

[ -n "$TARGET_RELEASE" ] || die "Aucune release précédente enregistrée (deploy/releases/previous) et aucun argument fourni. Usage: $0 [release]"
[ "$TARGET_RELEASE" != "$CURRENT_RELEASE" ] || die "La release ${TARGET_RELEASE} est déjà la release courante. Rien à faire."

log "Rollback  ${CURRENT_RELEASE:-<inconnue>}  →  ${TARGET_RELEASE}"

# ── Avertissement migration (best-effort) ────────────────────────
if [ -f "${LADINI_ROOT}/backend/alembic.ini" ] && [ -n "$CURRENT_RELEASE" ]; then
  CUR_SHA="$(read_release_field "$CURRENT_FILE" GIT_SHA 2>/dev/null || echo HEAD)"
  TGT_SHA="$( { [ -f "$PREVIOUS_FILE" ] && read_release_field "$PREVIOUS_FILE" GIT_SHA; } 2>/dev/null || echo "$TARGET_RELEASE")"
  CLASS="$(migration_class_between "$TGT_SHA" "$CUR_SHA" || echo MIGRATION_REQUIRES_MANUAL_RECOVERY)"
  if [ "$CLASS" = "MIGRATION_REQUIRES_MANUAL_RECOVERY" ]; then
    warn "════════════════════════════════════════════════════════════════"
    warn " La release ${CURRENT_RELEASE} contient une migration DB NON"
    warn " réversible automatiquement. Revenir au code ${TARGET_RELEASE}"
    warn " NE restaurera PAS le schéma attendu par ce code."
    warn " → suivez docs/runbooks/database-restore.md AVANT de continuer,"
    warn "   ou confirmez que la migration était EXPAND-only."
    warn "════════════════════════════════════════════════════════════════"
    if [ "${ROLLBACK_FORCE:-0}" != "1" ]; then
      die "Rollback interrompu. Relancez avec ROLLBACK_FORCE=1 si vous savez que la DB est compatible."
    fi
  fi
fi

export RELEASE_VERSION="$TARGET_RELEASE"
export COMPOSE_VERSION="compose-$(sha1sum "$COMPOSE_FILE" | cut -c1-12)"
export GIT_SHA="$( { [ -f "$PREVIOUS_FILE" ] && read_release_field "$PREVIOUS_FILE" GIT_SHA; } 2>/dev/null || echo "$TARGET_RELEASE")"
export BUILD_TIMESTAMP="$( { [ -f "$PREVIOUS_FILE" ] && read_release_field "$PREVIOUS_FILE" BUILD_TIMESTAMP; } 2>/dev/null || echo unknown)"

# ── 1. Pull de l'ancienne release ───────────────────────────────
log "1/4 · pull ${TARGET_RELEASE}…"
dc pull "${APP_SERVICES[@]}" || die "Impossible de tirer ${TARGET_RELEASE} — le registry a-t-il encore cette image ? (politique de rétention : current + previous, voir deploy.sh::prune)"

# métadonnées affinées
_gs="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.revision)"; [ -n "$_gs" ] && [ "$_gs" != unknown ] && export GIT_SHA="$_gs"
_ba="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.created)"; [ -n "$_ba" ] && [ "$_ba" != unknown ] && export BUILD_TIMESTAMP="$_ba"

# ── 2. Bascule (PAS de migration — jamais en rollback) ──────────
log "2/4 · bascule des services…"
dc up -d --remove-orphans || die "docker compose up a échoué pendant le rollback — état à vérifier d'urgence (docker compose ps)"

# ── 3. Santé + smoke ───────────────────────────────────────────
log "3/4 · attente santé…"
for svc in mcp api worker beat redis pgbouncer; do
  wait_healthy_container "$svc" "$HEALTH_TIMEOUT" || die "Rollback : '$svc' n'est pas passé healthy — INCIDENT (docs/runbooks/incident.md)"
done
wait_http "http://127.0.0.1:8000/health/ready" "$HEALTH_TIMEOUT" 200 || die "Rollback : /health/ready ≠ 200 — INCIDENT"

log "4/4 · smoke tests…"
EXPECT_RELEASE="$TARGET_RELEASE" "${HERE}/smoke.sh" || die "Rollback : smoke KO — INCIDENT (la release cible elle-même est peut-être mauvaise)"

# ── 4. Réécriture du manifeste ─────────────────────────────────
# La release vers laquelle on revient devient 'current'. L'ancienne 'current'
# (celle qu'on quitte) devient 'previous' → on peut re-avancer si besoin.
if [ -n "$CURRENT_RELEASE" ]; then
  write_release_file "$PREVIOUS_FILE" "$CURRENT_RELEASE" \
    "$(read_release_field "$CURRENT_FILE" GIT_SHA 2>/dev/null || true)" \
    "$(read_release_field "$CURRENT_FILE" BUILD_TIMESTAMP 2>/dev/null || true)"
fi
write_release_file "$CURRENT_FILE" "$TARGET_RELEASE" "$GIT_SHA" "$BUILD_TIMESTAMP"
history_append "rollback-success" "$TARGET_RELEASE" "from=${CURRENT_RELEASE:-none}"

cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  ROLLBACK SUCCESS
  Now running : ${TARGET_RELEASE}
  Was        : ${CURRENT_RELEASE:-<inconnue>}
  Health     : OK   (/health/ready = 200)
  Smoke      : OK
  Note       : APP rollback only — la base de données n'a pas été modifiée.
╚══════════════════════════════════════════════════════════════════╝
EOF
dc ps
