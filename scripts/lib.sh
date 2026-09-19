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

# ── Diagnostic PUR (jamais une action) — utilisé par preflight.sh (check 9,
# lancé MANUELLEMENT par un opérateur) pour signaler un déploiement
# concurrent SANS jamais toucher au verrou lui-même : ni le créer, ni le
# libérer, ni le récupérer (seul acquire_named_lock a cette autorité, au
# moment où il tente RÉELLEMENT d'acquérir). echo "free"|"active"|"stale".
# (2026-09-18, §BUG "preflight se bloque sur son propre verrou") : cette
# fonction est volontairement APPELÉE PAR preflight.sh, JAMAIS PAR
# node_deploy.sh sur SON PROPRE acquire_lock() à venir — la garantie contre
# ce faux-positif ne vient pas d'une logique ICI (ex: exclure son propre
# PID), mais de l'ORDRE d'exécution dans node_deploy.sh : préflight (donc
# cette fonction) tourne TOUJOURS avant que ce même process n'appelle
# acquire_lock() — au moment du diagnostic, ce process ne détient encore
# AUCUN verrou, donc "free" par construction, jamais par coïncidence de PID.
deploy_lock_status() {  # deploy_lock_status <lockdir>
  local dir="$1" pid
  [ -d "$dir" ] || { echo "free"; return 0; }
  pid="$(cat "${dir}/pid" 2>/dev/null || echo 0)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "active"
  else
    echo "stale"
  fi
}

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

# ── Celery worker : ping via le control bus (2026-09-18, audit smoke Celery)
# ─────────────────────────────────────────────────────────────────────
# celery_worker_ping <service> <app_module> [timeout_s] [retries] [retry_delay_s]
#
# Exécute `celery -A <app_module> inspect ping` DANS <service> (via `dc
# exec`), en BROADCAST — jamais `--destination`/`--hostname` explicite : le
# but est "AU MOINS UN worker de ce service répond", pas "LE worker nommé
# X répond". Une réplique scale-out future (`docker compose up --scale
# worker=N`) ou un `--hostname` non standard côté commande worker ne doit
# JAMAIS faire échouer ce test pour une raison de nommage (voir le
# HEALTHCHECK figé du Dockerfile, corrigé pour la même raison — il ciblait
# `-d "celery@$(hostname)"`, un nom de destination fragile, désormais dead
# code de toute façon car ce même repli compose l'écrase).
#
# Vérifié contre le code source de Celery 5.6 (celery/bin/control.py) :
# `inspect` lève TOUJOURS un exit code non-zéro dès que `replies` est vide
# (aucun worker n'a répondu dans le délai — "No nodes replied within time
# constraint") OU qu'une exception survient (broker injoignable —
# "Could not connect to the message broker: ..."), avec un message
# descriptif à chaque fois. Le SEUL vrai défaut trouvé dans l'ancienne
# version de ce test : elle jetait ce message (`>/dev/null 2>&1`), rendant
# IMPOSSIBLE de distinguer un worker réellement mort d'un broker
# injoignable ou d'un simple retard de démarrage — exactement l'ambiguïté
# reportée en incident réel (2026-09-18, release sha-6af07e0). Ce message
# est désormais TOUJOURS affiché (stderr) en cas d'échec final.
#
# Retries bornés (défaut 3, délai 3s = ~33s pire cas avec timeout=8 par
# défaut — raisonnable, pas arbitrairement énorme) : le healthcheck Docker
# du service worker (docker-compose.prod.yml) ne sonde que `pgrep -f
# 'celery.*worker'` — un process VIVANT, pas un worker connecté au control
# bus (délibéré, voir son commentaire : `inspect ping` en healthcheck
# redémarrait tous les workers en boucle pendant un hoquet Redis, audit
# 2026-09-10). `wait_healthy_container` (node_deploy.sh) peut donc rendre
# la main dès que le process a forké, potentiellement AVANT que le worker
# ait fini son `worker_process_init` (appels réseau DB/Langfuse — voir
# celery_app.py::worker_proc_alive_timeout, jusqu'à plusieurs dizaines de
# secondes sous contention). Sans retry, ce test peut échouer sur un
# worker parfaitement sain, juste pas encore prêt — un FAUX négatif, pas
# une preuve de panne. Un worker RÉELLEMENT mort/mal configuré épuise
# quand même tous les essais et échoue, fail-closed.
celery_worker_ping() {
  local svc="${1:?service requis}" app="${2:?module Celery (-A) requis}" \
        timeout="${3:-8}" retries="${4:-3}" delay="${5:-3}"
  local attempt out
  for ((attempt = 1; attempt <= retries; attempt++)); do
    # `if out=$(...); then` — PAS une affectation nue : sous `set -e`
    # (hérité par tout appelant qui source lib.sh, dont smoke.sh), une
    # affectation nue `out="$(cmd)"` dont `cmd` échoue tue le script ENTIER
    # immédiatement (même piège que le bug SSH_PORT d'infra/firewall/ufw.sh,
    # 2026-09-17) — avant même d'atteindre le retry ou l'affichage du
    # diagnostic ci-dessous. Tester l'affectation comme condition d'un `if`
    # est l'exemption documentée de `set -e` : `$out` est quand même peuplé
    # (stdout+stderr, 2>&1) que la commande réussisse ou non.
    if out="$(dc exec -T "$svc" celery -A "$app" inspect ping --timeout "$timeout" 2>&1)"; then
      return 0
    fi
    if [ "$attempt" -lt "$retries" ]; then
      sleep "$delay"
    fi
  done
  printf '%s\n' "$out" | sed 's/^/      /' >&2
  return 1
}

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

