#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-node-preflight-lock.sh — régression sur le DEUXIÈME
# auto-deadlock trouvé sur node_deploy.sh (2026-09-18, incident réel post-
# premier-fix cluster/node) : preflight.sh tournait APRÈS acquire_lock()
# dans node_deploy.sh, détectait le verrou que CE MÊME process venait de
# poser, et le signalait comme "déploiement concurrent" — échec sur son
# propre verrou (log réel : "pas de verrou de déploiement actif ✗ ... pid=
# <celui du node_deploy appelant> ... label=déploiement (node)").
#
# Fix (Option A retenue) : preflight.sh (non-mutant) tourne désormais
# TOUJOURS avant acquire_lock() dans node_deploy.sh. lib.sh::acquire_lock
# reste l'arbitre RÉEL de la concurrence ; preflight.sh::deploy_lock_status
# reste un DIAGNOSTIC pur, jamais une action sur le verrou lui-même.
#
# Teste la fonction RÉELLE de production (lib.sh::deploy_lock_status,
# exactement celle appelée par preflight.sh check 9) plutôt que d'exécuter
# preflight.sh en entier, qui a des dépendances réseau/registry (docker
# manifest inspect) hors de portée d'un test unitaire de la logique de lock
# — voir scripts/test/test-predeploy-check-minimal-host.sh pour la même
# discipline ailleurs dans ce dépôt.
#
# Cas G — exactement la séquence demandée :
#   G1 : séquence normale d'UN node_deploy — deploy_lock_status()==free
#        AVANT acquire_lock() (donc son propre preflight n'échoue jamais
#        sur son propre verrou, par construction de l'ordre) ; acquire_lock
#        réussit ; la mutation (simulée) peut commencer.
#   G2 : un DEUXIÈME node_deploy, VRAIMENT concurrent (le premier tient déjà
#        le verrou) — son acquire_lock() échoue. C'est lui, pas preflight,
#        qui arbitre la concurrence (voir brief).
#   G3 : preflight.sh lancé MANUELLEMENT pendant qu'un verrou NODE externe
#        est actif (un `deploy_lock_status` sur ce même LOCK_DIR, appelé
#        depuis un contexte SÉPARÉ du détenteur) doit signaler "active",
#        exactement comme check 9 de preflight.sh le ferait.
#   G4 (structurel) : node_deploy.sh appelle bien preflight.sh AVANT
#        acquire_lock — assertion statique sur l'ORDRE des lignes, pour
#        empêcher toute régression future de cet ordre précis.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

export LOCK_FILE="${SANDBOX}/deploy/.deploy.lock"
export CLUSTER_LOCK_FILE="${SANDBOX}/deploy/.cluster-deploy.lock"
mkdir -p "${SANDBOX}/deploy"

echo "═══ Cas G4 (structurel) : preflight.sh AVANT acquire_lock dans node_deploy.sh ═══"
NODE_DEPLOY="${ROOT}/scripts/node_deploy.sh"
PREFLIGHT_LINE="$(grep -n 'preflight\.sh"' "$NODE_DEPLOY" | grep -v '^\s*#' | head -n1 | cut -d: -f1)"
LOCK_LINE="$(grep -n '^acquire_lock$' "$NODE_DEPLOY" | head -n1 | cut -d: -f1)"
if [ -n "$PREFLIGHT_LINE" ] && [ -n "$LOCK_LINE" ] && [ "$PREFLIGHT_LINE" -lt "$LOCK_LINE" ]; then
  ok "preflight.sh (ligne ${PREFLIGHT_LINE}) appelé AVANT acquire_lock (ligne ${LOCK_LINE})"
else
  bad "ordre incorrect ou introuvable — preflight=${PREFLIGHT_LINE:-?} lock=${LOCK_LINE:-?} (RÉGRESSION du bug 2026-09-18)"
fi
echo

echo "═══ Cas G1 : séquence normale — preflight (diagnostic) puis lock puis mutation ═══"
rm -rf "${SANDBOX}/deploy/.deploy.lockdir"
OUT_G1="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  status=\$(deploy_lock_status \"\$LOCK_DIR\")
  echo \"status_before_lock=\${status}\"
  acquire_lock
  echo 'lock_acquired=1'
  touch '${SANDBOX}/mutation.marker'   # simule 'docker compose up'
  echo 'mutation_done=1'
" 2>&1)"
if grep -q "status_before_lock=free" <<<"$OUT_G1" \
   && grep -q "lock_acquired=1" <<<"$OUT_G1" \
   && grep -q "mutation_done=1" <<<"$OUT_G1" \
   && [ -f "${SANDBOX}/mutation.marker" ]; then
  ok "preflight (deploy_lock_status) voit 'free' AVANT son propre acquire_lock — jamais son propre verrou"
else
  bad "séquence normale cassée : $OUT_G1"
fi
rm -f "${SANDBOX}/mutation.marker"
echo

echo "═══ Cas G2 : DEUXIÈME node_deploy vraiment concurrent — échoue sur le verrou ═══"
rm -rf "${SANDBOX}/deploy/.deploy.lockdir"
(
  source "${ROOT}/scripts/lib.sh"
  acquire_lock
  sleep 5
) &
HOLDER_PID=$!
sleep 1
OUT_G2="$(bash -c "source '${ROOT}/scripts/lib.sh'; acquire_lock" 2>&1)"
RC_G2=$?
wait "$HOLDER_PID" 2>/dev/null
if [ "$RC_G2" -ne 0 ] && grep -qi "autre déploiement.*est en cours" <<<"$OUT_G2"; then
  ok "le deuxième node_deploy concurrent échoue sur acquire_lock() — c'est LUI l'arbitre, pas preflight"
else
  bad "le deuxième node_deploy aurait dû échouer sur le verrou (rc=${RC_G2}) : $OUT_G2"
fi
echo

echo "═══ Cas G3 : preflight.sh lancé MANUELLEMENT pendant qu'un lock externe est actif ═══"
rm -rf "${SANDBOX}/deploy/.deploy.lockdir"
(
  source "${ROOT}/scripts/lib.sh"
  acquire_lock
  sleep 5
) &
HOLDER_PID=$!
sleep 1
# Un OPÉRATEUR (process séparé, PAS le détenteur du verrou) lance
# preflight.sh à la main — la fonction que check 9 appelle doit signaler
# "active" : un déploiement tourne réellement ailleurs.
STATUS_G3="$(bash -c "source '${ROOT}/scripts/lib.sh'; deploy_lock_status \"\$LOCK_DIR\"" 2>&1)"
wait "$HOLDER_PID" 2>/dev/null
if [ "$STATUS_G3" = "active" ]; then
  ok "un verrou EXTERNE réellement actif est bien signalé 'active' par deploy_lock_status (check 9 de preflight.sh bloquerait correctement)"
else
  bad "un verrou externe actif aurait dû être détecté 'active', obtenu : '${STATUS_G3}'"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
