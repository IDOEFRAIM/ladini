#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/cluster_rollback.sh — ORCHESTRATEUR de rollback multi-node.
# Miroir de cluster_deploy.sh : lit le manifeste cluster écrit par le
# dernier cluster_deploy.sh, et fait revenir CHAQUE node concerné à sa
# release précédente (rollback APPLICATIF uniquement, JAMAIS la DB —
# §43, exactement comme scripts/rollback.sh au niveau d'un seul node).
#
#   ./scripts/cluster_rollback.sh [inventory_file]
#   (inventory_file par défaut : infra/inventory.yml — utilisé seulement
#    pour retrouver host/roles par nom de node ; la DÉCISION de qui
#    rollback vient du manifeste, pas de l'inventaire)
#
# Comme cluster_deploy.sh : UN NODE À LA FOIS, arrêt immédiat si un node
# échoue (pas de cascade — un node déjà rollback-é avec succès reste
# rollback-é, on ne le re-touche pas si un node suivant échoue).
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh"

INVENTORY_FILE="${1:-${LADINI_ROOT}/infra/inventory.yml}"
# (2026-09-18) §BUG CORRIGÉ : défaut incohérent avec cluster_deploy.sh
# (celui-ci pointait vers `/opt/ladini`, sans `/app` — cd échouerait sur le
# vrai chemin de checkout du node, `/opt/ladini/app`, voir .github/workflows/
# deploy.yml::DEPLOY_DIR et infra/providers/hetzner/cloud-init/app-node.yaml.tpl).
NODE_DEPLOY_DIR="${NODE_DEPLOY_DIR:-/opt/ladini/app}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
CLUSTER_MANIFEST="${RELEASES_DIR}/cluster-current.json"

[ -f "$CLUSTER_MANIFEST" ] || die "Aucun manifeste cluster trouvé (${CLUSTER_MANIFEST}) — jamais de cluster_deploy.sh exécuté sur cette machine ? Sans manifeste, ce script ne sait pas quels nodes ont changé de release ; rollback un node à la fois avec ./scripts/rollback.sh directement en SSH si besoin."

# ── Verrou CLUSTER-WIDE — implémentation PARTAGÉE avec cluster_deploy.sh,
# voir lib.sh (§DEADLOCK) pour le détail (ressource TOUJOURS distincte du
# verrou local par node, résolution REDIS_URL depuis $ENV_FILE, etc.). Même
# clé que cluster_deploy.sh (basée sur le nom de l'inventaire) : un deploy
# ET un rollback sur le MÊME inventaire s'excluent mutuellement, comme avant.
trap 'release_cluster_lock; release_all_locks' EXIT

acquire_cluster_lock "$(basename "$INVENTORY_FILE")" "cluster_rollback.sh"

# ── Lecture du manifeste (format écrit par cluster_deploy.sh, JSON minimal
# volontairement simple — même esprit que le parseur d'inventaire : pas de
# dépendance jq, juste grep/sed sur un format que NOUS contrôlons). ────
DESIRED_RELEASE="$(grep -o '"desired_release": *"[^"]*"' "$CLUSTER_MANIFEST" | head -n1 | sed 's/.*"\([^"]*\)"$/\1/')"
MIG_CLASS="$(grep -o '"migration_class": *"[^"]*"' "$CLUSTER_MANIFEST" | head -n1 | sed 's/.*"\([^"]*\)"$/\1/')"
log "Manifeste cluster : release désirée=${DESIRED_RELEASE:-?} migration=${MIG_CLASS:-?} (${CLUSTER_MANIFEST})"

if [ "$MIG_CLASS" = "MIGRATION_REQUIRES_MANUAL_RECOVERY" ]; then
  warn "════════════════════════════════════════════════════════════════"
  warn " Le dernier cluster_deploy.sh contenait une migration DB NON"
  warn " réversible automatiquement (MIGRATION_REQUIRES_MANUAL_RECOVERY)."
  warn " Rollback applicatif (code) SEUL, sur QUELQUE node que ce soit,"
  warn " NE restaurera PAS le schéma attendu par l'ancien code."
  warn " → suivez docs/runbooks/database-restore.md AVANT de continuer."
  warn "════════════════════════════════════════════════════════════════"
  if [ "${ROLLBACK_FORCE:-0}" != "1" ]; then
    die "Rollback cluster interrompu. Relancez avec ROLLBACK_FORCE=1 si vous savez que la DB est compatible avec le code précédent."
  fi
fi

