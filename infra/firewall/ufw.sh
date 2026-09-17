#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════
# infra/firewall/ufw.sh — pare-feu hôte PROVIDER-NEUTRAL (§19).
#
# Fonctionne sur n'importe quelle VM Linux avec ufw, indépendamment du
# firewall cloud du fournisseur (DO/AWS SG/Hetzner…). Le firewall cloud
# peut AJOUTER une couche, mais ne doit pas être la seule ligne de défense.
#
# Ouvre PUBLIQUEMENT : 22 (SSH, restreint par IP source côté sshd/ufw n'est
# PAS fait ici — c'est le firewall cloud, ex. infra/providers/hetzner/firewall.tf,
# qui restreint la SOURCE), et 80/443 (HTTP/HTTPS) SEULEMENT si
# PRIVATE_NET_CIDR n'est pas défini (voir bloc dédié plus bas). Rien d'autre.
# Les services internes (8000 api, 6379 redis, 6432 pgbouncer, 8003 mcp,
# 5555 flower) restent sur le réseau Docker privé + loopback.
#
#   sudo bash infra/firewall/ufw.sh              # applique
#   sudo bash infra/firewall/ufw.sh --dry-run    # affiche seulement
#
# (2026-09-17, audit Cloudflare/LB Hetzner) — `PRIVATE_NET_CIDR` (optionnel,
# env var) : si défini, 80/443/443-udp sont restreints à CE CIDR au lieu du
# monde entier — défense en profondeur cohérente avec une topologie
# Cloudflare → Load Balancer cloud → node (le LB parle au node via son
# réseau privé, jamais besoin d'exposer 80/443 publiquement AU NIVEAU HÔTE
# non plus, voir infra/providers/hetzner/firewall.tf pour le même
# raisonnement côté firewall cloud). Vide par défaut = comportement
# HISTORIQUE inchangé (80/443 ouverts à 0.0.0.0/0), pour ne rien casser sur
# un déploiement SANS Load Balancer devant (single-node exposé
# directement — toujours un topology valide de ce repo). Exemple Hetzner :
#   PRIVATE_NET_CIDR=10.20.0.0/16 bash infra/firewall/ufw.sh
#
# ⚠️ NE COUPE PAS votre session SSH : le port SSH courant est détecté et
#    autorisé AVANT tout `ufw enable`. Vérifiez la détection dans la sortie.
#
# (2026-09-17, incident réel post-`terraform apply` : `ufw status` restait
# `inactive` bien que cloud-init affiche `done`) — BUG CORRIGÉ : sous
# `set -euo pipefail`, l'ancienne ligne `SSH_PORT="$(grep ... | awk ... |
# head -n1)"` mourait SILENCIEUSEMENT dès que `sshd_config` n'a AUCUNE
# directive "Port" active — le cas NORMAL sur Ubuntu (rien de commenté ni
# présent par défaut, le port 22 vient du binaire sshd, pas du fichier).
# `pipefail` propage l'échec de `grep` (aucune ligne trouvée = exit 1) à
# TOUTE la pipeline, et cette affectation nue hérite de ce statut → le
# script entier s'arrête ici, avant le premier `ufw reset`. Le firewall
# hôte n'était donc JAMAIS appliqué — mais cloud-init `runcmd` concatène
# toutes ses commandes en un seul script SANS `set -e`, et sa dernière
# commande (`systemctl restart ssh || true`) réussit toujours : cloud-init
# rapportait donc `done` malgré cet échec totalement invisible. Reproduit et
# confirmé (pas une supposition) : `grep ... /dev/null | awk ... | head -n1`
# sous `set -euo pipefail`, sans garde, sort en erreur avant tout affichage.
# Fix : `|| true` sur la pipeline de détection — "aucune ligne trouvée" est
# un résultat NORMAL (→ fallback 22), pas une erreur.
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }

