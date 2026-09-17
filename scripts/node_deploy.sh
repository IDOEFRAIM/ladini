#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/node_deploy.sh — déploiement d'une RELEASE IMMUABLE sur UN SEUL
# node, avec le(s) rôle(s) Compose (`profiles:`) demandé(s).
#
#   ./scripts/node_deploy.sh <release>                       # rétro-compat :
#                                                             #   équivaut à
#                                                             #   l'ancien
#                                                             #   scripts/deploy.sh
#                                                             #   (rôles
#                                                             #   app+scheduler+admin,
#                                                             #   migrations incluses)
#   ./scripts/node_deploy.sh <release> app,scheduler,admin    # explicite, idem
#   ./scripts/node_deploy.sh <release> app --skip-migrate     # node "app" piloté
#                                                             #   par cluster_deploy.sh
#                                                             #   (migrations déjà
#                                                             #   faites ailleurs)
#   ./scripts/node_deploy.sh <release> --single-node          # forme explicite
#                                                             #   du mode rétro-compat
#
# ── Pourquoi ce fichier existe (refactor 2026-09-16, chantier Hetzner) ──
# `docker-compose.prod.yml` porte désormais un `profiles:` par service
# (app/scheduler/admin, voir le fichier). L'ancien scripts/deploy.sh
# lançait `dc up -d` SANS profil : sur le compose ACTUEL, cela démarrerait
# zéro service (aucun service n'est sans profil, sauf `autoheal`). Ce
# script généralise deploy.sh pour accepter une liste de rôles et piloter
# `--profile <role>` en conséquence, tout en restant 100% compatible avec
# l'usage "1 seul node qui fait tout" (phase 1, < 100 users) via le mode
# --single-node / rôles omis.
#
# CHOIX DE REFACTOR : la logique vit ICI (node_deploy.sh) et
# `scripts/deploy.sh` devient un WRAPPER FIN qui appelle
# `node_deploy.sh --single-node`. Raison : node_deploy.sh est le
# sur-ensemble strict (deploy.sh = node_deploy.sh avec tous les rôles) —
# faire l'inverse (deploy.sh contient la logique, node_deploy.sh l'appelle
# avec des variables d'environnement pour restreindre les rôles) aurait
# obligé à faire fuiter le concept de "rôle/profil" dans un script dont le
# NOM ET L'USAGE HISTORIQUE ("le" déploiement, singulier) n'en parlent pas
# — et un opérateur qui lit `./scripts/deploy.sh <release>` dans un
# runbook existant ne doit RIEN avoir à changer. deploy.sh reste donc le
# point d'entrée "legacy / single-VPS", node_deploy.sh le point d'entrée
# "brique élémentaire pilotée par cluster_deploy.sh".
#
# Ne CONSTRUIT rien (build-once). Ne fait PAS `git pull`. Séquence (2026-09-18,
# préflight PUIS lock — voir §BUG CORRIGÉ plus bas pour le pourquoi) :
#   préflight (non-mutant) → lock → snapshot release courante → pull →
#   migration (sauf --skip-migrate) → up -d --profile ... → attente santé →
#   smoke (adapté aux rôles présents SUR CE NODE) → enregistrement release
#   LOCALE → unlock.
#
# Échec AVANT `up` : ce node continue de tourner sur l'ancien code (rien
#   n'a bougé). Échec APRÈS `up` (santé/smoke KO) : rollback APPLICATIF
#   auto vers la release précédente CONNUE DE CE NODE (jamais de rollback
#   DB — §43). En mode cluster (--skip-migrate), un rollback DB n'aurait de
#   toute façon aucun sens ici : la DB est partagée par tout le cluster,
#   décidée par cluster_deploy.sh/cluster_rollback.sh, jamais par un seul
#   node.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

# ── Arguments ──────────────────────────────────────────────────────
TARGET_RELEASE="${1:-}"
[ -n "$TARGET_RELEASE" ] || die "usage: $0 <release> [roles-csv] [--skip-migrate] [--single-node]  (ex: sha-a83f6c1 app)"
shift