# Nodes à traiter : ceux marqués "success" dans le manifeste (les
# "not-attempted"/"failed" n'ont jamais reçu la nouvelle release, donc rien
# à rollback dessus — ils tournent déjà sur l'ancien code). On les rollback
# dans l'ORDRE INVERSE du déploiement : le scheduler a été déployé EN
# DERNIER par cluster_deploy.sh (voir son commentaire §5/6/7) — donc, pour
# rendre le cluster cohérent le plus vite possible sur l'ancien code, on le
# fait redescendre EN PREMIER ici (c'est lui qui a le moins tourné sur la
# nouvelle release ⇒ le moins de raisons de s'y attarder, et remettre
# rapidement un scheduler connu-bon limite la fenêtre sans tâches
# planifiées cohérentes).
mapfile -t MANIFEST_NODE_LINES < <(
  grep -o '{"name": *"[^"]*", *"host": *"[^"]*", *"roles": *"[^"]*", *"status": *"[^"]*"[^}]*}' "$CLUSTER_MANIFEST" \
  | while IFS= read -r obj; do
      nm="$(printf '%s' "$obj" | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p')"
      hs="$(printf '%s' "$obj" | sed -n 's/.*"host": *"\([^"]*\)".*/\1/p')"
      rl="$(printf '%s' "$obj" | sed -n 's/.*"roles": *"\([^"]*\)".*/\1/p')"
      st="$(printf '%s' "$obj" | sed -n 's/.*"status": *"\([^"]*\)".*/\1/p')"
      [ "$st" = "success" ] && printf '%s\t%s\t%s\n' "$nm" "$hs" "$rl"
    done
)
[ "${#MANIFEST_NODE_LINES[@]}" -gt 0 ] || die "Aucun node marqué 'success' dans le manifeste — rien à rollback (le dernier cluster_deploy.sh n'a peut-être touché aucun node avec succès)."

ORDERED_LINES=()
SCHEDULER_LINE=""
for line in "${MANIFEST_NODE_LINES[@]}"; do
  IFS=$'\t' read -r _n _h roles <<<"$line"
  if [[ ",${roles}," == *",scheduler,"* ]]; then
    SCHEDULER_LINE="$line"
  else
    ORDERED_LINES+=("$line")
  fi
done
_final=()
[ -n "$SCHEDULER_LINE" ] && _final+=("$SCHEDULER_LINE")
_final+=("${ORDERED_LINES[@]}")
ORDERED_LINES=("${_final[@]}")

log "Rollback — ${#ORDERED_LINES[@]} node(s), un à la fois (scheduler en premier — voir commentaire ci-dessus)…"

ROLLBACK_FAILED=0
FAILED_NODE_NAME=""
for line in "${ORDERED_LINES[@]}"; do
  IFS=$'\t' read -r name host roles <<<"$line"
  log "   → node '${name}' (${host}) — rôles: ${roles}"

  if ssh "${SSH_OPTS[@]}" "$host" bash -s <<REMOTE_SCRIPT
set -Eeuo pipefail
cd "${NODE_DEPLOY_DIR}"
ROLLBACK_FORCE="${ROLLBACK_FORCE:-0}" ./scripts/rollback.sh
REMOTE_SCRIPT
  then
    log "   ✓ node '${name}' rollback OK"
  else
    err "   ✗ node '${name}' rollback ÉCHOUÉ — arrêt (les nodes suivants ne sont PAS touchés ; ceux déjà rollback-és restent rollback-és)."
    ROLLBACK_FAILED=1
    FAILED_NODE_NAME="$name"
    break
  fi
done

if [ "$ROLLBACK_FAILED" = 1 ]; then
  history_append "cluster-rollback-failed" "${DESIRED_RELEASE:-unknown}" "failed_node=${FAILED_NODE_NAME}"
  cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════════╗
  CLUSTER ROLLBACK FAILED
  Failed node : ${FAILED_NODE_NAME}
  → INCIDENT : intervenez manuellement sur ce node (docs/runbooks/incident.md).
    Les nodes traités AVANT lui dans cet ordre sont revenus avec succès ;
    ceux qui restaient APRÈS lui n'ont pas été touchés (toujours sur la
    release qui a échoué au déploiement).
╚══════════════════════════════════════════════════════════════════╝
EOF
  exit 1
fi

history_append "cluster-rollback-success" "${DESIRED_RELEASE:-unknown}" "nodes=${#ORDERED_LINES[@]}"
cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  CLUSTER ROLLBACK SUCCESS
  ${#ORDERED_LINES[@]} node(s) revenus à leur release précédente.
  Note : APP rollback only — la base de données n'a pas été modifiée.
╚══════════════════════════════════════════════════════════════════╝
EOF
