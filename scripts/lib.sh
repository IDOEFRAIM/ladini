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
CLUSTER_LOCK_FILE="${CLUSTER_LOCK_FILE:-${LADINI_ROOT}/deploy/.cluster-deploy.lock}"
CURRENT_FILE="${RELEASES_DIR}/current"
PREVIOUS_FILE="${RELEASES_DIR}/previous"
HISTORY_FILE="${RELEASES_DIR}/history.log"

# ── Registry / namespace (surchargeable → ECR, Docker Hub…) ─────────
REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:-idoefraim/ladini}"
APP_SERVICES=(api worker mcp)          # les 3 images versionnées
ALL_SERVICES=(mcp api worker beat redis pgbouncer flower autoheal)

# ── Journalisation ────────────────────────────────────────────────
_c() { printf '\033[%sm' "$1"; }
log()  { printf '%s[deploy]%s %s\n'  "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%s[deploy][warn]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
err()  { printf '%s[deploy][ERROR]%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; }
die()  { err "$*"; exit 1; }

# ── Verrous de déploiement (§36, revu 2026-09-18 — voir §DEADLOCK plus bas)
# ─────────────────────────────────────────────────────────────────────
# Deux verrous DISTINCTS, deux RESSOURCES DISTINCTES (chemins différents) :
#
#   LOCK_DIR         (deploy/.deploy.lockdir)  — verrou LOCAL PAR NODE. Tenu
#     par deploy.sh / node_deploy.sh / rollback.sh (via acquire_lock, ci-
#     dessous) pendant TOUTE mutation du compose stack/du manifeste de
#     release SUR CE NODE — peu importe QUI a lancé le script (un humain en
#     SSH direct, ou node_deploy.sh piloté à distance par cluster_deploy.sh).
#     Protège : deux déploiements/rollbacks concurrents sur LE MÊME node.
#
#   CLUSTER_LOCK_DIR (deploy/.cluster-deploy.lockdir) — repli LOCAL du
#     verrou CLUSTER-WIDE (voir acquire_cluster_lock plus bas, mode "redis"
#     préféré). Tenu par cluster_deploy.sh/cluster_rollback.sh pendant TOUTE
#     la durée d'une ORCHESTRATION (rolling deploy/rollback sur plusieurs
#     nodes). Protège : deux orchestrations cluster concurrentes.
#
# §DEADLOCK CORRIGÉ ICI (2026-09-18, incident réel premier rollout Hetzner,
# self-hosted runner installé DIRECTEMENT SUR le node qu'il déploie) — AVANT
# ce correctif, les deux verrous ci-dessus partageaient LE MÊME chemin
# (`LOCK_DIR`) dès que le verrou cluster dégradait en local (Redis
# injoignable/REDIS_URL absent CÔTÉ ORCHESTRATEUR). Séquence observée :
#   1. cluster_deploy.sh (orchestrateur) prend le verrou LOCAL cluster
#      → crée deploy/.deploy.lockdir, PID = celui de cluster_deploy.sh.
#   2. cluster_deploy.sh SSH vers ladini-app-1 — qui EST la machine sur
#      laquelle il tourne déjà (runner self-hosted co-localisé).
#   3. node_deploy.sh démarre (nouveau process SSH) et appelle acquire_lock()
#      → tente de créer LE MÊME deploy/.deploy.lockdir → `mkdir` échoue
#      (déjà tenu par le PID de sa PROPRE session orchestratrice parente)
#      → "Un autre déploiement est en cours (pid=<celui de
#      cluster_deploy.sh>)" → le rollout s'arrête. AUTO-DEADLOCK confirmé.
# Le vrai bug n'était donc PAS "il faut que node_deploy.sh sache qu'il est
# piloté par un orchestrateur pour ne pas verrouiller" (ça retirerait une
# protection légitime : un humain qui lance deploy.sh en direct sur ce node
# PENDANT le rollout doit toujours être bloqué). Le vrai bug était que les
# DEUX verrous, censés protéger des choses DIFFÉRENTES, partageaient PAR
# ACCIDENT la même ressource dans le seul cas où l'orchestrateur est
# co-localisé avec un node. Fix : deux chemins, toujours distincts, jamais
# de collision possible — orchestrateur et node peuvent tourner sur la même
# machine sans jamais se marcher dessus.
#
# Implémentation : `mkdir` (atomique sur tout POSIX, y compris sans `flock` —
# util-linux vs. busybox vs. Cygwin), avec récupération de verrou PÉRIMÉ (pid
# mort) — factorisée UNE SEULE FOIS ici (acquire_named_lock) et réutilisée
# par les deux verrous ci-dessus, pour ne jamais laisser les deux
# implémentations diverger.
declare -a _HELD_LOCKS=()

_lock_holder_info() { [ -f "$1/info" ] && cat "$1/info" 2>/dev/null || true; }

acquire_named_lock() {  # acquire_named_lock <lockdir_path> <label humain>
  local dir="$1" label="${2:-déploiement}"
  mkdir -p "$(dirname "$dir")"
  # Verrou périmé ? (process mort) → on le récupère.
  if [ -d "$dir" ] && [ -f "$dir/pid" ]; then
    local old_pid; old_pid="$(cat "$dir/pid" 2>/dev/null || echo 0)"
    if [ -n "$old_pid" ] && ! kill -0 "$old_pid" 2>/dev/null; then
      warn "Verrou périmé (${label}, pid $old_pid mort) — récupération."
      rm -rf "$dir"
    fi
  fi
  if ! mkdir "$dir" 2>/dev/null; then
    local holder; holder="$(_lock_holder_info "$dir")"
    die "Un autre ${label} est en cours (${dir}${holder:+ — $holder}). Réessayez plus tard."
  fi
  _HELD_LOCKS+=("$dir")
  echo "$$" >"$dir/pid"
  printf 'pid=%s user=%s started=%s label=%s\n' "$$" "${USER:-?}" "$(date -u +%FT%TZ)" "$label" >"$dir/info"
}
release_named_lock() {  # release_named_lock <lockdir_path>
  local dir="$1" i
  rm -rf "$dir" 2>/dev/null || true
  for i in "${!_HELD_LOCKS[@]}"; do
    [ "${_HELD_LOCKS[$i]}" = "$dir" ] && unset '_HELD_LOCKS[i]'
  done
}
release_all_locks() {  # trap EXIT — filet de sécurité, relâche TOUT verrou encore tenu
  local dir
  for dir in "${_HELD_LOCKS[@]:-}"; do
    [ -n "$dir" ] && rm -rf "$dir" 2>/dev/null || true
  done
  _HELD_LOCKS=()
}
trap 'release_all_locks' EXIT

# Verrou LOCAL PAR NODE — API historique inchangée (deploy.sh, node_deploy.sh,
# rollback.sh, preflight.sh l'utilisent tel quel, sans connaître son
# implémentation interne).
LOCK_DIR="${LOCK_FILE%.lock}.lockdir"
acquire_lock() { acquire_named_lock "$LOCK_DIR" "déploiement (node)"; }
release_lock() { release_named_lock "$LOCK_DIR"; }

# Verrou CLUSTER-WIDE — repli LOCAL, ressource TOUJOURS DISTINCTE de LOCK_DIR
# (voir §DEADLOCK ci-dessus). Chemin propre, jamais partagé avec le verrou
# par node.
CLUSTER_LOCK_DIR="${CLUSTER_LOCK_FILE%.lock}.lockdir"

# ── Verrou CLUSTER-WIDE — implémentation partagée par cluster_deploy.sh ET
# cluster_rollback.sh (factorisée ici pour ne PAS dupliquer cette logique
# dans deux fichiers qui divergeraient inévitablement avec le temps — c'est
# exactement ce genre de duplication qui a caché le bug de deadlock ci-dessus
# assez longtemps dans un seul des deux scripts).
#
# Choix : verrou Redis (SET NX EX) quand redis-cli + REDIS_URL sont
# disponibles. Redis EXTERNE est déjà OBLIGATOIRE pour tout le cluster
# (docker-compose.prod.yml, REDIS_URL sans défaut, voir preflight.sh) — ce
# n'est donc PAS une nouvelle dépendance ajoutée pour ce seul verrou, juste
# la réutilisation d'une ressource déjà garantie partagée. SET NX EX est
# atomique côté serveur Redis : pas de leader election, juste un mutex
# classique avec expiration de sécurité (si le process qui tient le verrou
# meurt sans le relâcher — kill -9, OOM — le verrou expire tout seul après
# CLUSTER_LOCK_TTL plutôt que de bloquer indéfiniment tout déploiement futur,
# symétrique à la récupération de verrou périmé d'acquire_named_lock).
#
# (2026-09-18) Résolution de REDIS_URL, si pas déjà exportée par l'appelant :
# lue depuis $ENV_FILE (le .env applicatif du node, celui-là même que le
# compose stack utilise) plutôt que déclarée "indisponible ICI" par défaut.
# Sur le runner self-hosted actuel (installé DIRECTEMENT sur ladini-app-1,
# working-directory = $LADINI_ROOT), ce .env existe et contient déjà
# REDIS_URL — c'est le cas RÉEL de production, pas un repli dégradé. Ce
# repli en LOCAL (ci-dessous) redevient donc l'exception (poste dev sans
# redis-cli, ou .env absent), plus la norme. La valeur n'est JAMAIS logguée
# (ni ici, ni dans un message d'erreur) — seul le NOM de la clé Redis et le
# `holder` (hostname-pid-timestamp, jamais le secret) apparaissent en sortie.
_resolve_redis_url_from_env_file() {
  [ -n "${REDIS_URL:-}" ] && return 0
  [ -f "$ENV_FILE" ] || return 0
  local val
  val="$(grep -E '^REDIS_URL=' "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-)"
  if [ -n "$val" ]; then
    REDIS_URL="$val"
    export REDIS_URL
    log "REDIS_URL résolue depuis ${ENV_FILE} (valeur non affichée)."
  fi
}

CLUSTER_LOCK_TTL="${CLUSTER_LOCK_TTL:-3600}"
_CLUSTER_LOCK_TOKEN="$(hostname 2>/dev/null || echo host)-$$-$(date +%s)"
_CLUSTER_LOCK_MODE=""
_CLUSTER_LOCK_KEY=""

acquire_cluster_lock() {  # acquire_cluster_lock <lock-key-suffix> <label humain>
  _CLUSTER_LOCK_KEY="ladini:cluster-deploy:lock:${1:?lock-key-suffix requis}"
  local label="${2:-orchestration cluster}"
  _resolve_redis_url_from_env_file
  if command -v redis-cli >/dev/null 2>&1 && [ -n "${REDIS_URL:-}" ]; then
    if redis-cli -u "$REDIS_URL" SET "$_CLUSTER_LOCK_KEY" "$_CLUSTER_LOCK_TOKEN" NX EX "$CLUSTER_LOCK_TTL" 2>/dev/null | grep -qx OK; then
      _CLUSTER_LOCK_MODE="redis"
      log "Verrou cluster acquis (Redis, clé ${_CLUSTER_LOCK_KEY}, TTL ${CLUSTER_LOCK_TTL}s)."
      return 0
    fi
    local holder; holder="$(redis-cli -u "$REDIS_URL" GET "$_CLUSTER_LOCK_KEY" 2>/dev/null || true)"
    die "Un(e) autre ${label} est en cours (verrou Redis tenu par: ${holder:-inconnu}). Réessayez plus tard, ou attendez l'expiration du TTL (${CLUSTER_LOCK_TTL}s)."
  fi
  warn "redis-cli/REDIS_URL indisponible — verrou cluster-wide DÉGRADÉ en verrou LOCAL (${CLUSTER_LOCK_DIR}) : protège uniquement contre une double exécution DEPUIS CETTE MACHINE, pas depuis deux machines différentes."
  acquire_named_lock "$CLUSTER_LOCK_DIR" "$label"
  _CLUSTER_LOCK_MODE="local"
}
release_cluster_lock() {
  if [ "$_CLUSTER_LOCK_MODE" = "redis" ]; then
    # DEL seulement si on est toujours le détenteur (évite de supprimer le
    # verrou de quelqu'un d'autre si notre TTL a déjà expiré entre-temps).
    redis-cli -u "$REDIS_URL" eval \
      'if redis.call("GET",KEYS[1])==ARGV[1] then return redis.call("DEL",KEYS[1]) else return 0 end' \
      1 "$_CLUSTER_LOCK_KEY" "$_CLUSTER_LOCK_TOKEN" >/dev/null 2>&1 || true
  elif [ "$_CLUSTER_LOCK_MODE" = "local" ]; then
    release_named_lock "$CLUSTER_LOCK_DIR"
  fi
}

# ── docker compose : toujours avec le bon fichier + .env ───────────
dc() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# ── Image refs ────────────────────────────────────────────────────
image_ref() { printf '%s/%s/ladini-%s:%s' "$REGISTRY" "$IMAGE_NAMESPACE" "$1" "${2:?release}"; }

# ── Manifeste de release ──────────────────────────────────────────
# Écrit un fichier env `key=value` (release courante ou précédente).
# `roles` (2026-09-18) : rôles Compose (profiles) actifs SUR CE NODE pour
# cette release (ex: "app,scheduler,admin", ou "app" seul en mode cluster) —
# permet à rollback.sh de rejouer les BONS `--profile` au lieu d'en deviner
# (voir §BUG CORRIGÉ dans rollback.sh). Vide/absent = rétro-compat avec un
# manifeste écrit avant ce champ (rollback.sh retombe alors sur les 3 rôles).
write_release_file() {
  local target="$1" version="$2" git_sha="${3:-}" built_at="${4:-}" roles="${5:-}"
  mkdir -p "$RELEASES_DIR"
  cat >"$target" <<EOF
RELEASE_VERSION=${version}
GIT_SHA=${git_sha}
BUILD_TIMESTAMP=${built_at}
ROLES=${roles}
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
