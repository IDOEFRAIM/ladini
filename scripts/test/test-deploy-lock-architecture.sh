#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-deploy-lock-architecture.sh — régression sur les
# verrous de déploiement (scripts/lib.sh, 2026-09-18, incident réel premier
# rollout Hetzner : auto-deadlock du runner self-hosted co-localisé avec le
# node qu'il déploie).
#
# Teste les PRIMITIVES de verrouillage directement (source scripts/lib.sh
# dans un sandbox isolé — LOCK_FILE/CLUSTER_LOCK_FILE/ENV_FILE surchargés
# AVANT le source, jamais le vrai deploy/ du dépôt) plutôt que d'exécuter
# cluster_deploy.sh/node_deploy.sh bout en bout (nécessiterait docker/ssh/un
# vrai registre — hors de portée d'un test unitaire de la logique de lock).
#
# Couvre :
#   Cas A : single-node — deux acquire_lock() concurrents sur LE MÊME
#           verrou : le second échoue tant que le premier tient.
#   Cas B : orchestrateur + node co-localisés (LE bug) — un acquire_cluster_lock
#           (mode local, dégradé) PUIS un acquire_lock (verrou node), dans le
#           MÊME process, ne doivent JAMAIS entrer en collision.
#   Cas C : double orchestration concurrente — deux acquire_cluster_lock()
#           avec la MÊME clé (même inventaire) : le second échoue.
#   Cas D : verrou node PÉRIMÉ (pid mort) — récupéré automatiquement, pas
#           d'erreur.
#   Cas E : orchestrateur qui a RELÂCHÉ son verrou node (fin des migrations
#           locales) puis un node_deploy.sh (même machine) qui l'acquiert à
#           son tour — doit réussir ; ET un DEUXIÈME acquéreur concurrent du
#           verrou NODE à ce moment-là doit toujours échouer (la protection
#           contre deux déploiements sur LE MÊME node reste réelle).
#   Cas F : résolution de REDIS_URL depuis $ENV_FILE quand non exportée —
#           valeur reprise, JAMAIS présente dans la sortie (log).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

# ── Sandbox : jamais le vrai deploy/ du dépôt ──────────────────────
export LOCK_FILE="${SANDBOX}/deploy/.deploy.lock"
export CLUSTER_LOCK_FILE="${SANDBOX}/deploy/.cluster-deploy.lock"
export ENV_FILE="${SANDBOX}/.env"
export RELEASES_DIR="${SANDBOX}/deploy/releases"
mkdir -p "${SANDBOX}/deploy"

_fresh_env() {
  # Chaque cas repart d'un état de verrous propre (les précédents ont pu
  # laisser des lockdir résiduels d'un test à l'autre).
  rm -rf "${SANDBOX}/deploy/.deploy.lockdir" "${SANDBOX}/deploy/.cluster-deploy.lockdir"
}

echo "═══ Cas A : single-node — deux acquire_lock() concurrents (le même verrou) ═══"
_fresh_env
(
  source "${ROOT}/scripts/lib.sh"
  acquire_lock
  sleep 5
) &
HOLDER_PID=$!
sleep 1
OUT_A="$(bash -c "source '${ROOT}/scripts/lib.sh'; acquire_lock" 2>&1)"
RC_A=$?
wait "$HOLDER_PID" 2>/dev/null
if [ "$RC_A" -ne 0 ] && grep -qi "autre déploiement.*est en cours" <<<"$OUT_A"; then
  ok "un second acquire_lock() échoue pendant que le premier tient le verrou"
else
  bad "le second acquire_lock() aurait dû échouer (rc=${RC_A}) : $OUT_A"
fi
echo

echo "═══ Cas B : orchestrateur + node co-localisés — LE bug (deadlock) ═══"
_fresh_env
OUT_B="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  acquire_cluster_lock 'test-inventory' 'cluster_deploy.sh (test)' 2>&1
  # Le 'node_deploy.sh' co-localisé acquiert le verrou NODE — MÊME process
  # ici (équivalent d'un node_deploy.sh lancé via SSH-vers-soi-même, sur le
  # même filesystem) : ne doit JAMAIS collisionner avec le verrou cluster
  # ci-dessus, qui est une ressource DIFFÉRENTE.
  acquire_lock 2>&1
  echo '__NODE_LOCK_OK__'
" 2>&1)"
if grep -q "__NODE_LOCK_OK__" <<<"$OUT_B" && ! grep -qi "autre déploiement.*est en cours" <<<"$OUT_B"; then
  ok "acquire_cluster_lock (local) PUIS acquire_lock, même process : aucune collision (deadlock corrigé)"
else
  bad "collision détectée entre le verrou cluster et le verrou node : $OUT_B"
fi
echo

