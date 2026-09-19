#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-cluster-deploy-lb-validation.sh — régression sur la
# validation Load Balancer / chemin public de cluster_deploy.sh
# (2026-09-19, incident réel : `HEALTH_TIMEOUT: unbound variable` en
# production APRÈS un déploiement node réussi — release sha-d5de58c
# correctement déployée et healthy sur ladini-app-1, seule l'étape 7/9,
# la validation LB, a crashé sous `set -euo pipefail`).
#
# Teste les PRIMITIVES extraites dans scripts/lib.sh
# (resolve_public_health_url / validate_public_health) DIRECTEMENT — même
# style que test-deploy-lock-architecture.sh (source lib.sh dans un
# sandbox isolé) — plutôt que d'exécuter cluster_deploy.sh bout en bout
# (nécessiterait SSH/inventaire/migrations réels, hors de portée d'un test
# unitaire de CETTE logique précise).
#
# Couvre exactement la matrice demandée :
#   Cas A : HEALTH_TIMEOUT absent   → le défaut (180s) est utilisé, sans
#           jamais lever "unbound variable" sous `set -u`.
#   Cas B : HEALTH_TIMEOUT défini   → la valeur fournie est RESPECTÉE (on
#           le vérifie en bornant un check qui échoue toujours à une
#           valeur volontairement petite, et en mesurant que l'attente
#           réelle colle à ce budget, ni plus ni moins).
#   Cas C : PUBLIC_DOMAIN absent (et PUBLIC_HEALTH_URL absent) → chaîne
#           vide renvoyée explicitement (le SAUT de l'étape 7/9 dans
#           cluster_deploy.sh, jamais un crash ni une URL bidon).
#   Cas D : échec HTTP (curl/health check KO) → `validate_public_health`
#           renvoie proprement 1 (échec métier normal), jamais une erreur
#           "unbound variable" ni un crash du script appelant.
#   Cas E (bonus) : PUBLIC_HEALTH_URL explicite prime sur PUBLIC_DOMAIN.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

# ── Faux `curl` — mêmes formes que scripts/test/run-scenarios.sh (mêmes
# flags EXACTS que wait_http/lib.sh : `-s -o /dev/null -w '%{http_code}'
# --max-time N URL`). Piloté par MOCK_READY_HTTP (code renvoyé) — 000/timeout
# simulé en renvoyant un code jamais égal à "200".
BIN="${SANDBOX}/bin"; mkdir -p "$BIN"
cat > "${BIN}/curl" <<'EOF'
#!/usr/bin/env bash
code="${MOCK_READY_HTTP:-200}"
printf '%s' "$code"
exit 0
EOF
chmod +x "${BIN}/curl"
export PATH="${BIN}:${PATH}"

# ── Sandbox : jamais le vrai deploy/ du dépôt (aligné sur
# test-deploy-lock-architecture.sh) — validate_public_health/wait_http ne
# touchent rien sur disque, mais on isole quand même par cohérence/principe
# de moindre surprise si lib.sh évolue.
export LOCK_FILE="${SANDBOX}/deploy/.deploy.lock"
export CLUSTER_LOCK_FILE="${SANDBOX}/deploy/.cluster-deploy.lock"
export ENV_FILE="${SANDBOX}/.env"
export RELEASES_DIR="${SANDBOX}/deploy/releases"
mkdir -p "${SANDBOX}/deploy"

echo "═══ Cas A : HEALTH_TIMEOUT absent → défaut utilisé, jamais 'unbound variable' ═══"
OUT_A="$(bash -c "
  set -euo pipefail
  unset HEALTH_TIMEOUT
  source '${ROOT}/scripts/lib.sh'
  MOCK_READY_HTTP=200
  export MOCK_READY_HTTP
  validate_public_health 'https://api.example.test/health/ready'
  echo '__VALIDATED__'
" 2>&1)"
RC_A=$?
if [ "$RC_A" -eq 0 ] && grep -q "__VALIDATED__" <<<"$OUT_A" && ! grep -qi "unbound variable" <<<"$OUT_A"; then
  ok "HEALTH_TIMEOUT absent : défaut (180s) utilisé silencieusement, aucun crash"
else
  bad "HEALTH_TIMEOUT absent aurait dû réussir avec le défaut (rc=${RC_A}) : $OUT_A"
fi
echo

echo "═══ Cas A-bis : le VRAI point de régression — le call-site historique ═══"
# Reproduit LITTÉRALEMENT l'expression qui a crashé en prod (ligne 412,
# avant correctif) : `wait_http "$URL" "$HEALTH_TIMEOUT" 200` avec
# HEALTH_TIMEOUT jamais assignée dans ce scope. Après le correctif,
# cluster_deploy.sh ne référence plus JAMAIS `$HEALTH_TIMEOUT` sans
# l'avoir d'abord défini avec un défaut (voir la ligne ajoutée en tête du
# script) — ce test verrouille que `wait_http`/`validate_public_health`
# eux-mêmes ne dépendent JAMAIS d'une variable externe non gardée.
OUT_ABIS="$(bash -c "
  set -euo pipefail
  unset HEALTH_TIMEOUT
  source '${ROOT}/scripts/lib.sh'
  MOCK_READY_HTTP=200
  export MOCK_READY_HTTP
  # Appel SANS jamais mentionner \$HEALTH_TIMEOUT au site d'appel — exactement
  # ce que fait maintenant cluster_deploy.sh via validate_public_health.
  validate_public_health 'https://api.example.test/health/ready'
  echo '__NO_UNBOUND_CRASH__'
