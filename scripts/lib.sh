#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/lib.sh — helpers PARTAGÉS par preflight.sh / deploy.sh / rollback.sh
# / smoke.sh. Ne s'exécute pas seul : `source "$(dirname "$0")/lib.sh"`.
#
# Provider-neutral : aucun appel à doctl / aws / hcloud. Juste docker,
# docker compose, curl, flock, coreutils.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail

# ── Chemins ────────────────────────────────────────────────────────
LADINI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${LADINI_ROOT}/docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-${LADINI_ROOT}/.env}"
RELEASES_DIR="${RELEASES_DIR:-${LADINI_ROOT}/deploy/releases}"
LOCK_FILE="${LOCK_FILE:-${LADINI_ROOT}/deploy/.deploy.lock}"
CURRENT_FILE="${RELEASES_DIR}/current"
PREVIOUS_FILE="${RELEASES_DIR}/previous"
HISTORY_FILE="${RELEASES_DIR}/history.log"

# ── Registry / namespace (surchargeable → ECR, Docker Hub…) ─────────
REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:-idoefraim/agriconnect}"
APP_SERVICES=(api worker mcp)          # les 3 images versionnées
ALL_SERVICES=(mcp api worker beat redis pgbouncer flower autoheal)

# ── Journalisation ────────────────────────────────────────────────
_c() { printf '\033[%sm' "$1"; }
log()  { printf '%s[deploy]%s %s\n'  "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%s[deploy][warn]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
err()  { printf '%s[deploy][ERROR]%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; }
die()  { err "$*"; exit 1; }

# ── Verrou de déploiement (§36) — un seul deploy/rollback à la fois ─
# Implémenté avec `mkdir` (atomique sur tout POSIX, y compris quand `flock`
# n'est pas installé — util-linux vs. busybox vs. Cygwin). `flock` est utilisé
# EN PLUS s'il est présent (protège contre un kill -9 laissant un lockdir).
LOCK_DIR="${LOCK_FILE%.lock}.lockdir"
_LOCK_HELD=0
acquire_lock() {
  mkdir -p "$(dirname "$LOCK_DIR")"
  # Verrou périmé ? (process mort) → on le récupère.
  if [ -d "$LOCK_DIR" ] && [ -f "$LOCK_DIR/pid" ]; then
    local old_pid; old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo 0)"
    if [ -n "$old_pid" ] && ! kill -0 "$old_pid" 2>/dev/null; then
      warn "Verrou périmé (pid $old_pid mort) — récupération."
      rm -rf "$LOCK_DIR"
    fi
  fi
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    local holder; holder="$(cat "$LOCK_DIR/info" 2>/dev/null || true)"
    die "Un autre déploiement est en cours (${LOCK_DIR}${holder:+ — $holder}). Réessayez plus tard."
  fi
  _LOCK_HELD=1
  echo "$$" >"$LOCK_DIR/pid"
  printf 'pid=%s user=%s started=%s\n' "$$" "${USER:-?}" "$(date -u +%FT%TZ)" >"$LOCK_DIR/info"
}
release_lock() { [ "$_LOCK_HELD" = 1 ] && rm -rf "$LOCK_DIR" 2>/dev/null || true; }
trap 'release_lock' EXIT

# ── docker compose : toujours avec le bon fichier + .env ───────────
dc() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# ── Image refs ────────────────────────────────────────────────────
image_ref() { printf '%s/%s/ladini-%s:%s' "$REGISTRY" "$IMAGE_NAMESPACE" "$1" "${2:?release}"; }

# ── Manifeste de release ──────────────────────────────────────────
# Écrit un fichier env `key=value` (release courante ou précédente).
write_release_file() {
  local target="$1" version="$2" git_sha="${3:-}" built_at="${4:-}"
  mkdir -p "$RELEASES_DIR"
  cat >"$target" <<EOF
RELEASE_VERSION=${version}
GIT_SHA=${git_sha}
BUILD_TIMESTAMP=${built_at}
DEPLOYED_AT=$(date -u +%FT%TZ)
DEPLOYED_BY=${USER:-?}
EOF
}
read_release_field() {  # read_release_field <file> <KEY>
  [ -f "$1" ] || return 1
  awk -F= -v k="$2" '$1==k {sub(/^[^=]*=/,""); print; found=1} END{exit !found}' "$1"
}
current_release() { read_release_field "$CURRENT_FILE" RELEASE_VERSION 2>/dev/null || true; }
previous_release() { read_release_field "$PREVIOUS_FILE" RELEASE_VERSION 2>/dev/null || true; }

history_append() {  # history_append <event> <release> <detail>
  mkdir -p "$RELEASES_DIR"
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%FT%TZ)" "${USER:-?}" "$1" "$2" "${3:-}" >>"$HISTORY_FILE"
}

# ── Métadonnées d'une image déjà tirée (labels OCI = source de vérité) ─
image_label() {  # image_label <service> <release> <label>
  docker image inspect "$(image_ref "$1" "$2")" \
    --format "{{ index .Config.Labels \"$3\" }}" 2>/dev/null || true
}

# ── Attente de santé bornée ──────────────────────────────────────
# wait_healthy_container <service> <timeout_s> : le conteneur passe healthy
# selon Docker (healthcheck LIVENESS du compose).
wait_healthy_container() {
  local svc="$1" timeout="${2:-120}" waited=0 cid state
  while :; do
    cid="$(dc ps -q "$svc" 2>/dev/null | head -n1)"
    if [ -n "$cid" ]; then
      state="$(docker inspect -f '{{ .State.Health.Status }}{{ if not .State.Health }}nohealth{{ end }}' "$cid" 2>/dev/null || echo missing)"
      case "$state" in
        healthy) return 0 ;;
        nohealth) return 0 ;;  # pas de healthcheck défini → on ne bloque pas
      esac
    fi
    (( waited += 3 )); [ "$waited" -ge "$timeout" ] && return 1
    sleep 3
  done
}

# wait_http <url> <timeout_s> [expected_code] : poll un endpoint jusqu'à 2xx
# (ou le code attendu). Utilisé pour la READINESS réelle post-`up`.
wait_http() {
  local url="$1" timeout="${2:-90}" expect="${3:-}" waited=0 code
  while :; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || echo 000)"
    if [ -n "$expect" ]; then
      [ "$code" = "$expect" ] && return 0
    else
      [[ "$code" =~ ^2 ]] && return 0
    fi
    (( waited += 3 )); [ "$waited" -ge "$timeout" ] && { err "  $url → HTTP $code (attendu ${expect:-2xx}) après ${timeout}s"; return 1; }
    sleep 3
  done
}

# ── Migrations : classer une release ────────────────────────────
# ROLLBACK_SAFE si aucune migration DB dans le diff, ou seulement des
# migrations EXPAND. Sinon MIGRATION_REQUIRES_MANUAL_RECOVERY.
# (Best-effort : sans Alembic configuré, renvoie ROLLBACK_SAFE + warn.)
migration_class_between() {  # <from_sha> <to_sha>  → echo ROLLBACK_SAFE | MIGRATION_REQUIRES_MANUAL_RECOVERY
  local from="$1" to="$2"
  if [ ! -d "${LADINI_ROOT}/backend/alembic" ] && [ ! -f "${LADINI_ROOT}/backend/alembic.ini" ]; then
    echo "ROLLBACK_SAFE"; return 0
  fi
  "${LADINI_ROOT}/scripts/check_migrations.sh" "$from" "$to" --classify 2>/dev/null || echo "MIGRATION_REQUIRES_MANUAL_RECOVERY"
}
