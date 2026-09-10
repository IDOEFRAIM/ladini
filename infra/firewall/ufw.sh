#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# infra/firewall/ufw.sh — pare-feu hôte PROVIDER-NEUTRAL (§19).
#
# Fonctionne sur n'importe quelle VM Linux avec ufw, indépendamment du
# firewall cloud du fournisseur (DO/AWS SG/Hetzner…). Le firewall cloud
# peut AJOUTER une couche, mais ne doit pas être la seule ligne de défense.
#
# Ouvre PUBLIQUEMENT : 22 (SSH), 80 (HTTP→redirect), 443 (HTTPS). Rien d'autre.
# Les services internes (8000 api, 6379 redis, 6432 pgbouncer, 8003 mcp,
# 5555 flower) restent sur le réseau Docker privé + loopback.
#
#   sudo bash infra/firewall/ufw.sh              # applique
#   sudo bash infra/firewall/ufw.sh --dry-run    # affiche seulement
#
# ⚠️ NE COUPE PAS votre session SSH : le port SSH courant est détecté et
#    autorisé AVANT tout `ufw enable`. Vérifiez la détection dans la sortie.
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }

[ "$(id -u)" -eq 0 ] || { echo "Lancer en root (sudo)." >&2; exit 1; }
command -v ufw >/dev/null || { echo "Installer ufw : apt-get install -y ufw" >&2; exit 1; }

# Port SSH réel (sshd_config ; défaut 22). On l'autorise AVANT d'activer.
SSH_PORT="$(grep -Ei '^\s*Port\s+[0-9]+' /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' | head -n1)"
SSH_PORT="${SSH_PORT:-22}"
echo "→ Port SSH détecté : ${SSH_PORT}"
echo "→ Connexion SSH courante :"
ss -tnp 2>/dev/null | grep -i ssh || true
echo

run ufw --force reset
run ufw default deny incoming
run ufw default allow outgoing

run ufw limit "${SSH_PORT}/tcp"       # limit = anti-bruteforce SSH
run ufw allow 80/tcp
run ufw allow 443/tcp
run ufw allow 443/udp                 # HTTP/3 (Caddy)

# Docker publie ses ports en contournant ufw via iptables (chaîne DOCKER).
# Dans ce projet, api/flower sont publiés sur 127.0.0.1 UNIQUEMENT (voir
# docker-compose.prod.yml), donc non joignables de l'extérieur même sans
# règle ufw. Le reverse proxy (80/443) est le seul port Docker public et il
# est couvert ci-dessus. Rien de spécial à faire côté DOCKER-USER ici, mais
# si un jour un service est publié sur 0.0.0.0, ajouter une règle explicite
# dans /etc/ufw/after.rules (chaîne ufw-user-forward) ou repasser le port en
# 127.0.0.1.

run ufw --force enable
run ufw status verbose

echo
echo "✓ Pare-feu appliqué. Vérifiez que 'ufw status' liste bien ${SSH_PORT}/tcp, 80, 443 — et RIEN d'autre en ALLOW public."
echo "  Testez une NOUVELLE session SSH avant de fermer celle-ci."
