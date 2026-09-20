#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/check_valkey_network.sh — valide le tunnel WireGuard Hetzner ↔
# AWS ET l'accessibilité Valkey au bout, de bout en bout (migration
# Upstash → Valkey, 2026-09-20). Voir infra/wireguard/README.md.
#
#   sudo bash scripts/check_valkey_network.sh
#
# Vérifie, dans l'ordre :
#   1. l'interface wg0 existe
#   2. au moins un peer est configuré
#   3. le dernier handshake est récent (< HANDSHAKE_MAX_AGE_SECONDS, 180s
#      par défaut — un handshake vieux ou absent = tunnel mort)
#   4. ping ICMP vers l'IP tunnel Valkey (best-effort — un ICMP bloqué ne
#      fait PAS échouer le script, seulement un avertissement : de
#      nombreux réseaux filtrent ICMP sans que TCP soit affecté)
#   5. TCP joignable sur VALKEY_TUNNEL_IP:VALKEY_PORT
#   6. `PING` Valkey authentifié réussit (PONG)
#
# Sortie : 0 si TOUT (sauf l'ICMP best-effort) est vert, non-zéro sinon.
#
# N'affiche JAMAIS REDIS_URL ni le mot de passe — ni en clair, ni dans un
# message d'erreur, ni via la ligne de commande du client Valkey (utilise
# REDISCLI_AUTH, jamais `-a <password>` sur la ligne de commande, pour ne
# pas exposer le secret dans `ps aux` sur cette même machine).
#
# Variables d'environnement :
#   VALKEY_TUNNEL_IP           défaut 10.200.0.2 (voir infra/wireguard/README.md)
#   VALKEY_PORT                défaut 6379
#   VALKEY_PASSWORD            si absent, extrait au mieux de REDIS_URL dans $ENV_FILE
#   HANDSHAKE_MAX_AGE_SECONDS  défaut 180
#   WG_INTERFACE               défaut wg0
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${HERE}/lib.sh" 2>/dev/null || ENV_FILE="${ENV_FILE:-${HERE}/../.env}"

WG_INTERFACE="${WG_INTERFACE:-wg0}"
VALKEY_TUNNEL_IP="${VALKEY_TUNNEL_IP:-10.200.0.2}"
VALKEY_PORT="${VALKEY_PORT:-6379}"
HANDSHAKE_MAX_AGE_SECONDS="${HANDSHAKE_MAX_AGE_SECONDS:-180}"
FAIL=0

ok()  { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
bad() { printf '  \033[1;31m✗\033[0m %s\n' "$1"; FAIL=1; }
warn(){ printf '  \033[1;33m?\033[0m %s\n' "$1"; }

echo "═══ 1/6 : interface ${WG_INTERFACE} ═══"
if command -v wg >/dev/null 2>&1 && wg show "$WG_INTERFACE" >/dev/null 2>&1; then
  ok "interface ${WG_INTERFACE} présente"
else
  bad "interface ${WG_INTERFACE} absente ou 'wg' non installé — voir scripts/wireguard_setup_hetzner.sh"
fi
echo

echo "═══ 2/6 : peer configuré ═══"
PEER_COUNT=0
if command -v wg >/dev/null 2>&1; then
  PEER_COUNT="$(wg show "$WG_INTERFACE" peers 2>/dev/null | grep -c . || true)"
fi
if [ "${PEER_COUNT:-0}" -ge 1 ]; then
  ok "${PEER_COUNT} peer(s) configuré(s)"
else
  bad "aucun peer configuré sur ${WG_INTERFACE}"
fi
echo

echo "═══ 3/6 : handshake récent (< ${HANDSHAKE_MAX_AGE_SECONDS}s) ═══"
if command -v wg >/dev/null 2>&1 && [ "${PEER_COUNT:-0}" -ge 1 ]; then
  LAST_HANDSHAKE="$(wg show "$WG_INTERFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | head -n1)"
  NOW="$(date +%s)"
  if [ -n "${LAST_HANDSHAKE:-}" ] && [ "$LAST_HANDSHAKE" -gt 0 ] 2>/dev/null; then
    AGE=$((NOW - LAST_HANDSHAKE))
    if [ "$AGE" -le "$HANDSHAKE_MAX_AGE_SECONDS" ]; then
      ok "dernier handshake il y a ${AGE}s"
    else
      bad "dernier handshake trop ancien (${AGE}s > ${HANDSHAKE_MAX_AGE_SECONDS}s) — tunnel probablement mort"
    fi
  else
    bad "aucun handshake jamais établi — vérifier Security Group AWS (UDP 51820) et la config des deux côtés"
  fi
