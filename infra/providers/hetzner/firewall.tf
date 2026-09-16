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

  # SSH — restreint à admin_cidrs (jamais 0.0.0.0/0, voir variables.tf).
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_cidrs
    description = "SSH admin/déploiement — CIDR restreints (var.admin_cidrs)"
  }

  # HTTP — uniquement pour la redirection Caddy vers HTTPS (voir Caddyfile).
  # Ouvert publiquement car le Load Balancer Hetzner fait un TCP passthrough
  # (pas de terminaison TLS côté LB, voir load_balancer.tf) : Caddy sur
  # chaque node gère lui-même le certificat Let's Encrypt (challenge HTTP-01
  # + HTTPS). Le LB relaie donc le trafic public tel quel jusqu'au node.
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "HTTP public (redirect → HTTPS + challenge ACME) via le Load Balancer"
  }

  # HTTPS — trafic applicatif réel, terminé par Caddy sur le node.
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "HTTPS public via le Load Balancer"
  }

  # HTTP/3 (QUIC) — Caddy l'annonce (voir Caddyfile, `encode`/alt-svc par
  # défaut de Caddy 2.8) ; ouvrir l'UDP correspondant évite un fallback
  # silencieux en TCP-only pour les clients qui tentent QUIC en premier.
  rule {
    direction   = "in"
    protocol    = "udp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "HTTP/3 (QUIC) public via le Load Balancer"
  }

  # ICMP — ping/diagnostic depuis n'importe où ; pas de surface d'attaque
  # significative et utile pour le monitoring externe / debug réseau.
  rule {
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "ICMP (ping, diagnostic)"
  }

  # Aucune règle pour 6379 (Redis), 6432 (PgBouncer), 8003 (MCP), 5555
  # (Flower), ni les ports Alloy : ils ne sont PAS déclarés → refusés par
  # défaut par hcloud_firewall (default-deny). Ils restent accessibles
  # uniquement via le réseau privé (network.tf) entre nodes, ou en loopback
  # local (voir docker-compose.prod.yml : `127.0.0.1:5555:5555` pour Flower,
  # tunnel SSH requis — voir le commentaire du service `flower`).
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
