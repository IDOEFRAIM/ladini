#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/cluster_deploy.sh — ORCHESTRATEUR multi-node (chantier Hetzner
# scale-out). Pilote scripts/node_deploy.sh SUR CHAQUE node de
# infra/inventory.yml, un à la fois (rolling), avec les migrations DB
# faites UNE SEULE FOIS depuis ICI.
#
#   ./scripts/cluster_deploy.sh <release> [inventory_file]
#   (inventory_file par défaut : infra/inventory.yml)
#
#         CLUSTER ORCHESTRATOR  (ce script)
#                 ↓ (un appel SSH par node, dans l'ORDRE de l'inventaire,
#                    scheduler en dernier — voir §7 plus bas)
#         scripts/node_deploy.sh <release> <roles> --skip-migrate
#
# Ne construit rien, ne fait pas de leader election, ne dépend d'aucun
# système de consensus distribué (Consul/etcd) — le brief l'interdit
# explicitement : un rôle scheduler UNIQUE, décidé dans l'inventaire et
# validé par scripts/validate_inventory.py, suffit (§9/§44).
#
# Échec sur un node : le rollout S'ARRÊTE (les nodes suivants ne sont PAS
# touchés) et les nodes DÉJÀ à jour restent à jour — voir §9 ci-dessous et
# scripts/cluster_rollback.sh pour la remédiation.
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

TARGET_RELEASE="${1:-}"
INVENTORY_FILE="${2:-${LADINI_ROOT}/infra/inventory.yml}"
[ -n "$TARGET_RELEASE" ] || die "usage: $0 <release> [inventory_file]  (ex: sha-a83f6c1 infra/inventory.yml)"

# Répertoire du dépôt sur CHAQUE node distant (même convention que l'actuel
# .github/workflows/deploy.yml::secrets.DEPLOY_DIR — ici un seul répertoire
# partagé par tous les nodes de l'inventaire ; si un jour les nodes ont des
# chemins différents, ce sera un champ additionnel de infra/inventory.yml,
# volontairement PAS ajouté maintenant — YAGNI tant qu'un seul chemin suffit).
NODE_DEPLOY_DIR="${NODE_DEPLOY_DIR:-/opt/ladini/app}"
CLUSTER_SSH_KEY="${CLUSTER_SSH_KEY:-}"

# (2026-09-19, incident réel — `HEALTH_TIMEOUT: unbound variable` à l'étape
# 7/9) : même convention/même défaut que node_deploy.sh/rollback.sh
# (`HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"`) — mais déclarée ICI aussi,
# dans le scope de CET orchestrateur, qui ne l'a jamais héritée de ces
# scripts-là (processus SSH distincts sur les nodes distants, rien n'est
# exporté en retour). Voir lib.sh::validate_public_health pour la garde
# supplémentaire côté fonction (défense en profondeur — cette ligne ne
# doit toutefois jamais être retirée, c'est ELLE la source de vérité pour
# un opérateur qui veut surcharger ce délai sur ce script précis).
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=accept-new
)

if [ -n "$CLUSTER_SSH_KEY" ]; then
  [ -f "$CLUSTER_SSH_KEY" ] || die "CLUSTER_SSH_KEY introuvable : $CLUSTER_SSH_KEY"
  SSH_OPTS+=(
    -i "$CLUSTER_SSH_KEY"
    -o IdentitiesOnly=yes
  )
fi

CLUSTER_MANIFEST="${RELEASES_DIR}/cluster-current.json"
CLUSTER_DEPLOY_STARTED_AT="$(date -u +%FT%TZ)"

# ── Verrou CLUSTER-WIDE — implémentation dans lib.sh (acquire_cluster_lock/
# release_cluster_lock), PARTAGÉE avec cluster_rollback.sh — voir le
# commentaire détaillé de lib.sh (§DEADLOCK) pour : pourquoi ce verrou vit
# sur une ressource TOUJOURS DISTINCTE du verrou local par node (LOCK_DIR),
# le choix Redis SET NX EX vs. repli local, et la résolution de REDIS_URL
# depuis $ENV_FILE quand l'appelant ne l'a pas déjà exportée.
#
# `release_cluster_lock` gère explicitement le DEL Redis / la suppression du
# répertoire local ; `release_all_locks` (défini par lib.sh, PAS réenregistré
# ici — un `trap ... EXIT` REMPLACE le précédent, jamais ne l'empile) est
# donc explicitement rappelé en second : filet de sécurité pour tout autre
# verrou nommé encore tenu (voir l'étape 5/9 ci-dessous, verrou local
# temporaire autour des migrations).
trap 'release_cluster_lock; release_all_locks' EXIT