ROLES_CSV=""
SKIP_MIGRATE=0
SINGLE_NODE=0
for arg in "$@"; do
  case "$arg" in
    --skip-migrate) SKIP_MIGRATE=1 ;;
    --single-node)  SINGLE_NODE=1 ;;
    --*) die "option inconnue: $arg" ;;
    *)
      [ -z "$ROLES_CSV" ] || die "trop d'arguments positionnels (rôles déjà fournis: $ROLES_CSV)"
      ROLES_CSV="$arg"
      ;;
  esac
done

# Rétro-compat : ni rôles ni --single-node fournis, OU --single-node explicite
# → comportement de l'ancien deploy.sh (tous les rôles, migrations incluses).
if [ -z "$ROLES_CSV" ] || [ "$SINGLE_NODE" = 1 ]; then
  ROLES_CSV="app,scheduler,admin"
  SINGLE_NODE=1
fi

VALID_ROLES=(app scheduler admin)
IFS=',' read -r -a ROLES <<<"$ROLES_CSV"
[ "${#ROLES[@]}" -gt 0 ] || die "roles-csv vide"
PROFILE_ARGS=()
for r in "${ROLES[@]}"; do
  case " ${VALID_ROLES[*]} " in
    *" $r "*) ;;
    *) die "rôle inconnu '$r' (valides: ${VALID_ROLES[*]})" ;;
  esac
  PROFILE_ARGS+=(--profile "$r")
done
_has_role() { local want="$1" r; for r in "${ROLES[@]}"; do [ "$r" = "$want" ] && return 0; done; return 1; }

if [ "$SKIP_MIGRATE" != 1 ] && ! _has_role app; then
  die "migrations demandées (pas de --skip-migrate) mais rôle 'app' absent des rôles ($ROLES_CSV) — pgbouncer/api ne sont PAS sur ce node. En mode cluster, cluster_deploy.sh doit TOUJOURS passer --skip-migrate à node_deploy.sh (les migrations tournent UNE FOIS depuis l'orchestrateur, jamais depuis un node)."
fi

AUTO_ROLLBACK="${AUTO_ROLLBACK:-1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
ROLLBACK_HEALTH_TIMEOUT="${ROLLBACK_HEALTH_TIMEOUT:-90}"

# ── Services concernés par les rôles demandés ─────────────────────
# mcp/api/worker/pgbouncer ⇐ app ; beat ⇐ scheduler ; flower ⇐ admin.
# autoheal n'a pas de profil (toujours démarré, quel que soit le rôle du
# node — voir docker-compose.prod.yml).
PULL_SERVICES=(autoheal)
HEALTH_SERVICES=()
SMOKE_CHECK_APP=0
SMOKE_CHECK_SCHEDULER=0
SMOKE_CHECK_ADMIN=0
if _has_role app; then
  PULL_SERVICES+=(mcp api worker pgbouncer)
  HEALTH_SERVICES+=(mcp api worker pgbouncer)
  SMOKE_CHECK_APP=1
fi
if _has_role scheduler; then
  PULL_SERVICES+=(worker)   # beat réutilise l'image ladini-worker
  HEALTH_SERVICES+=(beat)
  SMOKE_CHECK_SCHEDULER=1
fi
if _has_role admin; then
  PULL_SERVICES+=(api)      # flower réutilise l'image ladini-api
  HEALTH_SERVICES+=(flower)
  SMOKE_CHECK_ADMIN=1
fi
# dédoublonnage (worker/api peuvent apparaître deux fois selon les rôles)
mapfile -t PULL_SERVICES < <(printf '%s\n' "${PULL_SERVICES[@]}" | awk '!seen[$0]++')

CURRENT_RELEASE="$(current_release || true)"
STAGE="init"
FAILED_REASON=""