PRIVATE_NET_CIDR="${PRIVATE_NET_CIDR:-}"
# Overridable uniquement pour les tests de régression (scripts/test/test-ufw-firewall.sh) :
# pointe la détection du port SSH sur un fixture au lieu du vrai sshd_config.
SSHD_CONFIG_PATH="${SSHD_CONFIG_PATH:-/etc/ssh/sshd_config}"

# root + `ufw` installé : requis uniquement en mode réel. `--dry-run` ne
# modifie rien et doit rester exécutable sans privilèges (CI, tests, poste
# de dev) — c'est ce qui permet de le couvrir par des tests automatisés.
if [ "$DRY" -eq 0 ]; then
  [ "$(id -u)" -eq 0 ] || { echo "Lancer en root (sudo)." >&2; exit 1; }
  command -v ufw >/dev/null || { echo "Installer ufw : apt-get install -y ufw" >&2; exit 1; }
fi

# Port SSH réel (sshd_config ; défaut 22 si aucune directive "Port" active,
# le cas courant). On l'autorise AVANT d'activer. Le `|| true` final est le
# correctif du bug décrit dans l'en-tête ci-dessus — ne pas le retirer.
SSH_PORT="$(grep -Ei '^\s*Port\s+[0-9]+' "$SSHD_CONFIG_PATH" 2>/dev/null | awk '{print $2}' | head -n1 || true)"
SSH_PORT="${SSH_PORT:-22}"
echo "→ Port SSH détecté : ${SSH_PORT}"
echo "→ Connexion SSH courante :"
ss -tnp 2>/dev/null | grep -i ssh || true
echo

if [ -n "$PRIVATE_NET_CIDR" ]; then
  echo "→ PRIVATE_NET_CIDR défini (${PRIVATE_NET_CIDR}) : 80/443 seront restreints à ce CIDR (LB cloud devant, pas d'exposition publique directe)."
else
  echo "→ PRIVATE_NET_CIDR non défini : 80/443/443-udp seront ouverts publiquement (exposition directe, comportement historique provider-neutral)."
fi
echo

run ufw --force reset
run ufw default deny incoming
run ufw default allow outgoing

run ufw limit "${SSH_PORT}/tcp"       # limit = anti-bruteforce SSH
if [ -n "$PRIVATE_NET_CIDR" ]; then
  run ufw allow from "$PRIVATE_NET_CIDR" to any port 80 proto tcp
  run ufw allow from "$PRIVATE_NET_CIDR" to any port 443 proto tcp
  # PAS de règle 443/udp ici, délibérément : un Load Balancer devant ce
  # node (le cas d'usage de PRIVATE_NET_CIDR) ne forwarde QUE du TCP (voir
  # infra/providers/hetzner/load_balancer.tf — ses 2 services sont
  # `protocol = "tcp"`), et Cloudflare négocie HTTP/3 avec le CLIENT
  # seulement — sa propre connexion vers l'origine (le LB) reste HTTP(S)
  # standard. Aucun trafic UDP n'atteindra donc jamais ce node dans cette
  # topologie ; ouvrir 443/udp, même restreint au CIDR privé, n'aurait
  # aucun effet utile.
else
  run ufw allow 80/tcp
  run ufw allow 443/tcp
  run ufw allow 443/udp               # HTTP/3 (Caddy) — utile en exposition directe (pas de LB devant)
fi

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
if [ -n "$PRIVATE_NET_CIDR" ]; then
  echo "✓ Pare-feu appliqué. Vérifiez que 'ufw status verbose' liste bien : ${SSH_PORT}/tcp (LIMIT, public), 80/tcp et 443/tcp (ALLOW depuis ${PRIVATE_NET_CIDR} uniquement) — et RIEN d'autre en ALLOW public."
else
  echo "✓ Pare-feu appliqué. Vérifiez que 'ufw status verbose' liste bien : ${SSH_PORT}/tcp, 80/tcp, 443/tcp, 443/udp (tous publics) — et RIEN d'autre en ALLOW."
fi
echo "  Testez une NOUVELLE session SSH avant de fermer celle-ci."
