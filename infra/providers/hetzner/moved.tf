# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/moved.tf — migration d'identité `count` → `for_each`
# (2026-09-19, audit scale-out).
#
# `hcloud_server.app` et `hcloud_load_balancer_target.app` sont passés d'un
# adressage par INDEX (`[0]`, `[1]`…) à un adressage par CLÉ STABLE
# (`["1"]`, `["2"]`…) — voir servers.tf pour le pourquoi. Ces blocs `moved`
# indiquent à Terraform de RENOMMER l'entrée d'état existante plutôt que de
# détruire l'ancienne adresse et en créer une nouvelle — AUCUNE ressource
# réelle n'est affectée, ceci est une opération PUREMENT sur l'état.
#
# Validé (2026-09-19) par un `terraform plan -refresh=false` offline contre
# l'état réel de production : avec ces blocs présents, le plan à
# `app_node_count` INCHANGÉ ne montre plus AUCUNE ligne pour
# `hcloud_server.app[0]`/`hcloud_load_balancer_target.app[0]` — seulement
# `hcloud_server.app["1"]`/`hcloud_load_balancer_target.app["1"]` déjà
# considérés comme le MÊME objet que l'état existant (aucun add/change/
# destroy imputable à cette seule migration).
#
# ⚠️ À GARDER dans le dépôt au moins jusqu'au premier `terraform apply` qui
# suit ce chantier (celui qui fera réellement converger l'état de production
# vers ces nouvelles adresses). Une fois cet `apply` confirmé réussi
# (`terraform state list` montre `hcloud_server.app["1"]`, plus `[0]`), ces
# blocs peuvent être retirés en toute sécurité — Terraform n'en a plus
# besoin après coup, un `moved` ne sert qu'à la PROCHAINE convergence
# d'état, pas à une mémoire permanente.
# ═════════════════════════════════════════════════════════════════════

moved {
  from = hcloud_server.app[0]
  to   = hcloud_server.app["1"]
}

moved {
  from = hcloud_load_balancer_target.app[0]
  to   = hcloud_load_balancer_target.app["1"]
}