fail() {  # fail <stage> <reason>
  STAGE="$1"; FAILED_REASON="$2"
  err "ÉCHEC à l'étape « $STAGE » : $FAILED_REASON"

  if [ "${STAGE_REACHED_UP:-0}" = "1" ] && [ "$AUTO_ROLLBACK" = "1" ] && [ -n "$CURRENT_RELEASE" ] && [ "$CURRENT_RELEASE" != "$TARGET_RELEASE" ]; then
    warn "Rollback APPLICATIF automatique (rôles: ${ROLES_CSV}) → ${CURRENT_RELEASE} (la DB n'est PAS touchée)."
    _rb_ok=1
    RELEASE_VERSION="$CURRENT_RELEASE" dc pull "${PULL_SERVICES[@]}" >/dev/null 2>&1 || _rb_ok=0
    RELEASE_VERSION="$CURRENT_RELEASE" dc "${PROFILE_ARGS[@]}" up -d --remove-orphans >/dev/null 2>&1 || _rb_ok=0
    if [ "$_rb_ok" = 1 ] && _has_role app; then
      wait_http "http://127.0.0.1:8000/health/ready" "$ROLLBACK_HEALTH_TIMEOUT" 200 >/dev/null 2>&1 || _rb_ok=0
    fi
    if [ "$_rb_ok" = 1 ]; then
      warn "Rollback applicatif OK — ce node tourne de nouveau sur ${CURRENT_RELEASE}."
      history_append "node-deploy-failed-autorollback" "$TARGET_RELEASE" "→ $CURRENT_RELEASE ; roles=$ROLES_CSV ; stage=$STAGE ; $FAILED_REASON"
    else
      err "Rollback automatique INCERTAIN — intervention manuelle requise sur CE NODE."
      history_append "node-deploy-failed-rollback-uncertain" "$TARGET_RELEASE" "roles=$ROLES_CSV ; stage=$STAGE ; $FAILED_REASON"
    fi
  else
    history_append "node-deploy-failed" "$TARGET_RELEASE" "roles=$ROLES_CSV ; stage=$STAGE ; $FAILED_REASON"
  fi

  cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════════╗
  NODE DEPLOYMENT FAILED
  Stage           : ${STAGE}
  Reason          : ${FAILED_REASON}
  Roles           : ${ROLES_CSV}
  Target release  : ${TARGET_RELEASE}
  Current good    : ${CURRENT_RELEASE:-<aucune enregistrée>}
  Rollback command: ./scripts/rollback.sh ${CURRENT_RELEASE:-<release-precedente>}
╚══════════════════════════════════════════════════════════════════╝
EOF
  exit 1
}
trap 'fail "${STAGE}" "commande inattendue (ligne $LINENO)"' ERR

# ── 1. Préflight ───────────────────────────────────────────────────
# NON-MUTANT (lit des fichiers, sonde docker/registry/ports — ne touche
# jamais le compose stack ni deploy/releases/). Exécuté délibérément AVANT
# acquire_lock() (§BUG CORRIGÉ ICI, 2026-09-18, incident réel : preflight
# tournait APRÈS acquire_lock() et détectait le verrou que CE MÊME
# node_deploy.sh venait de poser, le prenant pour un déploiement concurrent
# — auto-deadlock sur soi-même). Architecture retenue (Option A du brief) :
#   préflight (non-mutant) → acquire_lock() → mutations → release_lock()
# Un petit intervalle existe entre "préflight OK" et "verrou acquis" : deux
# node_deploy.sh concurrents peuvent tous les deux passer le préflight, mais
# UN SEUL obtiendra ensuite acquire_lock() — c'est LUI l'arbitre réel de la
# concurrence, pas preflight.sh (qui ne fait que diagnostiquer, jamais
# arbitrer). Voir scripts/preflight.sh pour le corollaire : son propre
# contrôle de verrou (check 9) reste utile pour un OPÉRATEUR qui le lance
# MANUELLEMENT pendant qu'un déploiement tourne ailleurs — ce n'est plus
# jamais auto-déclenché par node_deploy.sh sur SON PROPRE verrou, puisqu'il
# n'existe pas encore à ce stade.
STAGE="preflight"
log "1/7 · préflight (rôles: ${ROLES_CSV})…"
"${HERE}/preflight.sh" "$TARGET_RELEASE" || fail "preflight" "préconditions non satisfaites (voir ci-dessus)"

