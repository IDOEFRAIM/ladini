# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/load_balancer.tf — Load Balancer Hetzner Cloud.
#
# TCP passthrough sur 80/443 (PAS de terminaison TLS côté LB) : chaque node
# app fait tourner son propre Caddy (infra/reverse-proxy/) qui gère le
# certificat Let's Encrypt (challenge HTTP-01 sur 80) et termine le TLS —
# voir infra/reverse-proxy/Caddyfile. Terminer le TLS au niveau du LB
# casserait le renouvellement ACME (qui a besoin du vrai trafic HTTP par
# node) et forcerait à dupliquer/partager un certificat entre nodes, ce que
# ce projet ne fait pas. Le health check HTTPS ci-dessous parle donc
# directement à Caddy sur chaque node, en ignorant la validité du cert
# (`tls = true` sans exiger un CA connu du LB — Hetzner ne valide pas la
# chaîne, seule la connexion TLS + réponse HTTP comptent).
#
# Cible UNIQUEMENT les nodes app (jamais un scheduler/admin dédié : ce
# rôle ne sert pas de trafic HTTP public, voir servers.tf).
# ═════════════════════════════════════════════════════════════════════

resource "hcloud_load_balancer" "app" {
  name               = "ladini-lb"
  load_balancer_type = var.load_balancer_type
  location           = var.location
  labels             = var.labels

  algorithm {
    # least_connections : plus adapté que round_robin pour des requêtes de
    # durée variable (l'agent LLM peut prendre plusieurs secondes — voir
    # LLM_FAST_BUDGET_SECONDS/LLM_REASONING_BUDGET_SECONDS dans
    # docker-compose.prod.yml) — évite qu'un node lent accumule une file
    # pendant qu'un round_robin continue de lui envoyer sa part égale.
    type = "least_connections"
  }
}

# Attache le LB au réseau privé pour pouvoir router vers les IP privées des
# nodes (use_private_ip = true sur les targets ci-dessous) plutôt que de
# repasser par leurs IP publiques.
resource "hcloud_load_balancer_network" "app" {
  load_balancer_id = hcloud_load_balancer.app.id
  network_id       = hcloud_network.main.id

  depends_on = [hcloud_network_subnet.app]
}

# for_each sur les mêmes clés stables que hcloud_server.app (servers.tf) —
# migration miroir, voir moved.tf. L'identité d'un target ne dépend QUE de
# la clé du node qu'il cible, jamais de la taille courante du cluster :
# retirer un node du MILIEU (scale-in ciblé, futur) ne recrée jamais les
# targets des nodes qui restent.
resource "hcloud_load_balancer_target" "app" {
  for_each         = local.app_node_keys
  type             = "server"
  load_balancer_id = hcloud_load_balancer.app.id
  server_id        = hcloud_server.app[each.key].id
  use_private_ip   = true

  depends_on = [hcloud_load_balancer_network.app]
}

# ── HTTPS (443) — trafic applicatif réel, TCP passthrough ───────────
resource "hcloud_load_balancer_service" "https" {
  load_balancer_id = hcloud_load_balancer.app.id
  protocol         = "tcp"
  listen_port      = 443
  destination_port = 443

  health_check {
    protocol = "http"
    port     = 443
    interval = 10
    timeout  = 5
    retries  = 3

    http {
      domain       = var.public_domain
      path         = "/health/ready"
      tls          = true
      status_codes = ["200"]
    }
  }
}

# ── HTTP (80) — challenge ACME + redirection vers HTTPS par Caddy ───
# Health check en TCP simple (pas HTTP) : Caddy fait de l'HTTPS automatique
# dès qu'un nom de domaine est déclaré dans le Caddyfile, donc TOUTE requête
# HTTP en clair sur 80 reçoit une redirection 3xx vers HTTPS — un health
# check HTTP attendant un 200 échouerait à tort en permanence. Un simple
# TCP connect suffit : la vraie preuve de santé applicative (DB+Redis, voir
# backend/src/ladini/api/main.py) est portée par le service 443 ci-dessus.
resource "hcloud_load_balancer_service" "http" {
  load_balancer_id = hcloud_load_balancer.app.id
  protocol         = "tcp"
  listen_port      = 80
  destination_port = 80

  health_check {
    protocol = "tcp"
    port     = 80
    interval = 10
    timeout  = 5
    retries  = 3
  }
}