# ── JSON minimal (pas de dépendance jq) ────────────────────────────
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

CLUSTER_NODE_RESULTS=()   # "name|host|roles|status|release" par node traité
write_cluster_manifest() {
  # Réécrit ENTIÈREMENT deploy/releases/cluster-current.json à CHAQUE étape
  # (avant même la fin du rollout) — répond à "release désirée du cluster vs
  # release réelle de chaque node" (§25) et permet à cluster_rollback.sh (et
  # à un humain) de savoir où le rollout s'est arrêté, même en plein milieu.
  # Écriture atomique (tmp + mv) pour ne jamais laisser un lecteur voir un
  # JSON tronqué.
  local tmp="${CLUSTER_MANIFEST}.tmp.$$"
  mkdir -p "$RELEASES_DIR"
  {
    printf '{\n'
    printf '  "inventory": "%s",\n' "$(json_escape "$INVENTORY_FILE")"
    printf '  "desired_release": "%s",\n' "$(json_escape "$TARGET_RELEASE")"
    printf '  "desired_git_sha": "%s",\n' "$(json_escape "${GIT_SHA:-}")"
    printf '  "migration_class": "%s",\n' "$(json_escape "${MIG_CLASS:-unknown}")"
    printf '  "started_at": "%s",\n' "$(json_escape "$CLUSTER_DEPLOY_STARTED_AT")"
    printf '  "updated_at": "%s",\n' "$(date -u +%FT%TZ)"
    printf '  "deployed_by": "%s",\n' "$(json_escape "${USER:-?}")"
    printf '  "nodes": [\n'
    local total=${#CLUSTER_NODE_RESULTS[@]} i=0 n nm hs rl st rel
    for n in "${CLUSTER_NODE_RESULTS[@]}"; do
      i=$((i + 1))
      IFS='|' read -r nm hs rl st rel <<<"$n"
      printf '    {"name": "%s", "host": "%s", "roles": "%s", "status": "%s", "release": "%s"}%s\n' \
        "$(json_escape "$nm")" "$(json_escape "$hs")" "$(json_escape "$rl")" \
        "$(json_escape "$st")" "$(json_escape "$rel")" \
        "$([ "$i" -lt "$total" ] && printf ',')"
    done
    printf '  ]\n'
    printf '}\n'
  } >"$tmp"
  mv -f "$tmp" "$CLUSTER_MANIFEST"
}

# ── Parseur d'inventaire — MIROIR VOLONTAIRE de
# scripts/validate_inventory.py::parse_nodes (même format restreint, même
# sous-ensemble YAML). Si l'un des deux évolue, l'autre DOIT être mis à jour
# en même temps — pas de dépendance PyYAML ici non plus, pour rester
# exécutable sur un node/CI minimal (voir la note du .py).
parse_inventory() {
  sed 's/#.*//' "$1" | awk '
    /^[[:space:]]*-[[:space:]]*name:/ {
      if (name != "") print name "\t" host "\t" roles
      name=$0; sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/,"",name); gsub(/[[:space:]]+$/,"",name)
      host=""; roles=""
      next
    }
    /^[[:space:]]*host:/ {
      host=$0; sub(/^[[:space:]]*host:[[:space:]]*/,"",host); gsub(/[[:space:]]+$/,"",host)
      next
    }
    /^[[:space:]]*roles:[[:space:]]*\[/ {
      roles=$0; sub(/^.*roles:[[:space:]]*\[/,"",roles); sub(/\].*$/,"",roles)
      gsub(/[[:space:]]/,"",roles)
      next
    }
    END { if (name != "") print name "\t" host "\t" roles }
  '
}

# ═════════════════════════════════════════════════════════════════════
# 1. Validation de l'inventaire — ABORT bruyant si invalide, RIEN touché.
# ═════════════════════════════════════════════════════════════════════
log "1/9 · validation de l'inventaire (${INVENTORY_FILE})…"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python
"$PYTHON_BIN" "${HERE}/validate_inventory.py" "$INVENTORY_FILE" \
  || die "Inventaire invalide (voir ci-dessus) — AUCUN node n'a été touché."

# ═════════════════════════════════════════════════════════════════════
# 1.5. Connectivité SSH — TOUS les nodes de l'inventaire, AVANT toute
# mutation (verrou, migrations). Objectif : détecter un node injoignable
# (clé absente, host down, firewall cloud pas encore ouvert pour cette IP…)
# EN UN SEUL COUP, avant d'avoir déjà migré la DB et basculé un premier
# node — pas après coup, au milieu d'un rollout, où l'échec laisserait le
# cluster dans un état hétérogène (certains nodes déjà sur la nouvelle
# release, d'autres non — voir §5 plus bas pour ce cas malgré tout géré).
# Best-effort mais EXHAUSTIF : on teste TOUS les nodes avant de conclure,
# pas d'arrêt au premier échec, pour que l'opérateur voie le tableau
# complet en un seul essai plutôt que de découvrir les pannes une à une.
# ═════════════════════════════════════════════════════════════════════
log "2/9 · connectivité SSH — ${INVENTORY_FILE}…"
mapfile -t PRECHECK_LINES < <(parse_inventory "$INVENTORY_FILE")
[ "${#PRECHECK_LINES[@]}" -gt 0 ] || die "Aucun node parsé depuis ${INVENTORY_FILE} (pourtant validé à l'étape 1) — parseur bash désynchronisé de validate_inventory.py."

SSH_PRECHECK_FAILED=0
for line in "${PRECHECK_LINES[@]}"; do
  IFS=$'\t' read -r name host _roles <<<"$line"
  if ssh "${SSH_OPTS[@]}" "$host" true >/dev/null 2>&1; then
    log "   ✓ ${name} (${host}) joignable en SSH"
  else
    err "   ✗ ${name} (${host}) INJOIGNABLE en SSH (clé/host/firewall ?)"
    SSH_PRECHECK_FAILED=1
  fi
done
[ "$SSH_PRECHECK_FAILED" = 0 ] \
  || die "Au moins un node est injoignable en SSH (voir ci-dessus) — AUCUN node n'a été touché, AUCUNE migration DB n'a tourné."

# ═════════════════════════════════════════════════════════════════════
# 2. Verrou cluster-wide
# ═════════════════════════════════════════════════════════════════════
acquire_cluster_lock "$(basename "$INVENTORY_FILE")" "cluster_deploy.sh"

# Manifeste PRÉCÉDENT (avant de l'écraser) — utile pour résoudre le SHA
# "d'avant" pour la classification migration (§4) si dispo.
PREVIOUS_GIT_SHA=""
if [ -f "$CLUSTER_MANIFEST" ]; then
  PREVIOUS_GIT_SHA="$(grep -o '"desired_git_sha": *"[^"]*"' "$CLUSTER_MANIFEST" 2>/dev/null | head -n1 | sed 's/.*"\([^"]*\)"$/\1/')"
