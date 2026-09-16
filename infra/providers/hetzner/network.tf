# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/network.tf — réseau privé Hetzner (§ IaC Hetzner).
#
# Tout le trafic inter-node (Redis externe, DB via pgbouncer, MCP daemon,
# Celery broker) DOIT transiter par ce réseau privé, jamais par les IP
# publiques — voir infra/inventory.example.yml (commentaire sur node.host).
# ═════════════════════════════════════════════════════════════════════

resource "hcloud_network" "main" {
  name     = "ladini-network"
  ip_range = var.network_ip_range
  labels   = var.labels
}

# Subnet unique pour l'instant (nodes app + scheduler + admin). Rien
# n'empêche d'ajouter un 2e subnet (ex: DB managée si Hetzner en propose une
# un jour) sans toucher à celui-ci — voir infra/providers/README.md, la DB
# managée Hetzner-native n'existe pas encore, on pointe vers une DB externe.
resource "hcloud_network_subnet" "app" {
  network_id   = hcloud_network.main.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = var.app_subnet_ip_range
}
