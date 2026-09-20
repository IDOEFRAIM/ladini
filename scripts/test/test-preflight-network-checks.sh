#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/test/test-preflight-network-checks.sh — régression sur les
# nouveaux garde-fous réseau de scripts/preflight.sh (2026-09-20, tunnel
# WireGuard Hetzner ↔ AWS Valkey) : §4quinquies (host:port REDIS_URL
# résolvable/joignable) et §4sexies (gate WireGuard optionnel).
#
# Même discipline que test-preflight-redis-url-checks.sh : extrait
# l'expression RÉELLE de preflight.sh plutôt que de la réimplémenter, et
# teste la primitive TCP directement (pas tout preflight.sh, hors de
# portée d'un test unitaire — dépendances docker/registry/disque/ports).
#
# Couvre :
#   Cas A : extraction host:port depuis REDIS_URL, tous schémas/formats
#           (redis://, rediss://, avec/sans mot de passe, localhost, IP).
#   Cas B : un port TCP réellement OUVERT est détecté joignable.
#   Cas C : un port TCP FERMÉ est détecté injoignable, sans faire planter
#           le script (`set -uo pipefail`, jamais `set -e` ici).
#   Cas D : WIREGUARD_REQUIRED absent/0 — preflight.sh n'appelle JAMAIS
#           check_valkey_network.sh (pas de régression sur une topologie
#           sans WireGuard).
#   Cas E : WIREGUARD_REQUIRED=1 — preflight.sh appelle bien
#           check_valkey_network.sh (assertion statique sur le code).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFLIGHT="${ROOT}/scripts/preflight.sh"

PASS=0; FAIL=0
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "═══ Cas A : extraction host:port depuis REDIS_URL (motif réel de preflight.sh) ═══"
EXTRACT_LINE="$(grep -F "sed -nE 's#^REDIS_URL=rediss?://" "$PREFLIGHT" | head -n1)"
if [ -z "$EXTRACT_LINE" ]; then
  bad "impossible de trouver la ligne d'extraction host:port dans preflight.sh — a-t-elle changé de forme ?"
else
  ok "ligne d'extraction trouvée dans preflight.sh"
fi
EXTRACT_PATTERN="$(printf '%s\n' "$EXTRACT_LINE" | sed -n "s/.*sed -nE '\(.*\)'.*/\1/p")"
_extract_host_port() {
  printf '%s' "$1" | sed -nE "$EXTRACT_PATTERN"
}
declare -A CASES=(
  ["REDIS_URL=redis://:pass123@10.200.0.2:6379/0"]="10.200.0.2 6379"
  ["REDIS_URL=rediss://default:pass@managed.example.com:6380/0"]="managed.example.com 6380"
  ["REDIS_URL=redis://localhost:6379/0"]="localhost 6379"
  ["REDIS_URL=redis://valkey-host:6379/0"]="valkey-host 6379"
)
for url in "${!CASES[@]}"; do
  expected="${CASES[$url]}"
  got="$(_extract_host_port "$url")"
  if [ "$got" = "$expected" ]; then
    ok "extraction correcte pour '$url' -> '$got'"
  else
    bad "extraction incorrecte pour '$url' : attendu '$expected', obtenu '$got'"
  fi
done
echo

echo "═══ Cas B/C : joignabilité TCP réelle (port ouvert vs fermé) ═══"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN="python"
TEST_PORT=16379
"$PYTHON_BIN" -c "
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', ${TEST_PORT}))
s.listen(5)
time.sleep(20)
" &
LISTENER_PID=$!
trap 'kill "$LISTENER_PID" 2>/dev/null || true' EXIT
sleep 1

if timeout 3 bash -c "exec 3<>\"/dev/tcp/127.0.0.1/${TEST_PORT}\"" 2>/dev/null; then
  ok "port TCP réellement ouvert détecté joignable (même primitive que preflight.sh §4quinquies)"
else
  bad "port TCP ouvert aurait dû être détecté joignable"
fi

CLOSED_PORT=16380
if timeout 3 bash -c "exec 3<>\"/dev/tcp/127.0.0.1/${CLOSED_PORT}\"" 2>/dev/null; then
  bad "port TCP fermé détecté à tort comme joignable"
else
  ok "port TCP fermé correctement détecté injoignable, sans faire planter le script"
fi
kill "$LISTENER_PID" 2>/dev/null || true
trap - EXIT
echo

echo "═══ Cas D : WIREGUARD_REQUIRED absent/0 — pas d'appel à check_valkey_network.sh ═══"
GATE_BLOCK="$(grep -A3 'WIREGUARD_REQUIRED:-0' "$PREFLIGHT")"
if printf '%s' "$GATE_BLOCK" | grep -q 'if \[ "\${WIREGUARD_REQUIRED:-0}" = "1" \]'; then
  ok "le gate WireGuard est bien conditionné sur WIREGUARD_REQUIRED=1 (défaut 0 = comportement inchangé)"
else
  bad "le gate WireGuard ne semble plus conditionné correctement — vérifier preflight.sh"
fi
echo

echo "═══ Cas E : WIREGUARD_REQUIRED=1 — preflight.sh appelle check_valkey_network.sh ═══"
if grep -q 'check_valkey_network\.sh' "$PREFLIGHT"; then
  ok "preflight.sh référence bien check_valkey_network.sh dans le gate WireGuard"
else
  bad "preflight.sh ne référence plus check_valkey_network.sh — régression du §4sexies"
fi
echo

echo "═══ Résumé ═══"
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
