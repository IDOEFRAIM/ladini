#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-celery-smoke.sh — régression sur lib.sh::celery_worker_ping
# (2026-09-18, audit smoke Celery — incident réel sha-6af07e0 : "Celery
# worker ne répond pas au broker" alors que rien ne prouvait le worker
# réellement KO — le smoke test avalait `>/dev/null 2>&1` le message
# diagnostique réel de Celery, rendant la cause indistinguable).
#
# Exerce la VRAIE fonction de production (lib.sh::celery_worker_ping),
# jamais une copie — en substituant `dc` (déjà une indirection bash définie
# par lib.sh autour de `docker compose ...`) par un faux qui rejoue
# fidèlement les comportements RÉELS de `celery inspect ping` confirmés en
# lisant le code source de Celery 5.6 (celery/bin/control.py) :
#   - succès (≥1 réplique)      → exit 0, "pong" sur stdout
#   - aucune réplique           → exit non-zéro (EX_UNAVAILABLE), message
#     "No nodes replied within time constraint" sur stderr
#   - broker injoignable        → exit non-zéro, "Could not connect to the
#     message broker..." sur stderr
#   - app/commande invalide     → exit non-zéro, erreur d'import/commande
#
# Cas (demandés) :
#   A : worker joignable, broker OK                → PASS
#   B : worker absent/arrêté (aucune réplique)      → FAIL, retries épuisés
#   C : worker démarre mais broker injoignable      → FAIL, message broker
#   D : mauvaise app / mauvaise commande inspect    → FAIL, message erreur
#   E : réponse valide, nodename QUELCONQUE         → PASS, jamais de -d/
#       --destination envoyé (jamais lié à un hostname)
#   F : timeout de réponse worker (retries)         → FAIL propre + message,
#       ET le nombre de tentatives réellement effectué est vérifié (bornes
#       fail-closed, pas un `|| true` déguisé)
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

echo "═══ Cas A : worker joignable, broker OK → PASS ═══"
CALLS_A="${SANDBOX}/calls_a"
OUT_A="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  dc() {
    echo \"\$*\" >> '${CALLS_A}'
    echo '-> celery@worker-abc123: OK'
    printf '\tpong\n'
    return 0
  }
  celery_worker_ping worker ladini.api.celery_app 1 3 0
  echo \"RC=\$?\"
" 2>&1)"
if grep -q "RC=0" <<<"$OUT_A"; then
  ok "worker joignable + broker OK → celery_worker_ping réussit (RC=0)"
else
  bad "aurait dû réussir : $OUT_A"
fi
if [ "$(wc -l <"$CALLS_A" 2>/dev/null || echo 0)" -eq 1 ]; then
  ok "un seul essai nécessaire quand ça marche du premier coup (pas de retry inutile)"
else
  bad "nombre d'essais inattendu : $(cat "$CALLS_A" 2>/dev/null)"
fi
echo

echo "═══ Cas B : worker absent/arrêté (aucune réplique) → FAIL ═══"
CALLS_B="${SANDBOX}/calls_b"
OUT_B="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  dc() {
    echo \"\$*\" >> '${CALLS_B}'
    echo 'Error: No nodes replied within time constraint' >&2
    return 69
  }
  if celery_worker_ping worker ladini.api.celery_app 1 2 0; then echo \"RC=0\"; else echo \"RC=\$?\"; fi
" 2>&1)"
if grep -q "RC=1" <<<"$OUT_B"; then
  ok "aucune réplique → celery_worker_ping échoue (RC=1), jamais un faux PASS"
else
  bad "aurait dû échouer : $OUT_B"
fi
if grep -qi "no nodes replied" <<<"$OUT_B"; then
  ok "le message diagnostique réel de Celery est bien affiché (plus jamais avalé)"
else
  bad "le diagnostic Celery a disparu (régression de l'incident du swallow) : $OUT_B"
fi
echo