# ── Verrou (un seul deploy/rollback à la fois SUR CE NODE) ────────
# À partir d'ICI, et pas avant : c'est la première mutation potentielle
# (tout ce qui suit touche le compose stack et/ou deploy/releases/).
STAGE="lock"
acquire_lock

# ── 2. Résolution des métadonnées ─────────────────────────────────
STAGE="resolve-metadata"
export RELEASE_VERSION="$TARGET_RELEASE"
export COMPOSE_VERSION="compose-$(sha1sum "$COMPOSE_FILE" | cut -c1-12)"
# Si l'appelant (cluster_deploy.sh) a déjà résolu GIT_SHA/BUILD_TIMESTAMP
# (une seule fois, centralement — voir cluster_deploy.sh), on les GARDE :
# ne pas les écraser par une valeur locale potentiellement absente sur un
# node qui ne tire pas l'image "api" (ex: rôle scheduler seul).
export GIT_SHA="${GIT_SHA:-$TARGET_RELEASE}"
export BUILD_TIMESTAMP="${BUILD_TIMESTAMP:-unknown}"

# ── 3. Pull (atomique : si ça casse, rien n'a changé) ─────────────
STAGE="pull"
log "2/7 · pull des images ${TARGET_RELEASE} (${PULL_SERVICES[*]})…"
dc pull "${PULL_SERVICES[@]}" \
  || fail "pull" "impossible de tirer les images ${TARGET_RELEASE} — ce node continue sur l'ancien code"

# métadonnées affinées depuis l'image "api" tirée (source de vérité) —
# seulement si "api" fait partie de ce qu'on vient de tirer (rôle app ou
# admin). Sinon on fait confiance à GIT_SHA/BUILD_TIMESTAMP déjà fournis
# par l'environnement (cluster_deploy.sh) — voir commentaire étape 2.
if printf '%s\n' "${PULL_SERVICES[@]}" | grep -qx api; then
  _gs="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.revision)"
  _ba="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.created)"
  [ -n "$_gs" ] && [ "$_gs" != "unknown" ] && export GIT_SHA="$_gs"
  [ -n "$_ba" ] && [ "$_ba" != "unknown" ] && export BUILD_TIMESTAMP="$_ba"
elif [ "$GIT_SHA" = "$TARGET_RELEASE" ]; then
  warn "   image 'api' non tirée sur ce node (rôles: ${ROLES_CSV}) et GIT_SHA non fourni par l'appelant — GIT_SHA restera '${TARGET_RELEASE}' (dégradé)."
fi

# ── 4. Migrations DB — SAUTÉES si --skip-migrate (mode cluster) ──
STAGE="migrate"
MIG_CLASS="${MIG_CLASS:-ROLLBACK_SAFE}"
if [ "$SKIP_MIGRATE" = 1 ]; then
  log "3/7 · migrations : SAUTÉES (--skip-migrate — déjà faites une fois par cluster_deploy.sh)."