echo "═══ Cas C : double orchestration concurrente (même clé d'inventaire) ═══"
_fresh_env
(
  source "${ROOT}/scripts/lib.sh"
  acquire_cluster_lock "test-inventory" "cluster_deploy.sh (test, holder)"
  sleep 5
) &
HOLDER_PID=$!
sleep 1
OUT_C="$(bash -c "source '${ROOT}/scripts/lib.sh'; acquire_cluster_lock 'test-inventory' 'cluster_deploy.sh (test, second)'" 2>&1)"
RC_C=$?
wait "$HOLDER_PID" 2>/dev/null
if [ "$RC_C" -ne 0 ] && grep -qi "est en cours" <<<"$OUT_C"; then
  ok "un second acquire_cluster_lock() concurrent (même inventaire) échoue"
else
  bad "le second acquire_cluster_lock() aurait dû échouer (rc=${RC_C}) : $OUT_C"
fi
echo

echo "═══ Cas D : verrou node PÉRIMÉ (pid mort) — récupération automatique ═══"
_fresh_env
DEAD_PID="$(bash -c 'echo $$')"   # ce sous-shell est déjà terminé : pid garanti mort
mkdir -p "${SANDBOX}/deploy/.deploy.lockdir"
echo "$DEAD_PID" >"${SANDBOX}/deploy/.deploy.lockdir/pid"
printf 'pid=%s user=test started=2020-01-01T00:00:00Z\n' "$DEAD_PID" >"${SANDBOX}/deploy/.deploy.lockdir/info"
OUT_D="$(bash -c "source '${ROOT}/scripts/lib.sh'; acquire_lock" 2>&1)"
RC_D=$?
if [ "$RC_D" -eq 0 ] && grep -qi "périmé" <<<"$OUT_D"; then
  ok "verrou périmé (pid mort) détecté et récupéré automatiquement"
else
  bad "la récupération de verrou périmé a échoué (rc=${RC_D}) : $OUT_D"
fi
echo

echo "═══ Cas E : orchestrateur relâche son verrou node avant le rollout SSH ═══"
_fresh_env
OUT_E1="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  acquire_lock            # migrations locales de l'orchestrateur
  release_lock             # relâché AVANT la boucle SSH (voir cluster_deploy.sh)
  echo '__RELEASED__'
" 2>&1)"
if grep -q "__RELEASED__" <<<"$OUT_E1"; then
  ok "l'orchestrateur relâche bien son verrou node après ses mutations locales"
else
  bad "échec du relâchement anticipé du verrou node : $OUT_E1"
fi
# Le verrou node est maintenant LIBRE : node_deploy.sh (même machine, via
# SSH-vers-soi) doit pouvoir l'acquérir normalement...
(
  source "${ROOT}/scripts/lib.sh"
  acquire_lock
  sleep 5
) &
HOLDER_PID=$!
sleep 1
# ...MAIS un deuxième acquéreur (un humain qui lance deploy.sh en direct
# PENDANT que node_deploy.sh tient encore ce même verrou) doit toujours être
# bloqué : la protection contre 2 déploiements sur LE MÊME node est réelle.
OUT_E2="$(bash -c "source '${ROOT}/scripts/lib.sh'; acquire_lock" 2>&1)"
RC_E2=$?
wait "$HOLDER_PID" 2>/dev/null
if [ "$RC_E2" -ne 0 ] && grep -qi "autre déploiement.*est en cours" <<<"$OUT_E2"; then
  ok "deux déploiements concurrents sur LE MÊME node restent mutuellement exclusifs"
else
  bad "la protection node-level a été perdue (rc=${RC_E2}) : $OUT_E2"
fi
echo

echo "═══ Cas F : résolution de REDIS_URL depuis \$ENV_FILE, jamais logguée ═══"
_fresh_env
SECRET_MARKER="s3cr3tSENTINEL$$"
printf 'REDIS_URL=rediss://user:%s@redis.example.internal:6379/0\n' "$SECRET_MARKER" >"$ENV_FILE"
OUT_F="$(bash -c "
  unset REDIS_URL
  source '${ROOT}/scripts/lib.sh'
  _resolve_redis_url_from_env_file
  # N'affiche la valeur nulle part dans CE test non plus — seulement une
  # preuve indirecte que la variable a bien été peuplée.
  [ -n \"\${REDIS_URL:-}\" ] && echo '__REDIS_URL_SET__'
" 2>&1)"
if grep -q "__REDIS_URL_SET__" <<<"$OUT_F"; then
  ok "REDIS_URL résolue depuis \$ENV_FILE quand non exportée par l'appelant"
else
  bad "REDIS_URL n'a pas été résolue depuis \$ENV_FILE : $OUT_F"
fi
if grep -qF "$SECRET_MARKER" <<<"$OUT_F"; then
  bad "LE SECRET REDIS_URL APPARAÎT EN CLAIR DANS LA SORTIE — ne doit JAMAIS être loggué"
else
  ok "le secret REDIS_URL n'apparaît jamais dans la sortie/les logs"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