echo "═══ Cas C : worker démarre mais ne peut pas joindre le broker → FAIL ═══"
OUT_C="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  dc() {
    echo 'Error: Could not connect to the message broker. Reason: Error 111 connecting to redis.internal:6379. Connection refused.' >&2
    return 69
  }
  if celery_worker_ping worker ladini.api.celery_app 1 2 0; then echo \"RC=0\"; else echo \"RC=\$?\"; fi
" 2>&1)"
if grep -q "RC=1" <<<"$OUT_C" && grep -qi "could not connect to the message broker" <<<"$OUT_C"; then
  ok "broker injoignable → FAIL avec le message 'Could not connect to the message broker' visible (distinguable d'un worker mort)"
else
  bad "cas broker-injoignable mal géré : $OUT_C"
fi
echo

echo "═══ Cas D : mauvaise app Celery / mauvaise commande inspect → FAIL ═══"
OUT_D="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  dc() {
    echo \"Error: Unable to load celery application. The module 'ladini.api.wrong_app' was not found.\" >&2
    return 1
  }
  if celery_worker_ping worker ladini.api.wrong_app 1 2 0; then echo \"RC=0\"; else echo \"RC=\$?\"; fi
" 2>&1)"
if grep -q "RC=1" <<<"$OUT_D" && grep -qi "was not found" <<<"$OUT_D"; then
  ok "mauvaise app Celery → FAIL, erreur de chargement visible"
else
  bad "cas mauvaise app mal géré : $OUT_D"
fi
echo

echo "═══ Cas E : réponse valide, nodename QUELCONQUE → PASS, jamais lié à un hostname ═══"
CALLS_E="${SANDBOX}/calls_e"
OUT_E="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  dc() {
    echo \"\$*\" >> '${CALLS_E}'
    echo '-> celery@totalement-different-hostname-XYZ-42: OK'
    printf '\tpong\n'
    return 0
  }
  celery_worker_ping worker ladini.api.celery_app 1 3 0
  echo \"RC=\$?\"
" 2>&1)"
if grep -q "RC=0" <<<"$OUT_E"; then
  ok "réplique avec un nodename quelconque → PASS quand même"
else
  bad "aurait dû réussir malgré le nodename inattendu : $OUT_E"
fi
if grep -qE '\-\-destination|(^|[^a-zA-Z])-d[[:space:]]' "$CALLS_E" 2>/dev/null; then
  bad "la commande envoyée cible une destination précise (-d/--destination) — fragile, contraire à l'objectif broadcast"
else
  ok "aucun -d/--destination envoyé — le ping reste un broadcast, jamais lié à un hostname"
fi
echo

echo "═══ Cas F : timeout de réponse worker (retries épuisés) → FAIL propre ═══"
CALLS_F="${SANDBOX}/calls_f"
: >"$CALLS_F"
OUT_F="$(bash -c "
  source '${ROOT}/scripts/lib.sh'
  dc() {
    echo 1 >> '${CALLS_F}'
    echo 'Error: No nodes replied within time constraint' >&2
    return 69
  }
  if celery_worker_ping worker ladini.api.celery_app 1 3 0; then echo \"RC=0\"; else echo \"RC=\$?\"; fi
" 2>&1)"
ATTEMPTS_F="$(wc -l <"$CALLS_F" 2>/dev/null || echo 0)"
if grep -q "RC=1" <<<"$OUT_F"; then
  ok "timeout persistant → FAIL propre (pas de faux PASS après épuisement des essais)"
else
  bad "aurait dû échouer après épuisement des essais : $OUT_F"
fi
if [ "$ATTEMPTS_F" -eq 3 ]; then
  ok "exactement 3 tentatives effectuées (retries bornés — ni 1 seul essai fragile, ni boucle infinie)"
else
  bad "nombre de tentatives inattendu : ${ATTEMPTS_F} (attendu 3)"
fi
if grep -qi "no nodes replied within time constraint" <<<"$OUT_F"; then
  ok "message explicite affiché après épuisement des essais"
else
  bad "message d'échec manquant après épuisement des essais : $OUT_F"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
