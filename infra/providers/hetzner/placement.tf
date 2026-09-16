# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/placement.tf — Spread Placement Group Hetzner.
#
# Répartit les nodes app sur des hyperviseurs physiques distincts (best
# effort Hetzner — pas de garantie dure, mais réduit fortement la
# corrélation de panne). N'a de sens qu'à partir de 2 nodes : avec un seul
# node, un placement group est un no-op qui complique la lecture du plan
# Terraform pour rien — d'où la condition ci-dessous (count).
#
# Max 10 servers par placement group "spread" côté API Hetzner ; si
# app_node_count dépasse 10 un jour, il faudra plusieurs groupes — hors
# scope Phase 1/2 de ce chantier (< 100 utilisateurs).
# ═════════════════════════════════════════════════════════════════════

resource "hcloud_placement_group" "app" {
  count  = var.app_node_count >= 2 ? 1 : 0
  name   = "ladini-app-spread"
  type   = "spread"
  labels = var.labels
}