fi

# ═════════════════════════════════════════════════════════════════════
# 3. Préflight local (l'orchestrateur va lui-même piloter le pull + les
#    migrations, il doit satisfaire les mêmes préconditions que deploy.sh)
# ═════════════════════════════════════════════════════════════════════
log "3/9 · préflight (orchestrateur)…"
"${HERE}/preflight.sh" "$TARGET_RELEASE" || die "préflight orchestrateur KO — voir ci-dessus. Rien n'a été touché."

# ═════════════════════════════════════════════════════════════════════
# 4. Résolution des métadonnées de release — UNE SEULE FOIS, ICI.
# ═════════════════════════════════════════════════════════════════════
log "4/9 · résolution des métadonnées (pull local de l'image api)…"
export RELEASE_VERSION="$TARGET_RELEASE"
export COMPOSE_VERSION="compose-$(sha1sum "$COMPOSE_FILE" | cut -c1-12)"
dc pull api >/dev/null || die "impossible de tirer l'image api:${TARGET_RELEASE} depuis l'orchestrateur — aucun node n'a été touché."
GIT_SHA="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.revision)"
BUILD_TIMESTAMP="$(image_label api "$TARGET_RELEASE" org.opencontainers.image.created)"
[ -n "$GIT_SHA" ] && [ "$GIT_SHA" != unknown ] \
  || die "GIT_SHA introuvable dans les labels OCI de l'image api:${TARGET_RELEASE} — impossible de résoudre la release avec confiance (voir §BUG ci-dessous). Aucun node n'a été touché."
