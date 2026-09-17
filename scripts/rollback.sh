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

# ═════════════════════════════════════════════════════════════════════
# §BUG CORRIGÉ ICI (2026-09-18, audit lock cluster/node) — `docker-compose.
# prod.yml` porte un `profiles:` par service (app/scheduler/admin) depuis le
# chantier Hetzner scale-out (voir node_deploy.sh) ; SANS `--profile`, `dc up`
# ne démarre AUCUN service profilé (seul `autoheal`, qui n'en a pas). Ce
# script appelait `dc up -d` nu depuis avant ce refactor et n'avait jamais
# été mis à jour — un rollback réel aurait ramené le stack à ZÉRO service
# applicatif (api/worker/mcp/beat/flower down), rattrapé seulement a
# posteriori par l'échec des checks de santé (§3/4 plus bas), jamais
# silencieusement — mais un vrai INCIDENT le temps de le détecter.
#
# Fix : lit `ROLES` depuis le manifeste de la release COURANTE (celle qu'on
# QUITTE — c'est SES rôles qui étaient actifs sur CE node, donc les rôles à
# rallumer avec l'ancien code). `write_release_file` (lib.sh) écrit ce champ
# depuis node_deploy.sh (chemin deploy.sh ET cluster_deploy.sh) depuis ce
# même correctif. Rétro-compat : un manifeste écrit AVANT ce champ (ou vide)
# retombe sur les 3 rôles — comportement historique de ce script, jamais
# moins permissif qu'avant.
ROLES_CSV="$(read_release_field "$CURRENT_FILE" ROLES 2>/dev/null || true)"
[ -n "$ROLES_CSV" ] || ROLES_CSV="app,scheduler,admin"
IFS=',' read -r -a _ROLLBACK_ROLES <<<"$ROLES_CSV"
_has_rollback_role() { local want="$1" r; for r in "${_ROLLBACK_ROLES[@]}"; do [ "$r" = "$want" ] && return 0; done; return 1; }
PROFILE_ARGS=()
for r in "${_ROLLBACK_ROLES[@]}"; do
  PROFILE_ARGS+=(--profile "$r")
done

# Services de santé + drapeaux smoke — MÊME dérivation que node_deploy.sh
# (voir son commentaire "Services concernés par les rôles demandés") : ce
# node peut ne PAS porter tous les rôles (cluster). §BUG CORRIGÉ ICI en même
# temps que --profile ci-dessus : l'ancienne liste figée `mcp api worker beat
# redis pgbouncer` attendait un service `redis` qui N'EXISTE PLUS dans
# docker-compose.prod.yml (Redis externe obligatoire depuis le chantier
# Hetzner scale-out, voir preflight.sh) — `wait_healthy_container redis`
# attendait donc un conteneur qui ne serait JAMAIS créé, garantissant un
# timeout de ${HEALTH_TIMEOUT}s puis un échec systématique, sur CHAQUE
# rollback, quel que soit l'état réel du stack.
HEALTH_SERVICES=()
SMOKE_CHECK_APP=0
SMOKE_CHECK_SCHEDULER=0
SMOKE_CHECK_ADMIN=0
if _has_rollback_role app; then
  HEALTH_SERVICES+=(mcp api worker pgbouncer)
  SMOKE_CHECK_APP=1
fi
if _has_rollback_role scheduler; then
  HEALTH_SERVICES+=(beat)
  SMOKE_CHECK_SCHEDULER=1
fi
if _has_rollback_role admin; then
  HEALTH_SERVICES+=(flower)
  SMOKE_CHECK_ADMIN=1
fi

log "Rollback  ${CURRENT_RELEASE:-<inconnue>}  →  ${TARGET_RELEASE}  (rôles: ${ROLES_CSV})"

# ── Avertissement migration (best-effort) ────────────────────────
# §BUG CORRIGÉ ICI (2026-09-16, follow-up pre-Hetzner, même classe que
# node_deploy.sh/cluster_deploy.sh) : `CUR_SHA` retombait sur le littéral
# "HEAD" si `$CURRENT_FILE` n'avait pas (ou plus) de champ GIT_SHA —
# `migration_class_between` aurait alors diffé contre le HEAD local de LA
# MACHINE QUI EXÉCUTE LE SCRIPT, jamais garanti de correspondre à la release
# réellement en service. Sans SHA fiable, on ne devine JAMAIS — on force la
# classification la plus prudente (MIGRATION_REQUIRES_MANUAL_RECOVERY),
# cohérent avec le principe déjà affirmé ailleurs : "ne jamais promettre un
# rollback automatique quand il est faux".
if [ -f "${LADINI_ROOT}/backend/alembic.ini" ] && [ -n "$CURRENT_RELEASE" ]; then
  CUR_SHA="$(read_release_field "$CURRENT_FILE" GIT_SHA 2>/dev/null || true)"
  TGT_SHA="$( { [ -f "$PREVIOUS_FILE" ] && read_release_field "$PREVIOUS_FILE" GIT_SHA; } 2>/dev/null || true)"
  if [ -n "$CUR_SHA" ] && [ -n "$TGT_SHA" ]; then
    CLASS="$(migration_class_between "$TGT_SHA" "$CUR_SHA" || echo MIGRATION_REQUIRES_MANUAL_RECOVERY)"
  else
    warn "   GIT_SHA manquant dans le manifeste de release (current ou previous) — classification impossible avec confiance."
    CLASS="MIGRATION_REQUIRES_MANUAL_RECOVERY"
  fi
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
log "2/4 · bascule des services (rôles: ${ROLES_CSV})…"
dc "${PROFILE_ARGS[@]}" up -d --remove-orphans || die "docker compose up a échoué pendant le rollback — état à vérifier d'urgence (docker compose ps)"

# ── 3. Santé + smoke ───────────────────────────────────────────
log "3/4 · attente santé…"
for svc in "${HEALTH_SERVICES[@]}"; do
  wait_healthy_container "$svc" "$HEALTH_TIMEOUT" || die "Rollback : '$svc' n'est pas passé healthy — INCIDENT (docs/runbooks/incident.md)"
done
if _has_rollback_role app; then
  wait_http "http://127.0.0.1:8000/health/ready" "$HEALTH_TIMEOUT" 200 || die "Rollback : /health/ready ≠ 200 — INCIDENT"
fi

log "4/4 · smoke tests…"
EXPECT_RELEASE="$TARGET_RELEASE" \
  SMOKE_CHECK_APP="$SMOKE_CHECK_APP" \
  SMOKE_CHECK_SCHEDULER="$SMOKE_CHECK_SCHEDULER" \
  SMOKE_CHECK_ADMIN="$SMOKE_CHECK_ADMIN" \
  "${HERE}/smoke.sh" || die "Rollback : smoke KO — INCIDENT (la release cible elle-même est peut-être mauvaise)"

# ── 4. Réécriture du manifeste ─────────────────────────────────
# La release vers laquelle on revient devient 'current'. L'ancienne 'current'
# (celle qu'on quitte) devient 'previous' → on peut re-avancer si besoin.
if [ -n "$CURRENT_RELEASE" ]; then
  write_release_file "$PREVIOUS_FILE" "$CURRENT_RELEASE" \
    "$(read_release_field "$CURRENT_FILE" GIT_SHA 2>/dev/null || true)" \
    "$(read_release_field "$CURRENT_FILE" BUILD_TIMESTAMP 2>/dev/null || true)" \
    "$ROLES_CSV"
fi
write_release_file "$CURRENT_FILE" "$TARGET_RELEASE" "$GIT_SHA" "$BUILD_TIMESTAMP" "$ROLES_CSV"
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