# ── Validation Load Balancer / chemin public — provider-neutre ─────
# (2026-09-19, incident réel — `HEALTH_TIMEOUT: unbound variable`) :
# `cluster_deploy.sh` référençait `$HEALTH_TIMEOUT` pour sa dernière étape
# (health-check public post-rollout, cluster déjà déployé et healthy sur
# tous les nodes) sans jamais la définir dans SON PROPRE scope — cette
# variable n'existe que dans `node_deploy.sh`/`rollback.sh` (processus SSH
# distincts, sur le node distant, qui n'exportent rien en retour vers
# l'orchestrateur). Sous `set -euo pipefail`, toute référence à une
# variable jamais assignée est fatale. Les deux fonctions ci-dessous
# portent chacune leur PROPRE défaut explicite (même convention
# `HEALTH_TIMEOUT`, même valeur 180s que node_deploy.sh/rollback.sh — voir
# leurs propres `HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"`) : impossible de
# reproduire ce bug depuis un site d'appel qui oublierait de la définir.
#
# Extraites ici (plutôt que codées en dur dans cluster_deploy.sh) pour être
# testables en isolation, sans SSH/inventaire/migrations — voir
# scripts/test/test-cluster-deploy-lb-validation.sh.

# resolve_public_health_url : dérive l'URL de health-check PUBLIQUE à
# sonder. `PUBLIC_HEALTH_URL` explicite en priorité, sinon dérivée de
# `PUBLIC_DOMAIN` (`https://$PUBLIC_DOMAIN/health/ready`), sinon CHAÎNE
# VIDE — ce n'est PAS une erreur : ça signifie "pas de LB/domaine public
# dans cet environnement" (ex: single-node de test), l'appelant doit alors
# SAUTER la validation plutôt que d'échouer. Fonction PURE, aucun accès
# réseau, jamais de crash même si les deux variables sont totalement
# absentes de l'environnement.
resolve_public_health_url() {
  if [ -n "${PUBLIC_HEALTH_URL:-}" ]; then
    printf '%s' "$PUBLIC_HEALTH_URL"
  elif [ -n "${PUBLIC_DOMAIN:-}" ]; then
    printf 'https://%s/health/ready' "$PUBLIC_DOMAIN"
  fi
}

# validate_public_health <url> [timeout_s] : sonde `<url>` jusqu'à 200 (via
# wait_http, donc curl borné --max-time 5 par tentative + retry toutes les
# 3s jusqu'au timeout global — voir wait_http). `timeout_s` retombe sur
# `$HEALTH_TIMEOUT` si définie (convention partagée avec node_deploy.sh/
# rollback.sh), sinon 180s en dur — JAMAIS une référence non gardée.
validate_public_health() {
  local url="${1:?url requise}"
  local timeout="${2:-${HEALTH_TIMEOUT:-180}}"
  wait_http "$url" "$timeout" 200
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