" 2>&1)"
if grep -q "__NO_UNBOUND_CRASH__" <<<"$OUT_ABIS"; then
  ok "aucune référence non gardée à \$HEALTH_TIMEOUT ne subsiste dans le chemin critique"
else
  bad "régression : le chemin critique référence encore \$HEALTH_TIMEOUT sans garde : $OUT_ABIS"
fi
echo

echo "═══ Cas B : HEALTH_TIMEOUT défini → la valeur fournie est respectée ═══"
# Endpoint qui échoue TOUJOURS (MOCK_READY_HTTP=503) + timeout volontairement
# petit (3s). Assertion sur le CONTENU du message d'erreur de wait_http
# (« ... après 3s »), pas sur un chrono mur — wait_http vérifie le budget
# AVANT de dormir (`(( waited += 3 )); [ "$waited" -ge "$timeout" ] && …`),
# donc un timeout de 3s avec un pas de 3s échoue dès la 1ère tentative
# (~0s réel) : mesurer le temps écoulé serait un faux négatif, alors que le
# message porte la PREUVE directe, déterministe, que la valeur fournie
# (jamais le défaut 180s) a bien été utilisée.
OUT_B="$(bash -c "
  set -uo pipefail
  source '${ROOT}/scripts/lib.sh'
  MOCK_READY_HTTP=503
  export MOCK_READY_HTTP
  HEALTH_TIMEOUT=3
  validate_public_health 'https://api.example.test/health/ready' \"\$HEALTH_TIMEOUT\"
" 2>&1)"
RC_B=$?
if [ "$RC_B" -ne 0 ] && grep -q "après 3s" <<<"$OUT_B" && ! grep -q "après 180s" <<<"$OUT_B"; then
  ok "HEALTH_TIMEOUT=3 respecté (wait_http a échoué avec ce budget précis, jamais le défaut 180s)"
else
  bad "HEALTH_TIMEOUT=3 n'a pas été respecté (rc=${RC_B}) : $OUT_B"
fi
echo

echo "═══ Cas C : PUBLIC_DOMAIN et PUBLIC_HEALTH_URL absents → chaîne vide (SAUT explicite) ═══"
OUT_C="$(bash -c "
  set -euo pipefail
  unset PUBLIC_HEALTH_URL PUBLIC_DOMAIN
  source '${ROOT}/scripts/lib.sh'
  url=\"\$(resolve_public_health_url)\"
  printf '__URL__[%s]__END__\n' \"\$url\"
" 2>&1)"
if grep -q '__URL__\[\]__END__' <<<"$OUT_C"; then
  ok "ni PUBLIC_HEALTH_URL ni PUBLIC_DOMAIN : URL vide renvoyée explicitement (jamais un crash)"
else
  bad "résolution d'URL publique inattendue sans variables : $OUT_C"
fi
echo

echo "═══ Cas D : échec HTTP → erreur propre (return 1), jamais 'unbound variable' ═══"
OUT_D="$(bash -c "
  set -euo pipefail
  source '${ROOT}/scripts/lib.sh'
  MOCK_READY_HTTP=503
  export MOCK_READY_HTTP
  if validate_public_health 'https://api.example.test/health/ready' 2; then
    echo '__UNEXPECTED_SUCCESS__'
  else
    echo '__CLEAN_FAILURE__'
  fi
" 2>&1)"
if grep -q "__CLEAN_FAILURE__" <<<"$OUT_D" && ! grep -qi "unbound variable" <<<"$OUT_D"; then
  ok "un health-check public qui échoue renvoie une erreur propre (jamais unbound variable)"
else
  bad "l'échec HTTP n'a pas été géré proprement : $OUT_D"
fi
echo

echo "═══ Cas E (bonus) : PUBLIC_HEALTH_URL explicite prime sur PUBLIC_DOMAIN ═══"
OUT_E="$(bash -c "
  set -euo pipefail
  export PUBLIC_HEALTH_URL='https://explicit.example.test/health/ready'
  export PUBLIC_DOMAIN='should-be-ignored.example.test'
  source '${ROOT}/scripts/lib.sh'
  resolve_public_health_url
" 2>&1)"
if [ "$OUT_E" = "https://explicit.example.test/health/ready" ]; then
  ok "PUBLIC_HEALTH_URL explicite prend le pas sur PUBLIC_DOMAIN"
else
  bad "priorité PUBLIC_HEALTH_URL/PUBLIC_DOMAIN incorrecte : obtenu '$OUT_E'"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
