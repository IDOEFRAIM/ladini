#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/configure_valkey_bind_wireguard.sh — configure `bind` dans
# valkey.conf sur l'IP WireGuard, et SEULEMENT si cette IP existe déjà
# réellement sur l'interface wg0 (2026-09-20, cutover Upstash → Valkey).
#
# Corrige un ordre dangereux identifié : configurer `bind 10.200.0.2
# 127.0.0.1` puis `restart` Valkey AVANT que wg0 soit UP avec cette
# adresse ferait échouer le `bind()` au démarrage (adresse inexistante sur
# aucune interface) — Valkey resterait alors soit DOWN, soit (pire,
# risque de régression silencieuse) redémarré par systemd avec un ANCIEN
# valkey.conf resté en 0.0.0.0 par accident si l'écriture précédente avait
# aussi échoué à mi-chemin.
#
# Ordre imposé par CE script — refuse d'aller plus loin si l'IP demandée
# n'existe pas ENCORE sur wg0 :
#   1. `ip addr show wg0` — l'interface doit exister
#   2. l'IP demandée (défaut 10.200.0.2) doit apparaître dans sa liste
#      d'adresses — SINON : FATAL, aucune modification, aucun restart.
#   3. backup de valkey.conf
#   4. écriture de `bind <IP> 127.0.0.1`
#   5. détection du VRAI nom d'unit systemd (scripts/detect_valkey_systemd_unit.sh
#      — jamais `valkey` en dur, voir ce script pour le pourquoi)
#   6. restart de CETTE unit
#   7. PONG en local (127.0.0.1) — preuve que Valkey a bien redémarré
#   8. PONG via l'IP WireGuard elle-même (127.0.0.1 seul ne prouve pas le bind sur l'IP tunnel)
#
#   sudo bash scripts/configure_valkey_bind_wireguard.sh --wg-ip 10.200.0.2
#   sudo bash scripts/configure_valkey_bind_wireguard.sh --dry-run   # affiche sans modifier
#
# N'affiche JAMAIS le mot de passe Valkey (utilise REDISCLI_AUTH, jamais
# `-a` sur la ligne de commande — même discipline que
# scripts/check_valkey_network.sh).
# ═════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/detect_valkey_systemd_unit.sh
source "${HERE}/detect_valkey_systemd_unit.sh"

WG_IP="10.200.0.2"
WG_INTERFACE="wg0"
VALKEY_CONF="${VALKEY_CONF:-/etc/valkey/valkey.conf}"
VALKEY_PASSWORD="${VALKEY_PASSWORD:-}"
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --wg-ip) WG_IP="${2:?}"; shift 2 ;;
    --interface) WG_INTERFACE="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "Option inconnue : $1" >&2; exit 1 ;;
  esac
done

run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }

if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "Lancer en root (sudo)." >&2
  exit 1
fi

echo "═══ 1/8 : interface ${WG_INTERFACE} existe ═══"
if ! ip addr show "$WG_INTERFACE" >/dev/null 2>&1; then
  echo "FATAL: interface ${WG_INTERFACE} absente — WireGuard doit être UP avant de toucher à valkey.conf." >&2
  echo "       Voir infra/wireguard/README.md : installer + activer wg0 D'ABORD." >&2
  exit 1
fi
echo "  ✓ ${WG_INTERFACE} existe"
echo

echo "═══ 2/8 : IP ${WG_IP} présente sur ${WG_INTERFACE} ═══"
if ! ip addr show "$WG_INTERFACE" | grep -qE "inet ${WG_IP//./\\.}/"; then
  echo "FATAL: ${WG_IP} n'apparaît PAS sur ${WG_INTERFACE} — REFUS de modifier valkey.conf." >&2
  echo "       'ip addr show ${WG_INTERFACE}' :" >&2
  ip addr show "$WG_INTERFACE" 2>&1 | sed 's/^/       /' >&2
  echo "       Corriger la config wg0 (Address=) et relancer 'wg-quick up ${WG_INTERFACE}' d'abord." >&2
  exit 1
fi
echo "  ✓ ${WG_IP} présente sur ${WG_INTERFACE}"
echo

echo "═══ 3/8 : backup de ${VALKEY_CONF} ═══"
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : backup sauté)"
elif [ ! -f "$VALKEY_CONF" ]; then
  echo "FATAL: ${VALKEY_CONF} introuvable." >&2
  exit 1
