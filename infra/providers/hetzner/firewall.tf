# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/firewall.tf — pare-feu Hetzner Cloud (couche 2,
# EN PLUS de infra/firewall/ufw.sh sur chaque hôte — défense en profondeur,
# voir infra/firewall/ufw.sh en-tête et infra/providers/README.md).
#
# Default-deny : hcloud_firewall n'autorise QUE les règles `rule` déclarées
# ci-dessous ; tout le reste en entrée est refusé implicitement. Aucun
# service interne (Redis 6379, PgBouncer 6432, MCP 8003, Flower 5555,
# Alloy) n'est jamais ouvert publiquement ici — ils restent joignables
# uniquement via le réseau privé (network.tf) ou un tunnel SSH (voir
# commentaire du service flower dans docker-compose.prod.yml).
# ═════════════════════════════════════════════════════════════════════

resource "hcloud_firewall" "app" {
  name   = "ladini-app-firewall"
  labels = var.labels

  # ═══════════════════════════════════════════════════════════════════
  # (2026-09-17, audit Cloudflare/LB) — 80/443/443-udp RETIRÉS du monde
  # public. Fait délibéré, pas un oubli : voir le raisonnement complet.
  #
  # FAIT HETZNER (documenté, pas une supposition) : un Cloud Firewall ne
  # filtre QUE l'interface PUBLIQUE d'un server — le trafic sur le réseau
  # privé (network.tf, 10.20.0.0/16) n'est JAMAIS filtré par ce firewall,
  # quelles que soient les règles ci-dessous. Le Load Balancer route vers
  # ce node via SA SEULE IP PRIVÉE (`use_private_ip = true`, voir
  # load_balancer.tf) — donc retirer 80/443/443-udp d'ICI n'affecte EN
  # RIEN le chemin LB → node, qui ne passe jamais par ce firewall.
  #
  # Ce que ça ferme réellement : le node a AUSSI une IP PUBLIQUE
  # (nécessaire pour SSH admin + egress apt/docker/GHCR/APIs LLM — voir
  # servers.tf/README §"IP publique"). Avant ce correctif, n'importe qui
  # pouvait contourner Cloudflare + le LB en appelant directement
  # `IP_PUBLIQUE_DU_NODE:80` / `:443` — le firewall les acceptait
  # (source_ips = 0.0.0.0/0) tout autant que le trafic légitime. Plus
  # aucune règle publique 80/443/443-udp ⇒ ce bypass est structurellement
  # impossible (bloqué au niveau hyperviseur Hetzner, AVANT même d'arriver
  # à la VM — voir aussi infra/firewall/ufw.sh, désormais aligné en
  # défense en profondeur).
  #
  # ACME (Let's Encrypt, challenge HTTP-01 sur Caddy) : AUCUN impact.
  # Let's Encrypt valide en résolvant le VRAI enregistrement DNS public
  # (api.ladini.tech → Cloudflare proxied → IP du LB), jamais en visant
  # directement l'IP du node — le challenge emprunte donc EXACTEMENT le
  # même chemin que le trafic normal (Cloudflare → LB → node en privé),
  # jamais l'IP publique du node.
  # ═══════════════════════════════════════════════════════════════════

  # SSH — restreint à admin_cidrs (jamais 0.0.0.0/0, voir variables.tf).
  # Doit rester public : l'admin n'est PAS sur le réseau privé Hetzner.
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_cidrs
    description = "SSH admin/déploiement — CIDR restreints (var.admin_cidrs)"
  }

  # ICMP — ping/diagnostic depuis n'importe où ; pas de surface d'attaque
  # significative et utile pour le monitoring externe / debug réseau.
  rule {
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "ICMP (ping, diagnostic)"
  }

  # Aucune règle pour 80/443/443-udp (voir le bloc de commentaire ci-dessus),
  # ni pour 6379 (Redis), 6432 (PgBouncer), 8003 (MCP), 5555 (Flower), ni les
  # ports Alloy : tous refusés par défaut par hcloud_firewall (default-deny)
  # sur l'interface PUBLIQUE. Le trafic HTTP/HTTPS légitime (Cloudflare → LB
  # → node) passe par le réseau privé, jamais filtré ici par construction —
  # voir network.tf. Les services internes restent accessibles uniquement
  # via ce réseau privé entre nodes, ou en loopback local (voir
  # docker-compose.prod.yml : `127.0.0.1:5555:5555` pour Flower, tunnel SSH
  # requis — voir le commentaire du service `flower`).
}

# Appliqué à TOUS les nodes (app + scheduler dédié éventuel) — un seul
# firewall partagé, pas de différenciation par rôle : le trafic entrant
# public autorisé (22/80/443/443udp/icmp) est identique quel que soit le
# rôle Compose du node.
resource "hcloud_firewall_attachment" "app" {
  firewall_id = hcloud_firewall.app.id
  server_ids = concat(
    [for s in hcloud_server.app : s.id],
    [for s in hcloud_server.scheduler : s.id],
  )
}
