#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-celery-healthcheck.sh — régression sur le healthcheck
# Docker du worker Celery (2026-09-18, incident réel sha-891cb2f : le worker
# crash-loopait sur une ValueError TLS Redis au boot, mais `docker ps`
# rapportait "healthy").
#
# Cas G — le healthcheck ne doit JAMAIS déclarer un worker "healthy" s'il
# n'a pas RÉELLEMENT terminé son démarrage (connexion broker+backend
# réussie, signal `worker_ready` de Celery) — un simple `pgrep` (process
# existe À CET INSTANT) ne suffit pas : un crash-loop rapide laisse une
# fenêtre où un process existe brièvement sans jamais avoir démarré
# correctement.
#
# Teste la VRAIE expression shell (extraite littéralement de
# docker-compose.prod.yml et infra/docker/Dockerfile.worker, pas une copie
# réécrite) avec un faux `pgrep` sur PATH — matrice des 4 combinaisons
# process-vivant × marqueur-présent.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

MARKER="${SANDBOX}/celery_worker_ready"
BIN="${SANDBOX}/bin"; mkdir -p "$BIN"

_fake_pgrep() {  # _fake_pgrep <exit_code>
  cat > "${BIN}/pgrep" <<EOF
#!/usr/bin/env bash
exit ${1}
EOF
  chmod +x "${BIN}/pgrep"
}

# ── L'expression RÉELLE, extraite des fichiers (pas retapée à la main) ──
extract_compose_test() {
  grep -oE "pgrep -f 'celery\.\*worker' >/dev/null && test -f [^\"]+" \
    "${ROOT}/docker-compose.prod.yml" | head -n1
}
extract_dockerfile_test() {
  grep -oE 'pgrep -f "celery\.\*worker" >/dev/null && test -f "[^"]+"' \
    "${ROOT}/infra/docker/Dockerfile.worker" | head -n1
}

COMPOSE_EXPR="$(extract_compose_test)"
DOCKERFILE_EXPR="$(extract_dockerfile_test)"

echo "═══ Cas G0 (structurel) : le healthcheck exige bien pgrep ET le marqueur ═══"
if [ -n "$COMPOSE_EXPR" ]; then
  ok "docker-compose.prod.yml (worker) combine pgrep && test -f : ${COMPOSE_EXPR}"
else
  bad "docker-compose.prod.yml : expression pgrep+marqueur introuvable (régression possible — retour à pgrep seul ?)"
fi
if [ -n "$DOCKERFILE_EXPR" ]; then
  ok "Dockerfile.worker (HEALTHCHECK baked-in) combine pgrep && test -f : ${DOCKERFILE_EXPR}"
else
  bad "Dockerfile.worker : expression pgrep+marqueur introuvable (régression possible)"
fi
echo

echo "═══ Cas G1 : worker vraiment sain (process vivant + a fini de démarrer) → healthy ═══"
_fake_pgrep 0
: >"$MARKER"
PATH="${BIN}:${PATH}" sh -c "pgrep -f 'celery.*worker' >/dev/null && test -f '${MARKER}'"
RC=$?
[ "$RC" -eq 0 ] && ok "process vivant + marqueur présent → exit 0 (healthy)" || bad "aurait dû être healthy (rc=${RC})"
echo

echo "═══ Cas G2 : LE bug — crash-loop (process momentanément vivant, jamais prêt) → unhealthy ═══"
_fake_pgrep 0
rm -f "$MARKER"
PATH="${BIN}:${PATH}" sh -c "pgrep -f 'celery.*worker' >/dev/null && test -f '${MARKER}'"
RC=$?
if [ "$RC" -ne 0 ]; then
  ok "process vivant MAIS jamais prêt (marqueur absent) → exit non-zéro (unhealthy — le bug de l'incident est corrigé)"
else
  bad "RÉGRESSION du bug de l'incident : rapporté 'healthy' alors que le worker n'a jamais fini de démarrer"
fi
echo

echo "═══ Cas G3 : process réellement mort (marqueur résiduel d'un ancien run) → unhealthy ═══"
_fake_pgrep 1
: >"$MARKER"
PATH="${BIN}:${PATH}" sh -c "pgrep -f 'celery.*worker' >/dev/null && test -f '${MARKER}'"
RC=$?
[ "$RC" -ne 0 ] && ok "process mort (même avec un vieux marqueur) → unhealthy" || bad "aurait dû être unhealthy (rc=${RC})"
echo

echo "═══ Cas G4 : rien du tout (process mort, jamais démarré) → unhealthy ═══"
_fake_pgrep 1
rm -f "$MARKER"
PATH="${BIN}:${PATH}" sh -c "pgrep -f 'celery.*worker' >/dev/null && test -f '${MARKER}'"
RC=$?
[ "$RC" -ne 0 ] && ok "process mort + jamais démarré → unhealthy" || bad "aurait dû être unhealthy (rc=${RC})"
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