[ -n "$BUILD_TIMESTAMP" ] && [ "$BUILD_TIMESTAMP" != unknown ] || BUILD_TIMESTAMP="unknown"
export GIT_SHA BUILD_TIMESTAMP
log "   GIT_SHA=${GIT_SHA} BUILD_TIMESTAMP=${BUILD_TIMESTAMP}"

# ═════════════════════════════════════════════════════════════════════
# §BUG CORRIGÉ ICI — classification migration : source de vérité = le
# GIT_SHA résolu ci-dessus (labels OCI de l'image RÉELLEMENT déployée),
# JAMAIS un HEAD local supposé.
#
# Avant ce chantier, deploy.sh appelait (voir lib.sh::migration_class_between,
# toujours utilisé tel quel par node_deploy.sh en mode single-node) :
#     migration_class_between "${GIT_SHA:-HEAD~1}" HEAD
# où le second argument est le littéral "HEAD" — résolu par check_migrations.sh
# via `git diff base..HEAD` sur le dépôt qui EXÉCUTE le script. Sur un seul
# host qui vient de faire `git checkout <sha> -- scripts/ ...` juste avant
# (voir l'ancien .github/workflows/deploy.yml), HEAD colle en général au bon
# commit — mais c'est une COÏNCIDENCE d'ordonnancement, pas une garantie : Un
# orchestrateur qui pilote PLUSIEURS nodes depuis une machine dont le
# working tree peut être sur N'IMPORTE QUEL autre commit (rebase en cours,
# résidu d'une exécution précédente, dev qui a fait un checkout manuel sur
# la même machine…) classifierait alors LA MAUVAISE RELEASE. Un faux
# ROLLBACK_SAFE masquerait une vraie migration destructive — exactement le
# scénario que §43/§11 du projet veulent empêcher.
#
# Correctif : ne JAMAIS passer le littéral "HEAD". On passe explicitement
# GIT_SHA (résolu depuis le label OCI immuable de l'image, à l'étape 4
# ci-dessus) comme second ref. `check_migrations.sh` fait un `git diff
# base..head` sur des OBJETS git (pas sur le working tree checké out) — donc
# aucun besoin de `git checkout` : juste s'assurer que le commit existe
# localement (fetch si besoin) avant de diffuser dessus.
# ═════════════════════════════════════════════════════════════════════
log "5/9 · migrations base de données (UNE SEULE FOIS, depuis l'orchestrateur)…"
STAGE_MIGRATE_DONE=0
MIG_CLASS="ROLLBACK_SAFE"
# ═════════════════════════════════════════════════════════════════════
# §BUG CORRIGÉ ICI (2026-09-26, incident migration delivery) : cette étape
# était gardée par `if [ -f backend/alembic.ini ]` — fichier qui n'existe
# NULLE PART dans ce repo (Alembic n'a jamais été configuré ; voir le
# commentaire de tête de `check_migrations.sh` : "Drizzle définit et migre ;
# le backend n'exécute plus aucun DDL"). Résultat : la classification
# ci-dessous tournait bien, mais AUCUNE migration Drizzle
# (`backend/schema_contract/migrations/`) n'était jamais réellement
# appliquée — sur AUCUNE release depuis la mise en place de ce pipeline.
# `0004_add_monthly_recurrence.sql` était présent sur chaque node, référencé
# dans `_journal.json`, jamais rejoué contre la vraie base : le `CHECK
# recurrence_type_chk` de prod n'a jamais vu MONTHLY malgré un code
# applicatif qui l'acceptait déjà — `create_recurring_need` échouait en
# CheckViolationError, masqué à l'agent par la sanitisation générique
# d'erreurs DB. Remplacé par le runner officiel partagé
# (`ladini.schema_migrations`, `backend/tests/schema/db_tools.py` réutilise
# EXACTEMENT le même moteur — voir sa docstring) : lit `_journal.json`,
# n'applique que ce qui manque (table `__drizzle_migrations`), échoue fort.
# Plus de `if` sur un fichier qui n'existera jamais : cette étape tourne
# TOUJOURS, et son échec bloque TOUJOURS le déploiement.
# ═════════════════════════════════════════════════════════════════════
git -C "$LADINI_ROOT" cat-file -e "${GIT_SHA}^{commit}" 2>/dev/null \
  || git -C "$LADINI_ROOT" fetch --quiet origin "$GIT_SHA" 2>/dev/null \
  || warn "   commit ${GIT_SHA} introuvable localement même après fetch — classification migration en best-effort (fallback MIGRATION_REQUIRES_MANUAL_RECOVERY si le diff échoue)."

