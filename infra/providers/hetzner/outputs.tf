# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/outputs.tf — valeurs consommées après `apply` :
# remplir infra/inventory.yml (copié depuis infra/inventory.example.yml,
# gitignored), pointer le DNS PUBLIC_DOMAIN, alimenter scripts/deploy.sh
# via SSH. Rien ici n'est un secret (token/clé privée jamais en output).
# ═════════════════════════════════════════════════════════════════════

output "app_nodes" {
  description = "Nodes app (index 0 = app+scheduler+admin sauf si scheduler_on_dedicated_node=true) : nom, IP publique, IP privée, rôles — à reporter dans infra/inventory.yml."
  value = [
    for i, s in hcloud_server.app : {
      name      = s.name
      public_ip = s.ipv4_address
      # `network` est un bloc imbriqué représenté par un SET (pas une liste) —
      # non indexable par [0] (confirmé par `terraform validate`, corrigé
      # 2026-09-16). Le splat `[*]` fonctionne sur un set (conversion
      # implicite en liste) ; chaque node n'a qu'un seul réseau privé attaché
      # ici (voir network.tf), donc `[0]` après le splat est sûr.
      private_ip = s.network[*].ip[0]
      roles      = local.app_node_roles[i]
    }
  ]
}

output "scheduler_node" {
  description = "Node scheduler dédié (null si scheduler_on_dedicated_node=false, auquel cas Beat/Flower tournent sur app_nodes[0])."
  value = length(hcloud_server.scheduler) > 0 ? {
    name       = hcloud_server.scheduler[0].name
    public_ip  = hcloud_server.scheduler[0].ipv4_address
    private_ip = hcloud_server.scheduler[0].network[*].ip[0]
    roles      = ["scheduler", "admin"]
  } : null
}

output "load_balancer_ipv4" {
  description = "IP publique IPv4 du Load Balancer — c'est CETTE IP que le DNS PUBLIC_DOMAIN (registrar / infra/reverse-proxy) doit cibler en enregistrement A."
  value       = hcloud_load_balancer.app.ipv4
}

output "load_balancer_ipv6" {
  description = "IP publique IPv6 du Load Balancer — enregistrement AAAA optionnel."
  value       = hcloud_load_balancer.app.ipv6
}

output "network_id" {
  description = "ID du réseau privé Hetzner (référence pour debug/console, rarement nécessaire en usage courant)."
  value       = hcloud_network.main.id
}

output "ssh_command_hint" {
  description = "Rappel : comment se connecter à chaque node app une fois `apply` terminé (attendre ~1-2 min que cloud-init termine)."
  value = [
    for s in hcloud_server.app : "ssh ${var.deploy_user}@${s.ipv4_address}"
  ]
}