else
  log "3/7 · migrations base de données…"
  RELEASE_VERSION="$TARGET_RELEASE" dc "${PROFILE_ARGS[@]}" up -d --wait --wait-timeout 60 pgbouncer \
    || fail "migrate" "PgBouncer non healthy — DB injoignable (DB_HOST/USER/PASSWORD/NAME ?)"

  # ═══════════════════════════════════════════════════════════════════
  # §BUG CORRIGÉ ICI (2026-09-16, follow-up pre-Hetzner) — même correctif
  # que cluster_deploy.sh (voir son propre commentaire "§BUG CORRIGÉ ICI") :
  # ce chemin single-node passait AUPARAVANT le littéral "HEAD" comme second
  # ref à `migration_class_between`, résolu par check_migrations.sh via
  # `git diff base..HEAD` sur le working tree de LA MACHINE QUI EXÉCUTE LE
  # SCRIPT — jamais garanti de correspondre au commit RÉELLEMENT packagé
  # dans l'image `${TARGET_RELEASE}` qu'on vient de tirer (résidu de rebase,
  # checkout manuel antérieur sur la même machine, etc.). `GIT_SHA` est déjà
  # résolu ci-dessus (étape 2/3, depuis le label OCI `org.opencontainers.
  # image.revision` de l'image RÉELLEMENT tirée — jamais depuis git local) :
  # c'est LUI la source de vérité, jamais "HEAD". `FROM_SHA` vient du
  # manifeste de release LOCAL à ce node (`$PREVIOUS_FILE`, écrit par ce
  # même script à l'étape 8 d'un déploiement antérieur) — équivalent
  # single-node du `PREVIOUS_GIT_SHA` que cluster_deploy.sh lit depuis le
  # manifeste cluster — SAUF que la lecture doit se faire sur `$CURRENT_FILE`
  # (pas `$PREVIOUS_FILE`) : `$CURRENT_FILE` n'est réécrit qu'à l'étape 8
  # ci-dessous, donc à CE stade (étape 3/7) il porte encore le GIT_SHA de la
  # release ACTUELLEMENT en service — exactement le "FROM" recherché.
  # `$PREVIOUS_FILE`, lui, n'est mis à jour qu'à l'étape 8 du déploiement
  # PRÉCÉDENT : le lire ici pointerait un cran trop loin en arrière (N-2 au
  # lieu de N-1).
  # ═══════════════════════════════════════════════════════════════════
  if [ -f "${LADINI_ROOT}/backend/alembic.ini" ]; then
    if [ -n "$CURRENT_RELEASE" ]; then
      git -C "$LADINI_ROOT" cat-file -e "${GIT_SHA}^{commit}" 2>/dev/null \
        || git -C "$LADINI_ROOT" fetch --quiet origin "$GIT_SHA" 2>/dev/null \
        || warn "   commit ${GIT_SHA} introuvable localement même après fetch — classification migration en best-effort (fallback MIGRATION_REQUIRES_MANUAL_RECOVERY si le diff échoue)."
      CURRENT_GIT_SHA="$(read_release_field "$CURRENT_FILE" GIT_SHA 2>/dev/null || true)"
      FROM_SHA="${CURRENT_GIT_SHA:-${GIT_SHA}~1}"
      MIG_CLASS="$(migration_class_between "$FROM_SHA" "$GIT_SHA" || echo MIGRATION_REQUIRES_MANUAL_RECOVERY)"
    fi
    log "   classification migration : ${MIG_CLASS} (${FROM_SHA:-<inconnu>}..${GIT_SHA})"
    RELEASE_VERSION="$TARGET_RELEASE" dc "${PROFILE_ARGS[@]}" run --rm --no-deps -w /app/backend \
      api alembic upgrade head \
      || fail "migrate" "alembic upgrade head a échoué — bascule annulée, ce node tourne toujours sur l'ancien code"
    log "   migrations appliquées."
  else
    warn "   backend/alembic.ini absent → migrations Alembic SAUTÉES (schéma géré hors-Alembic)."
  fi
fi

# ── 5. Bascule applicative (rôles demandés uniquement) ────────────
STAGE="up"
log "4/7 · bascule des services (${TARGET_RELEASE}, rôles: ${ROLES_CSV})…"
STAGE_REACHED_UP=1
RELEASE_VERSION="$TARGET_RELEASE" dc "${PROFILE_ARGS[@]}" up -d --remove-orphans \
  || fail "up" "docker compose up a échoué"

