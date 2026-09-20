#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# scripts/wireguard_setup_hetzner.sh — installe WireGuard sur un node
# Hetzner et prépare wg0 pour le tunnel Hetzner ↔ AWS Valkey (migration
# Upstash → Valkey, 2026-09-20). Miroir de wireguard_setup_aws.sh, voir
# infra/wireguard/README.md pour la procédure complète.
#
# Usage :
#   sudo bash scripts/wireguard_setup_hetzner.sh                # génère les clés, affiche la clé publique, s'arrête
#   sudo bash scripts/wireguard_setup_hetzner.sh --activate \
#        --aws-pubkey '<clé publique AWS>' --aws-endpoint '<IP_PUBLIQUE_AWS>:51820'
#                                                                # complète wg0.conf et active le tunnel
#   sudo bash scripts/wireguard_setup_hetzner.sh --dry-run       # affiche les commandes sans les exécuter
#   sudo bash scripts/wireguard_setup_hetzner.sh --tunnel-ip 10.200.0.3 ...
#                                                                # pour un 2e node Hetzner (scale-out) — voir README.md
#
# Idempotent : une paire de clés déjà présente n'est JAMAIS régénérée.
# N'affiche JAMAIS la clé PRIVÉE.
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

DRY=0
ACTIVATE=0
AWS_PUBKEY=""
AWS_ENDPOINT=""
TUNNEL_IP="10.200.0.1"   # 1er node Hetzner ; .3/.4/... pour les suivants (voir README.md)

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --activate) ACTIVATE=1; shift ;;
    --aws-pubkey) AWS_PUBKEY="${2:-}"; shift 2 ;;
    --aws-endpoint) AWS_ENDPOINT="${2:-}"; shift 2 ;;
    --tunnel-ip) TUNNEL_IP="${2:-}"; shift 2 ;;
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

echo "═══ 3/4 : clé PUBLIQUE Hetzner (à copier côté AWS) ═══"
if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : lecture de clé sautée)"
else
  echo "  $(cat "$PUBKEY_PATH")"
fi
echo "  Cette clé N'EST PAS un secret — copiez-la dans"
echo "  infra/wireguard/aws-wg0.conf.template (section [Peer], PublicKey =)"
echo "  sur l'EC2 AWS. La clé PRIVÉE (${PRIVKEY_PATH}) ne quitte JAMAIS cette machine."
echo

echo "═══ 4/4 : configuration wg0 ═══"
if [ "$ACTIVATE" -eq 0 ]; then
  echo "  --activate non fourni — wg0 N'A PAS été configuré/démarré."
  echo "  Une fois la clé publique AWS obtenue, relancez :"
  echo "    sudo bash scripts/wireguard_setup_hetzner.sh --activate --aws-pubkey '<clé publique AWS>' --aws-endpoint '<IP_PUBLIQUE_AWS>:51820'"
  exit 0
fi

if [ -z "$AWS_PUBKEY" ] || [ -z "$AWS_ENDPOINT" ]; then
  echo "FATAL: --activate requiert --aws-pubkey '<clé publique AWS>' ET --aws-endpoint '<IP_PUBLIQUE_AWS>:51820'" >&2
  exit 1
fi

if [ "$DRY" -eq 1 ]; then
  echo "  (dry-run : écriture de ${CONF_PATH} sautée)"
else
  PRIVKEY_VALUE="$(cat "$PRIVKEY_PATH")"
  {
    echo "[Interface]"
    echo "PrivateKey = ${PRIVKEY_VALUE}"
    echo "Address = ${TUNNEL_IP}/24"
    echo
    echo "[Peer]"
    echo "PublicKey = ${AWS_PUBKEY}"
    echo "Endpoint = ${AWS_ENDPOINT}"
    echo "AllowedIPs = ${AWS_TUNNEL_IP}/32"
    echo "PersistentKeepalive = 25"
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
echo "✓ wg0 configuré et démarré côté Hetzner (IP tunnel : ${TUNNEL_IP})."
echo "  Étape suivante : sudo bash scripts/check_valkey_network.sh"
echo "  AVANT de toucher au Security Group AWS (voir infra/wireguard/README.md)."