FROM_SHA="${PREVIOUS_GIT_SHA:-${GIT_SHA}~1}"
MIG_CLASS="$(migration_class_between "$FROM_SHA" "$GIT_SHA" || echo MIGRATION_REQUIRES_MANUAL_RECOVERY)"
log "   classification migration : ${MIG_CLASS}  (${FROM_SHA}..${GIT_SHA})"

# Verrou LOCAL PAR NODE (LOCK_DIR — le même que deploy.sh/node_deploy.sh),
# tenu UNIQUEMENT le temps de ces deux commandes : elles mutent le compose
# stack LOCAL de la machine orchestratrice (pgbouncer up, migration DB) —
# exactement le genre d'opération que ce verrou existe pour protéger,
# peu importe que ce soit ICI (l'orchestrateur) ou node_deploy.sh (un
# node) qui la déclenche. Acquis puis RELÂCHÉ avant la boucle SSH
# ci-dessous (étape 5/9) — jamais tenu pendant le rollout lui-même, donc
# jamais en conflit avec node_deploy.sh acquérant ce même verrou sur un
# node co-localisé (voir §DEADLOCK dans lib.sh : ce sont deux verrous
# DISTINCTS de toute façon, mais celui-ci en particulier n'a pas de raison
# de rester tenu plus longtemps que la mutation qu'il protège).
acquire_lock
RELEASE_VERSION="$TARGET_RELEASE" dc --profile app up -d --wait --wait-timeout 60 pgbouncer \
  || die "PgBouncer local (orchestrateur) non healthy — DB injoignable depuis cette machine (.env DB_HOST/USER/PASSWORD/NAME ?). Aucun node n'a été touché."
# `python -m ladini.schema_migrations.cli` (asyncpg direct, statement_cache_size=0 —
# voir sa docstring pour l'incompatibilité PgBouncer transaction-mode/prepared
# statements) affiche explicitement current/pending/applied — jamais un
# "migrations appliquées" muet. Sortie non nulle = migration cassée = deploy
# STOP ici, avant tout rollout : aucun node n'est jamais touché sur cet échec.
RELEASE_VERSION="$TARGET_RELEASE" dc --profile app run --rm --no-deps -w /app/backend/src \
  api python -m ladini.schema_migrations.cli \
  || die "L'application des migrations Drizzle a échoué depuis l'orchestrateur (voir le détail ci-dessus) — AUCUN node n'a été touché, AUCUN rollback automatique (intervention manuelle requise sur la migration en échec)."
release_lock
STAGE_MIGRATE_DONE=1