# ── Reverse proxy Caddy — uniquement sur les nodes avec rôle app ────
# Le compose applicatif est lancé d'abord afin que le réseau Docker
# app_agri_net existe avant que Caddy tente de le rejoindre.
if _has_role app; then
  PROXY_DIR="${LADINI_ROOT}/infra/reverse-proxy"

  [ -f "${PROXY_DIR}/.env" ] \
    || fail "up" "reverse proxy: ${PROXY_DIR}/.env absent"

  log "   démarrage/vérification du reverse proxy Caddy…"

  (
    cd "$PROXY_DIR"
    docker compose --env-file .env up -d
  ) || fail "up" "reverse proxy Caddy: docker compose up a échoué"

  CADDY_ID="$(
    cd "$PROXY_DIR"
    docker compose --env-file .env ps -q caddy
  )"

  [ -n "$CADDY_ID" ] \
    || fail "up" "reverse proxy Caddy: conteneur introuvable"

  CADDY_STATUS=""
  for _i in $(seq 1 90); do
    CADDY_STATUS="$(
      docker inspect         --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}'         "$CADDY_ID" 2>/dev/null || true
    )"

    [ "$CADDY_STATUS" = "healthy" ] && break
    sleep 1
  done

  [ "$CADDY_STATUS" = "healthy" ] \
    || fail "health" "reverse proxy Caddy non healthy après 90s (status=${CADDY_STATUS:-unknown})"

  log "   ✓ Caddy healthy"
fi

# ── 6. Attente de la SANTÉ RÉELLE (services de CE node uniquement) ─
STAGE="health"
log "5/7 · attente santé (timeout ${HEALTH_TIMEOUT}s)…"
for svc in "${HEALTH_SERVICES[@]}"; do
  wait_healthy_container "$svc" "$HEALTH_TIMEOUT" \
    || fail "health" "conteneur '$svc' n'est jamais passé healthy"
  log "   ✓ $svc healthy"
done
if _has_role app; then
  wait_http "http://127.0.0.1:8000/health/ready" "$HEALTH_TIMEOUT" 200 \
    || fail "health" "/health/ready ≠ 200 (DB ou Redis non joignables depuis l'API de ce node)"
  log "   ✓ /health/ready = 200"
fi

# ── 7. Smoke tests — adaptés aux rôles PRÉSENTS SUR CE NODE ───────
# smoke.sh sait désormais se limiter via SMOKE_CHECK_APP/SCHEDULER/ADMIN
# (voir scripts/smoke.sh) : sur un node "scheduler" seul par exemple, il
# n'y a ni conteneur api ni conteneur worker — les sections qui en
# dépendent seraient de faux échecs si on les laissait actives.
STAGE="smoke"
log "6/7 · smoke tests (app=${SMOKE_CHECK_APP} scheduler=${SMOKE_CHECK_SCHEDULER} admin=${SMOKE_CHECK_ADMIN})…"
EXPECT_RELEASE="$TARGET_RELEASE" \
  SMOKE_CHECK_APP="$SMOKE_CHECK_APP" \
  SMOKE_CHECK_SCHEDULER="$SMOKE_CHECK_SCHEDULER" \
  SMOKE_CHECK_ADMIN="$SMOKE_CHECK_ADMIN" \
  "${HERE}/smoke.sh" \
  || fail "smoke" "smoke tests KO"

# ── 8. Enregistrement de la release réussie (LOCALE à ce node) ────
STAGE="record"
log "7/7 · enregistrement de la release (locale à ce node)…"
if [ -n "$CURRENT_RELEASE" ] && [ "$CURRENT_RELEASE" != "$TARGET_RELEASE" ]; then
  cp -f "$CURRENT_FILE" "$PREVIOUS_FILE"
fi
write_release_file "$CURRENT_FILE" "$TARGET_RELEASE" "$GIT_SHA" "$BUILD_TIMESTAMP" "$ROLES_CSV"
history_append "node-deploy-success" "$TARGET_RELEASE" "roles=$ROLES_CSV ; prev=${CURRENT_RELEASE:-none} ; mig=${MIG_CLASS}"
trap - ERR

cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  NODE DEPLOYMENT SUCCESS
  Roles     : ${ROLES_CSV}
  Release   : ${TARGET_RELEASE}
  Previous  : ${CURRENT_RELEASE:-<aucune>}
  Git SHA   : ${GIT_SHA}
  Built at  : ${BUILD_TIMESTAMP}
  Migration : ${MIG_CLASS}
  Health    : OK
  Smoke     : OK
  Rollback  : ./scripts/rollback.sh            # → ${CURRENT_RELEASE:-<none>}
╚══════════════════════════════════════════════════════════════════╝
EOF
dc ps
