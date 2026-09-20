#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/wireguard_setup_aws.sh — installe WireGuard sur l'EC2 AWS
# (Valkey) et prépare wg0 pour le tunnel Hetzner ↔ AWS (migration
# Upstash → Valkey, 2026-09-20). Voir infra/wireguard/README.md pour la
# procédure complète et le plan d'adressage (10.200.0.0/24).
#
# Usage :
#   sudo bash scripts/wireguard_setup_aws.sh                # génère les clés, affiche la clé publique, s'arrête
#   sudo bash scripts/wireguard_setup_aws.sh --activate \
#        --hetzner-pubkey '<clé publique Hetzner>' [--hetzner-endpoint '<IP:port>']
#                                                            # complète wg0.conf et active le tunnel
#   sudo bash scripts/wireguard_setup_aws.sh --dry-run       # affiche les commandes sans les exécuter
#
# Idempotent : si une paire de clés existe déjà dans /etc/wireguard/, elle
# n'est JAMAIS régénérée (régénérer casserait tout tunnel déjà négocié avec
# les peers existants) — le script réutilise la clé publique déjà présente.
#
# N'affiche JAMAIS la clé PRIVÉE. La clé publique n'est pas un secret (peut
# être copiée/collée sans risque) — voir infra/wireguard/README.md.
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

DRY=0
ACTIVATE=0
HETZNER_PUBKEY=""
HETZNER_ENDPOINT=""
HETZNER_TUNNEL_IP="10.200.0.1"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --activate) ACTIVATE=1; shift ;;
    --hetzner-pubkey) HETZNER_PUBKEY="${2:-}"; shift 2 ;;
    --hetzner-endpoint) HETZNER_ENDPOINT="${2:-}"; shift 2 ;;
    --hetzner-tunnel-ip) HETZNER_TUNNEL_IP="${2:-}"; shift 2 ;;
    *) echo "Option inconnue : $1" >&2; exit 1 ;;
  esac
done

run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }

if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "Lancer en root (sudo)." >&2
  exit 1
fi

WG_DIR="/etc/wireguard"
PRIVKEY_PATH="${WG_DIR}/privatekey"
PUBKEY_PATH="${WG_DIR}/publickey"
CONF_PATH="${WG_DIR}/wg0.conf"
AWS_TUNNEL_IP="10.200.0.2"

echo "═══ 1/4 : installation wireguard-tools ═══"
if command -v wg >/dev/null 2>&1; then
  echo "  ✓ wg déjà installé ($(command -v wg))"
else
  run apt-get update -y
  run apt-get install -y wireguard wireguard-tools
fi
echo

echo "═══ 2/4 : génération de la paire de clés (si absente) ═══"
run mkdir -p "$WG_DIR"
run chmod 700 "$WG_DIR"
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : génération de clé sautée)"
elif [ -f "$PRIVKEY_PATH" ] && [ -f "$PUBKEY_PATH" ]; then
  echo "  ✓ paire de clés déjà présente — RÉUTILISÉE (jamais régénérée automatiquement, casserait le tunnel avec les peers existants)"
else
  umask 077
  wg genkey | tee "$PRIVKEY_PATH" | wg pubkey > "$PUBKEY_PATH"
  chmod 600 "$PRIVKEY_PATH" "$PUBKEY_PATH"
  echo "  ✓ nouvelle paire de clés générée dans ${WG_DIR}"
fi
echo

echo "═══ 3/4 : clé PUBLIQUE AWS (à copier côté Hetzner) ═══"
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : lecture de clé sautée)"
else
  echo "  $(cat "$PUBKEY_PATH")"
fi
echo "  Cette clé N'EST PAS un secret — copiez-la dans"
echo "  infra/wireguard/hetzner-wg0.conf.template (section [Peer], PublicKey =)"
echo "  sur le node Hetzner. La clé PRIVÉE (${PRIVKEY_PATH}) ne quitte JAMAIS cette machine."
echo

echo "═══ 4/4 : configuration wg0 ═══"
if [ "$ACTIVATE" -eq 0 ]; then
  echo "  --activate non fourni — wg0 N'A PAS été configuré/démarré."
  echo "  Une fois la clé publique Hetzner obtenue, relancez :"
  echo "    sudo bash scripts/wireguard_setup_aws.sh --activate --hetzner-pubkey '<clé publique Hetzner>'"
  exit 0
fi

if [ -z "$HETZNER_PUBKEY" ]; then
  echo "FATAL: --activate requiert --hetzner-pubkey '<clé publique Hetzner>'" >&2
  exit 1
fi

if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : écriture de ${CONF_PATH} sautée)"
else
  PRIVKEY_VALUE="$(cat "$PRIVKEY_PATH")"
  {
    echo "[Interface]"
    echo "PrivateKey = ${PRIVKEY_VALUE}"
    echo "Address = ${AWS_TUNNEL_IP}/24"
    echo "ListenPort = 51820"
    echo
    echo "[Peer]"
    echo "PublicKey = ${HETZNER_PUBKEY}"
    echo "AllowedIPs = ${HETZNER_TUNNEL_IP}/32"
    if [ -n "$HETZNER_ENDPOINT" ]; then
      echo "Endpoint = ${HETZNER_ENDPOINT}"
    fi
  } > "$CONF_PATH"
  chmod 600 "$CONF_PATH"
  echo "  ✓ ${CONF_PATH} écrit (clé privée jamais affichée ci-dessus)"
fi

run systemctl enable wg-quick@wg0
run systemctl restart wg-quick@wg0

echo
echo "═══ Vérification ═══"
run wg show wg0
echo
echo "✓ wg0 configuré et démarré côté AWS."
echo "  Étape suivante : côté Hetzner, scripts/wireguard_setup_hetzner.sh --activate"
echo "  avec la clé publique AWS ci-dessus, PUIS scripts/check_valkey_network.sh"
echo "  pour valider le tunnel de bout en bout AVANT de toucher au Security Group"
echo "  (voir infra/wireguard/README.md §Security Group AWS)."
