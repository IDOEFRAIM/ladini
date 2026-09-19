#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/scale_in_drain.sh — DRAIN explicite d'un node AVANT scale-in
# (chantier Hetzner scale-out, audit 2026-09-19).
#
# Terraform n'a aucune notion de "drain" : décrémenter `app_node_count`
# planifie directement la DESTRUCTION du node de plus haut index/clé — sans
# égard pour un trafic ou une tâche Celery en cours dessus. Ce script fait
# la partie "volontaire et sûre" demandée AVANT ce `terraform apply` :
#
#   1. Refuse de draîner un node qui porte le rôle `scheduler` (singleton —
#      voir scripts/validate_inventory.py) : le retirer nécessite d'abord
#      de RÉASSIGNER ce rôle ailleurs, une décision humaine, jamais un
#      effet de bord de ce script.
#   2. Arrête `api` en premier (SSH, `docker compose stop`, respecte le
#      `stop_grace_period` du service — voir docker-compose.prod.yml) :
#      `/health/ready` échoue aussitôt → le Load Balancer Hetzner cesse de
#      router du NOUVEAU trafic vers ce node dès son prochain health check
#      (interval/retries — voir infra/providers/hetzner/load_balancer.tf),
#      pendant que les requêtes DÉJÀ en cours terminent dans leur grace
#      period.
#   3. Attend une marge de sécurité pour laisser le Load Balancer constater
#      l'échec et retirer le target du pool actif.
#   4. Arrête `mcp` puis `worker` (dans cet ordre — `worker` en dernier,
#      grace period la plus longue, pour laisser les tâches Celery déjà
#      dispatchées vers ce node se terminer plutôt que d'être coupées net).
#
#   ./scripts/scale_in_drain.sh <node_name> [inventory_file]
#   (ex: ./scripts/scale_in_drain.sh ladini-app-2)
#
# Ne touche JAMAIS Terraform ni infra/inventory.yml — volontairement séparé
# (voir la procédure complète, README de infra/providers/hetzner) :
#   1. CE script (drain applicatif)
#   2. `app_node_count` -= 1 dans environments/production.tfvars
#   3. `terraform plan` (vérifier qu'IL NE PLANIFIE QUE la destruction de
#      CE node précis — hcloud_server.app["N"]/hcloud_load_balancer_target
#      .app["N"], jamais un autre node)
#   4. `terraform apply`
#   5. `python scripts/generate_inventory.py` (le node retiré disparaît de
#      infra/inventory.yml automatiquement, puisqu'il n'existe plus dans
#      le state Terraform)
# ═════════════════════════════════════════════════════════════════════
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LADINI_ROOT="$(cd "${HERE}/.." && pwd)"

NODE_NAME="${1:-}"
INVENTORY_FILE="${2:-${LADINI_ROOT}/infra/inventory.yml}"
[ -n "$NODE_NAME" ] || {
  echo "usage: $0 <node_name> [inventory_file]  (ex: ladini-app-2)" >&2
  exit 1
}
[ -f "$INVENTORY_FILE" ] || {
  echo "[scale_in_drain] inventaire introuvable : ${INVENTORY_FILE}" >&2
  exit 1
}

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

# ── Recherche du node — MIROIR du parseur restreint (voir
# scripts/validate_inventory.py / scripts/cluster_deploy.sh::parse_inventory
# pour le même sous-ensemble YAML, volontairement dupliqué en awk plutôt
# qu'une dépendance PyYAML — cohérence avec les deux autres parseurs déjà
# dans ce dépôt). ─────────────────────────────────────────────────────
MATCH="$(
  sed 's/#.*//' "$INVENTORY_FILE" | awk -v want="$NODE_NAME" '
    /^[[:space:]]*-[[:space:]]*name:/ {
      if (name != "" && name == want) print name "\t" host "\t" roles
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
    END { if (name != "" && name == want) print name "\t" host "\t" roles }
  '
)"

[ -n "$MATCH" ] || {
  echo "[scale_in_drain] node '${NODE_NAME}' introuvable dans ${INVENTORY_FILE}." >&2
  exit 1
}

IFS=$'\t' read -r name host roles <<<"$MATCH"

if [[ ",${roles}," == *",scheduler,"* ]]; then
  cat >&2 <<EOF
[scale_in_drain] REFUS : '${name}' porte le rôle 'scheduler' (Celery Beat).

Ce rôle est un SINGLETON (scripts/validate_inventory.py) — le retirer sans
plan explicite laisserait le cluster SANS AUCUNE tâche planifiée. Avant de
draîner ce node :
  1. Décidez où Beat doit vivre ensuite (un autre node app existant, ou un
     node scheduler dédié via 'scheduler_on_dedicated_node = true').
  2. Redéployez avec le rôle 'scheduler' déjà transféré (inventaire +
     application) et confirmez qu'UN SEUL node porte 'scheduler'.
  3. Relancez ensuite ce script sur '${name}' une fois qu'il ne porte plus
     que 'app' (ou n'est plus dans le pool du tout).

AUCUNE action n'a été effectuée.
EOF
  exit 1
fi

echo "[scale_in_drain] node cible : ${name} (${host}) — rôles: ${roles}"
echo "[scale_in_drain] 1/3 · arrêt de 'api' (LB commence à voir /health/ready échouer)…"
ssh "${SSH_OPTS[@]}" "$host" 'cd /opt/ladini/app && docker compose -f docker-compose.prod.yml --env-file .env stop api' \
  || { echo "[scale_in_drain] échec SSH/arrêt de 'api' sur ${host} — RIEN d'autre n'a été arrêté." >&2; exit 1; }

DRAIN_WAIT="${DRAIN_WAIT:-45}"
echo "[scale_in_drain] 2/3 · attente ${DRAIN_WAIT}s (marge pour que le Load Balancer retire ce target du pool actif — interval×retries, voir load_balancer.tf)…"
sleep "$DRAIN_WAIT"

echo "[scale_in_drain] 3/3 · arrêt de 'mcp' puis 'worker' (grace period la plus longue en dernier, tâches Celery en cours laissées terminer)…"
ssh "${SSH_OPTS[@]}" "$host" 'cd /opt/ladini/app && docker compose -f docker-compose.prod.yml --env-file .env stop mcp worker' \
  || { echo "[scale_in_drain] échec SSH/arrêt de 'mcp'/'worker' sur ${host} — 'api' est déjà arrêté sur ce node, intervention manuelle recommandée." >&2; exit 1; }

cat <<EOF

╔══════════════════════════════════════════════════════════════════╗
  NODE DRAINÉ : ${name} (${host})
  Il ne sert plus AUCUN trafic HTTP et ne consomme plus de tâches Celery.

  Prochaines étapes (VOLONTAIRES — rien n'est automatique au-delà d'ici) :
    1. Décrémentez app_node_count dans environments/production.tfvars.
    2. terraform plan  — vérifiez qu'il ne détruit QUE les ressources de
       '${name}' (hcloud_server.app[...] / hcloud_load_balancer_target
       .app[...] correspondants), 0 autre changement inattendu.
    3. terraform apply
    4. python scripts/generate_inventory.py
    5. python scripts/validate_inventory.py
╚══════════════════════════════════════════════════════════════════╝
EOF
