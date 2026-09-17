# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/servers.tf — nodes Hetzner Cloud.
#
# Rôles Compose (docker-compose.prod.yml `profiles:`) : app / scheduler /
# admin — voir infra/inventory.example.yml pour la contrainte (EXACTEMENT
# UN node `scheduler` dans tout le cluster, jamais 0, jamais 2+).
#
# Par défaut (var.scheduler_on_dedicated_node = false) : le node app
# d'index 0 cumule app+scheduler+admin — comme documenté en Phase 1 dans
# infra/inventory.example.yml. C'est le choix par défaut le plus
# économique (pas de server supplémentaire) et cohérent avec le MVP
# actuel. Passez scheduler_on_dedicated_node à true pour isoler Beat/Flower
# sur un server séparé (utile si la charge scheduler/admin perturbe le
# node app, ou pour réduire le blast radius d'un redémarrage app).
#
# app_node_count scale de 1 à N sans changement de structure : `count`
# porte toute la logique, aucune ressource nommée en dur par index.
# ═════════════════════════════════════════════════════════════════════

resource "hcloud_ssh_key" "deploy" {
  name       = "ladini-deploy-key"
  public_key = var.ssh_public_key
  labels     = var.labels
}

locals {
  # Rôles par node app (index 0 = app+scheduler+admin si le scheduler n'est
  # PAS dédié ; sinon tous les nodes app ne portent que 'app').
  app_node_roles = [
    for i in range(var.app_node_count) :
    (i == 0 && !var.scheduler_on_dedicated_node) ? ["app", "scheduler", "admin"] : ["app"]
  ]

  placement_group_id = length(hcloud_placement_group.app) > 0 ? hcloud_placement_group.app[0].id : null
}

resource "hcloud_server" "app" {
  count        = var.app_node_count
  name         = "ladini-app-${count.index + 1}"
  server_type  = var.app_server_type
  image        = var.app_image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.deploy.id]
  firewall_ids = [hcloud_firewall.app.id]

  # Placement group non appliqué en dessous de 2 nodes (placement.tf).
  placement_group_id = local.placement_group_id

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  network {
    network_id = hcloud_network.main.id
    # Pas d'`ip` figé : laisse hcloud allouer dans app_subnet_ip_range.
    # Les IP privées réelles vont dans infra/inventory.yml (gitignored,
    # rempli après `terraform apply` à partir de l'output `app_nodes`).
  }

  # (2026-09-17) — les labels Hetzner Cloud suivent la syntaxe des labels
  # Kubernetes : valeur alphanumérique + `-_.` uniquement, JAMAIS de virgule
  # (confirmé par un vrai `terraform plan` : "label value 'app,scheduler,
  # admin' (key: role) is not correctly formatted"). `node_roles` plus bas
  # (passé au cloud-init, pas un label Hetzner) garde la virgule — seul CE
  # label change de séparateur.
  labels = merge(var.labels, {
    role = join("-", local.app_node_roles[count.index])
  })

  user_data = templatefile("${path.module}/cloud-init/app-node.yaml.tpl", {
    deploy_user      = var.deploy_user
    ssh_public_key   = var.ssh_public_key
    git_repo_url     = var.git_repo_url
    git_ref          = var.git_ref
    public_domain    = var.public_domain
    acme_email       = var.acme_email
    node_name        = "ladini-app-${count.index + 1}"
    node_roles       = join(",", local.app_node_roles[count.index])
    private_net_cidr = var.network_ip_range
  })

  # Le subnet doit exister avant qu'un server puisse s'y attacher.
  depends_on = [hcloud_network_subnet.app]
}

# ── Node scheduler DÉDIÉ (optionnel — var.scheduler_on_dedicated_node) ──
# count = 0 par défaut (Beat/Flower vivent sur ladini-app-1, voir locals
# ci-dessus). Passé à 1 uniquement si l'utilisateur isole volontairement
# ce rôle. JAMAIS > 1 : le singleton scheduler est une contrainte dure
# (scripts/validate_inventory.py), pas une question de scale.
resource "hcloud_server" "scheduler" {
  count        = var.scheduler_on_dedicated_node ? 1 : 0
  name         = "ladini-scheduler-1"
  server_type  = var.scheduler_server_type
  image        = var.app_image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.deploy.id]
  firewall_ids = [hcloud_firewall.app.id]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  network {
    network_id = hcloud_network.main.id
  }

  # Même correctif que hcloud_server.app ci-dessus — même clé, même règle
  # Hetzner (pas de virgule dans une valeur de label).
  labels = merge(var.labels, {
    role = "scheduler-admin"
  })

  user_data = templatefile("${path.module}/cloud-init/app-node.yaml.tpl", {
    deploy_user      = var.deploy_user
    ssh_public_key   = var.ssh_public_key
    git_repo_url     = var.git_repo_url
    git_ref          = var.git_ref
    public_domain    = var.public_domain
    acme_email       = var.acme_email
    node_name        = "ladini-scheduler-1"
    node_roles       = "scheduler,admin"
    private_net_cidr = var.network_ip_range
  })

  depends_on = [hcloud_network_subnet.app]
}