# ═════════════════════════════════════════════════════════════════════
# 5/6/7. Rolling deploy par node — UN À LA FOIS, dans l'ordre de
# l'inventaire, SAUF le node "scheduler" qui passe EN DERNIER.
#
# Pourquoi scheduler en dernier (et pas en premier) : c'est le SEUL rôle
# sans redondance possible (§9 — beat DOIT rester un singleton). En le
# déployant en dernier :
#   - si un node "app" échoue en cours de route, le rollout s'arrête AVANT
#     d'avoir jamais touché au scheduler — Celery Beat continue de tourner
#     sur l'ANCIEN code, stable, pendant toute la durée de l'incident ;
#   - les tâches planifiées ne sont jamais interrompues par une release qui
#     s'avère mauvaise sur un node app ;
#   - en phase 1 (1 seul node cumule tout), cet ordre est un no-op — il ne
#     change rien tant que app_node_count == 1.
# ═════════════════════════════════════════════════════════════════════
mapfile -t NODE_LINES < <(parse_inventory "$INVENTORY_FILE")
[ "${#NODE_LINES[@]}" -gt 0 ] || die "Aucun node parsé depuis ${INVENTORY_FILE} (pourtant validé à l'étape 1 ?) — parseur bash désynchronisé de validate_inventory.py."

ORDERED_LINES=()
SCHEDULER_LINE=""
for line in "${NODE_LINES[@]}"; do
  IFS=$'\t' read -r _n _h roles <<<"$line"
  if [[ ",${roles}," == *",scheduler,"* ]]; then
    SCHEDULER_LINE="$line"
  else
    ORDERED_LINES+=("$line")
  fi
done
[ -n "$SCHEDULER_LINE" ] && ORDERED_LINES+=("$SCHEDULER_LINE")

log "6/9 · rollout — ${#ORDERED_LINES[@]} node(s), un à la fois (scheduler en dernier)…"

ROLLOUT_FAILED=0
FAILED_NODE_NAME=""
NODES_ATTEMPTED=0
for line in "${ORDERED_LINES[@]}"; do
  IFS=$'\t' read -r name host roles <<<"$line"
  NODES_ATTEMPTED=$((NODES_ATTEMPTED + 1))
  log "   → node '${name}' (${host}) — rôles: ${roles}"

  if ssh "${SSH_OPTS[@]}" "$host" bash -s <<REMOTE_SCRIPT
set -Eeuo pipefail
cd "${NODE_DEPLOY_DIR}"
git fetch --quiet --tags origin
git checkout --quiet "${GIT_SHA}" -- scripts/ docker-compose.prod.yml infra/ 2>/dev/null || true
GIT_SHA="${GIT_SHA}" BUILD_TIMESTAMP="${BUILD_TIMESTAMP}" MIG_CLASS="${MIG_CLASS}" \
  ./scripts/node_deploy.sh "${TARGET_RELEASE}" "${roles}" --skip-migrate
REMOTE_SCRIPT
  then
    log "   ✓ node '${name}' OK (${TARGET_RELEASE})"
    CLUSTER_NODE_RESULTS+=("${name}|${host}|${roles}|success|${TARGET_RELEASE}")
  else
    err "   ✗ node '${name}' ÉCHEC — arrêt du rollout (les nodes suivants ne seront PAS touchés)."
    CLUSTER_NODE_RESULTS+=("${name}|${host}|${roles}|failed|${TARGET_RELEASE}")
    ROLLOUT_FAILED=1
    FAILED_NODE_NAME="$name"
    break
  fi
  write_cluster_manifest
done