else
  BACKUP="${VALKEY_CONF}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$VALKEY_CONF" "$BACKUP"
  echo "  ✓ Backup : ${BACKUP}"
fi
echo

echo "═══ 4/8 : écriture de 'bind ${WG_IP} 127.0.0.1' ═══"
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : écriture sautée — appliquerait : bind ${WG_IP} 127.0.0.1)"
else
  if grep -qE '^\s*bind\s+' "$VALKEY_CONF"; then
    sed -i "s/^\s*bind\s\+.*/bind ${WG_IP} 127.0.0.1/" "$VALKEY_CONF"
  else
    printf 'bind %s 127.0.0.1\n' "$WG_IP" >> "$VALKEY_CONF"
  fi
  if ! grep -qE '^\s*protected-mode\s+yes' "$VALKEY_CONF"; then
    if grep -qE '^\s*protected-mode\s+' "$VALKEY_CONF"; then
      sed -i 's/^\s*protected-mode\s\+.*/protected-mode yes/' "$VALKEY_CONF"
    else
      echo 'protected-mode yes' >> "$VALKEY_CONF"
    fi
  fi
  echo "  ✓ ${VALKEY_CONF} mis à jour : bind ${WG_IP} 127.0.0.1, protected-mode yes"
fi
echo

echo "═══ 5/8 : détection de l'unit systemd Valkey réelle ═══"
if [ "$DRY" -eq 1 ]; then
  VALKEY_UNIT="(dry-run, non détecté)"
  echo "  (dry-run : détection sautée)"
else
  if ! VALKEY_UNIT="$(detect_valkey_systemd_unit)"; then
    echo "FATAL: impossible de détecter l'unit systemd Valkey (voir ci-dessus) — valkey.conf modifié mais AUCUN restart effectué." >&2
    exit 1
  fi
  echo "  ✓ unit détectée : ${VALKEY_UNIT}"
fi
echo

echo "═══ 6/8 : restart de ${VALKEY_UNIT} ═══"
run systemctl restart "$VALKEY_UNIT"
echo

echo "═══ 7/8 : PONG en local (127.0.0.1) ═══"
CLI_BIN=""
for candidate in valkey-cli redis-cli; do
  command -v "$candidate" >/dev/null 2>&1 && { CLI_BIN="$candidate"; break; }
done
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : PING sauté)"
elif [ -z "$CLI_BIN" ]; then
  echo "  ? ni valkey-cli ni redis-cli disponible — impossible de valider PING localement" >&2
elif [ -z "$VALKEY_PASSWORD" ]; then
  echo "  ? VALKEY_PASSWORD non fourni — impossible de valider PING authentifié (fournir via l'env var, jamais en argument)" >&2
else
  LOCAL_PING="$(REDISCLI_AUTH="$VALKEY_PASSWORD" "$CLI_BIN" -h 127.0.0.1 -p 6379 --no-auth-warning ping 2>&1)"
  [ "$LOCAL_PING" = "PONG" ] && echo "  ✓ PONG (127.0.0.1)" || { echo "  ✗ échec PING local : ${LOCAL_PING}" >&2; exit 1; }
fi
echo

echo "═══ 8/8 : PONG via l'IP WireGuard (${WG_IP}) ═══"
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : PING sauté)"
elif [ -z "$CLI_BIN" ] || [ -z "$VALKEY_PASSWORD" ]; then
  echo "  ? sauté (voir avertissement ci-dessus)"
else
  WG_PING="$(REDISCLI_AUTH="$VALKEY_PASSWORD" "$CLI_BIN" -h "$WG_IP" -p 6379 --no-auth-warning ping 2>&1)"
  [ "$WG_PING" = "PONG" ] && echo "  ✓ PONG (${WG_IP})" || { echo "  ✗ échec PING via ${WG_IP} : ${WG_PING}" >&2; exit 1; }
fi
unset VALKEY_PASSWORD LOCAL_PING WG_PING

echo
echo "✓ Terminé. Étape suivante : PONG depuis Hetzner (scripts/check_valkey_network.sh),"
echo "  PUIS SEULEMENT ENSUITE retirer l'exposition publique 6379 (Security Group AWS)."
