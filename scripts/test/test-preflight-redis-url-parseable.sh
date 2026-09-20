#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-preflight-redis-url-parseable.sh — régression sur le
# nouveau garde-fou §4quinquies de scripts/preflight.sh (2026-09-20,
# incident réel production release sha-efc4ff8/sha-381321d) : REDIS_URL
# doit être parsable par `urllib.parse.SplitResult.port` — le même
# mécanisme stdlib que redis-py/kombu appellent en interne — AVANT tout
# déploiement.
#
# Incident réel : un mot de passe Valkey régénéré contenait un caractère
# qui cassait le parsing d'URL. `redis.from_url`/`kombu.parse_url`
# construisent leur client Redis AU NIVEAU MODULE (import-time) dans
# plusieurs fichiers (`api/routes/twilio_webhook.py`, `api/celery_app.py`)
# — API et worker crashaient TOUS LES DEUX au démarrage, y compris après
# un rollback vers une ANCIENNE release (même `.env` cassé, lu par
# n'importe quel code). Ce check aurait empêché le déploiement AVANT que
# quoi que ce soit ne soit touché.
#
# Couvre :
#   Cas A : REDIS_URL bien formée (mot de passe alphanumérique simple) —
#           acceptée.
#   Cas B : port non-numérique (caractère de mot de passe qui a débordé
#           dans le champ port, EXACTEMENT la classe de bug de l'incident)
#           — rejetée, avec seulement le NOM de l'exception affiché
#           (jamais l'URL ni le mot de passe).
#   Cas C : URL vide — ce check précis ne la rejette PAS lui-même (une
#           URL vide n'a pas de "port" invalide à proprement parler) ;
#           c'est le rôle des checks §4/§4ter (déjà existants, testés par
#           ailleurs) de rejeter une REDIS_URL absente/sans schéma. Ce cas
#           vérifie seulement que le check NE PLANTE PAS sur une entrée
#           vide (`set -uo pipefail` friendly).
#   Cas D : le message d'échec n'affiche JAMAIS le mot de passe.
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN="python"

_check() {
  # Reproduit exactement le §4quinquies de preflight.sh.
  REDIS_URL_TO_CHECK="$1" "$PYTHON_BIN" -c '
import os
import sys
from urllib.parse import urlsplit

url = os.environ.get("REDIS_URL_TO_CHECK", "")
try:
    parts = urlsplit(url)
    _ = parts.port
    _ = parts.hostname
except Exception as exc:
    print(type(exc).__name__, file=sys.stderr)
    sys.exit(1)
' 2>&1
}

echo "═══ Cas A : REDIS_URL bien formée — acceptée ═══"
if OUT="$(_check 'redis://:goodpass123@10.200.0.2:6379/0')"; then
  ok "URL bien formée acceptée"
else
  bad "aurait dû être acceptée : ${OUT}"
fi
echo

echo "═══ Cas B : port non-numérique (classe de bug de l'incident réel) — rejetée ═══"
if OUT="$(_check 'redis://:badpass@10.200.0.2:63a9/0')"; then
  bad "aurait dû être rejetée (port non-numérique)"
else
  if [ "$OUT" = "ValueError" ]; then
    ok "rejetée avec le nom d'exception attendu (ValueError), rien de plus affiché"
  else
    bad "rejetée mais avec un message inattendu : ${OUT}"
  fi
fi
echo

echo "═══ Cas C : URL vide — le check lui-même ne plante pas (rejet de l'absence délégué à §4/§4ter) ═══"
if _check '' >/tmp/pf_test_case_c.out 2>&1; then
  ok "URL vide : check exécuté sans planter (rc=0 — délibéré, voir §4/§4ter pour le rejet de l'absence)"
else
  RC=$?
  if [ "$RC" -eq 1 ]; then
    ok "URL vide : check exécuté sans planter (rejetée proprement, rc=1)"
  else
    bad "le check a planté de façon inattendue sur une entrée vide (rc=${RC})"
  fi
fi
rm -f /tmp/pf_test_case_c.out
echo

echo "═══ Cas D : le mot de passe n'apparaît JAMAIS dans la sortie d'échec ═══"
SECRET_MARKER="s3cr3tMarkerXYZ"
OUT="$(_check "redis://:${SECRET_MARKER}@10.200.0.2:63a9/0" || true)"
if printf '%s' "$OUT" | grep -q "$SECRET_MARKER"; then
  bad "le mot de passe est apparu dans la sortie du check : ${OUT}"
else
  ok "mot de passe absent de la sortie même en cas d'échec"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