# Nodes jamais atteints (après le point d'échec) — consignés "not-attempted"
# pour que cluster-current.json reflète l'état RÉEL, pas un état supposé.
if [ "$ROLLOUT_FAILED" = 1 ]; then
  for ((idx = NODES_ATTEMPTED; idx < ${#ORDERED_LINES[@]}; idx++)); do
    IFS=$'\t' read -r name host roles <<<"${ORDERED_LINES[$idx]}"
    CLUSTER_NODE_RESULTS+=("${name}|${host}|${roles}|not-attempted|")
  done
fi
write_cluster_manifest

if [ "$ROLLOUT_FAILED" = 1 ]; then
  history_append "cluster-deploy-failed" "$TARGET_RELEASE" "failed_node=${FAILED_NODE_NAME} ; mig=${MIG_CLASS}"
  cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════════╗
  CLUSTER DEPLOYMENT FAILED
  Failed node     : ${FAILED_NODE_NAME}
  Target release  : ${TARGET_RELEASE}
  Migration class : ${MIG_CLASS}
  Manifest        : ${CLUSTER_MANIFEST}

  IMPORTANT — le rollout s'est ARRÊTÉ ici. Les nodes déployés AVANT
  '${FAILED_NODE_NAME}' tournent maintenant sur ${TARGET_RELEASE} et ne
  sont PAS revenus en arrière automatiquement (pas de rollback en cascade
  — chaque node bon reste bon). node_deploy.sh a déjà tenté un rollback
  APPLICATIF LOCAL sur '${FAILED_NODE_NAME}' lui-même (voir ses logs SSH
  ci-dessus) ; la DB, elle, n'a PAS bougé (rollback DB jamais automatique).
EOF
  if [ "$MIG_CLASS" = "MIGRATION_REQUIRES_MANUAL_RECOVERY" ]; then
    cat >&2 <<EOF
  ⚠ MIGRATION_REQUIRES_MANUAL_RECOVERY : cette release contient une
    migration DB non réversible automatiquement. Un rollback applicatif
    (code) seul, sur QUELQUE node que ce soit, NE restaurera PAS le schéma
    attendu par l'ancien code. Consultez docs/runbooks/database-restore.md
    AVANT tout rollback.
EOF
  else
    cat >&2 <<EOF
  ✓ ROLLBACK_SAFE : un rollback applicatif (code) est sûr côté schéma.
EOF
  fi
  cat >&2 <<EOF

  Prochaine étape : ./scripts/cluster_rollback.sh ${INVENTORY_FILE}
╚══════════════════════════════════════════════════════════════════╝
EOF
  exit 1
fi

# ═════════════════════════════════════════════════════════════════════
# 7. Validation Load Balancer — best-effort, provider-neutre.
#
# (2026-09-19) — `infra/providers/hetzner/outputs.tf` expose désormais
# `load_balancer_ipv4`, mais ce script reste délibérément SANS dépendance à
# `terraform`/`hcloud` (voir infra/providers/README.md, "provider-neutral" —
# `.github/workflows/deploy.yml` lui-même n'en a aucune). On ne sonde donc
# PAS le LB par son IP Hetzner directe : on sonde le domaine PUBLIC
# (`PUBLIC_HEALTH_URL`, ou dérivé de `PUBLIC_DOMAIN` si fourni — CETTE
# variable est déjà disponible dans .github/workflows/deploy.yml, aucun
# nouveau secret/input requis). C'est en réalité un check PLUS complet que
# "seulement le LB" : il traverse Cloudflare → LB → un node app réel, donc
# valide le chemin qu'un vrai utilisateur emprunte, pas seulement
# l'infrastructure interne.
#
# Chaque node a DÉJÀ été validé individuellement (node_deploy.sh a fait
# tourner son propre health+smoke en loopback avant de rendre la main via
# SSH) — ce hook valide EN PLUS que le chemin public route correctement
# vers un node "app" à jour. Sans `PUBLIC_HEALTH_URL`/`PUBLIC_DOMAIN` fourni
# (single-node sans LB, environnement de test…), cette étape reste un no-op
# documenté plutôt qu'une vérification inventée.
#
# `resolve_public_health_url`/`validate_public_health` (lib.sh) portent
# CHACUNE leur propre défaut explicite pour `HEALTH_TIMEOUT` — voir leur
# docstring pour l'incident (`unbound variable`) que cette extraction
# corrige à la racine, pas seulement au site d'appel.
# ═════════════════════════════════════════════════════════════════════
PUBLIC_HEALTH_URL="$(resolve_public_health_url)"

if [ -n "$PUBLIC_HEALTH_URL" ]; then
  log "7/9 · validation Load Balancer/chemin public (${PUBLIC_HEALTH_URL}, timeout ${HEALTH_TIMEOUT}s)…"
  if validate_public_health "$PUBLIC_HEALTH_URL" "$HEALTH_TIMEOUT"; then
    log "   ✓ ${PUBLIC_HEALTH_URL} = 200"
  else
    # ⚠️ SÉMANTIQUE IMPORTANTE — CE N'EST PAS UN ÉCHEC DE DÉPLOIEMENT.
    # À ce stade, TOUS les nodes de l'inventaire ont déjà individuellement
    # réussi (rollout terminé sans erreur juste au-dessus, chacun avec son
    # propre health+smoke en loopback validé par node_deploy.sh) et le
    # manifeste cluster a déjà été écrit avec leur statut "success" (voir
    # write_cluster_manifest, appelé après la boucle de rollout, AVANT
    # cette étape). Seul le CHEMIN PUBLIC (Cloudflare → Load Balancer →
    # node) n'a pas répondu 200 dans le délai imparti — un problème
    # d'infrastructure/routage, jamais une preuve que le code déployé est
    # mauvais. On distingue donc explicitement ce cas du bloc CLUSTER
    # DEPLOYMENT FAILED ci-dessus (qui, lui, signifie qu'un NODE a
    # réellement échoué) : même code de sortie (1 — cette validation reste
    # critique, voir la consigne), mais un message qui ne laisse planer
    # aucun doute sur ce qui a réellement échoué.
    history_append "cluster-deploy-nodes-ok-lb-failed" "$TARGET_RELEASE" "nodes=${#ORDERED_LINES[@]} ; mig=${MIG_CLASS} ; url=${PUBLIC_HEALTH_URL}"
    cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════════╗
  NODE DEPLOYMENT SUCCEEDED — CLUSTER (LB/PUBLIC) VALIDATION FAILED
  Release         : ${TARGET_RELEASE}
  Git SHA         : ${GIT_SHA}
  Nodes (${#ORDERED_LINES[@]})      : tous déployés et healthy individuellement (voir le détail ci-dessus)
  Failed check    : validation Load Balancer / chemin public
  URL             : ${PUBLIC_HEALTH_URL}
  Timeout         : ${HEALTH_TIMEOUT}s
  Manifest        : ${CLUSTER_MANIFEST}

  IMPORTANT — ceci N'EST PAS un échec de déploiement applicatif. Chaque
  node de l'inventaire tourne déjà sur ${TARGET_RELEASE} et a validé sa
  propre santé (health+smoke, en loopback) AVANT cette étape. Seul le
  CHEMIN PUBLIC (Cloudflare → Load Balancer → node) n'a pas répondu 200
  sur /health/ready dans le délai imparti — suspectez le Load Balancer
  (target marqué unhealthy), le DNS, ou Cloudflare, PAS l'application.
  Voir infra/providers/hetzner/README.md § Load Balancer.

  Aucun rollback n'est nécessaire à ce stade — le code déployé est sain.
  Prochaine étape : vérifiez la console Hetzner (Load Balancer → Targets)
  et/ou testez manuellement : curl -v ${PUBLIC_HEALTH_URL}
╚══════════════════════════════════════════════════════════════════╝
EOF
    exit 1
  fi
else
  log "7/9 · validation LB — SAUTÉE (ni PUBLIC_HEALTH_URL ni PUBLIC_DOMAIN fournis — pas de LB/domaine public dans cet environnement)."
fi

# ═════════════════════════════════════════════════════════════════════
# 8/9. Enregistrement final
# ═════════════════════════════════════════════════════════════════════
log "8/9 · manifeste cluster écrit → ${CLUSTER_MANIFEST}"
history_append "cluster-deploy-success" "$TARGET_RELEASE" "nodes=${#ORDERED_LINES[@]} ; mig=${MIG_CLASS}"

cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  CLUSTER DEPLOYMENT SUCCESS
  Release    : ${TARGET_RELEASE}
  Git SHA    : ${GIT_SHA}
  Migration  : ${MIG_CLASS}
  Nodes (${#ORDERED_LINES[@]})   :
EOF
for r in "${CLUSTER_NODE_RESULTS[@]}"; do
  IFS='|' read -r nm hs rl st _ <<<"$r"
  printf '    - %-16s %-16s roles=%-20s %s\n' "$nm" "$hs" "$rl" "$st"
done
cat <<EOF
  Manifest   : ${CLUSTER_MANIFEST}
╚══════════════════════════════════════════════════════════════════╝
EOF
log "9/9 · terminé."