else
  bad "impossible de vérifier le handshake (wg absent ou aucun peer)"
fi
echo

echo "═══ 4/6 : ping ICMP vers ${VALKEY_TUNNEL_IP} (best-effort) ═══"
if command -v ping >/dev/null 2>&1; then
  if ping -c 2 -W 2 "$VALKEY_TUNNEL_IP" >/dev/null 2>&1; then
    ok "ping ICMP OK"
  else
    warn "ping ICMP échoué (non bloquant — de nombreux réseaux filtrent ICMP sans affecter TCP)"
  fi
else
  warn "'ping' indisponible — étape sautée (non bloquant)"
fi
echo

echo "═══ 5/6 : TCP ${VALKEY_TUNNEL_IP}:${VALKEY_PORT} joignable ═══"
if (exec 3<>"/dev/tcp/${VALKEY_TUNNEL_IP}/${VALKEY_PORT}") 2>/dev/null; then
  exec 3<&- 3>&- 2>/dev/null || true
  ok "TCP joignable"
else
  bad "TCP ${VALKEY_TUNNEL_IP}:${VALKEY_PORT} injoignable — tunnel up mais Valkey down, ou bind/Security Group/firewall hôte incorrect"
fi
echo

echo "═══ 6/6 : PING Valkey authentifié ═══"
VALKEY_PASSWORD="${VALKEY_PASSWORD:-}"
if [ -z "$VALKEY_PASSWORD" ] && [ -f "${ENV_FILE:-}" ]; then
  # Extraction best-effort depuis REDIS_URL=redis://:PASSWORD@HOST:PORT/DB —
  # jamais affichée, seulement utilisée en mémoire pour authentifier ce test.
  redis_url_line="$(grep -E '^REDIS_URL=' "$ENV_FILE" 2>/dev/null | tail -n1)"
  VALKEY_PASSWORD="$(printf '%s' "$redis_url_line" | sed -nE 's#^REDIS_URL=rediss?://:?([^@]*)@.*#\1#p')"
fi

CLI_BIN=""
for candidate in valkey-cli redis-cli; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CLI_BIN="$candidate"
    break
  fi
done

if [ -z "$CLI_BIN" ]; then
  bad "ni valkey-cli ni redis-cli disponible sur cette machine — impossible de valider PING (installer redis-tools ou valkey-tools)"
elif [ -z "$VALKEY_PASSWORD" ]; then
  bad "mot de passe Valkey introuvable (ni VALKEY_PASSWORD, ni REDIS_URL exploitable dans \$ENV_FILE) — impossible de valider PING authentifié"
else
  # REDISCLI_AUTH plutôt que `-a` sur la ligne de commande : évite que le
  # mot de passe apparaisse dans `ps aux` sur cette même machine pendant
  # l'exécution de la commande.
  PING_OUT="$(REDISCLI_AUTH="$VALKEY_PASSWORD" "$CLI_BIN" -h "$VALKEY_TUNNEL_IP" -p "$VALKEY_PORT" --no-auth-warning ping 2>&1)"
  if [ "$PING_OUT" = "PONG" ]; then
    ok "PONG reçu (authentification OK)"
  else
    bad "PING a échoué (réponse : ${PING_OUT})"
  fi
fi
unset VALKEY_PASSWORD PING_OUT redis_url_line
echo

echo "═══ Résumé ═══"
if [ "$FAIL" -eq 0 ]; then
  echo "OK — tunnel WireGuard + Valkey accessibles et authentifiés via ${VALKEY_TUNNEL_IP}:${VALKEY_PORT}."
  exit 0
else
  echo "ÉCHEC — au moins une vérification a échoué (détails ci-dessus, jamais de secret affiché)."
  exit 1
fi
