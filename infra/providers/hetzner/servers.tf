# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/servers.tf — nodes Hetzner Cloud.
#
# Rôles Compose (docker-compose.prod.yml `profiles:`) : app / scheduler /
# admin — voir infra/inventory.example.yml pour la contrainte (EXACTEMENT
# UN node `scheduler` dans tout le cluster, jamais 0, jamais 2+).
#
# Par défaut (var.scheduler_on_dedicated_node = false) : le node app "1"
# cumule app+scheduler+admin — comme documenté en Phase 1 dans
# infra/inventory.example.yml. C'est le choix par défaut le plus
# économique (pas de server supplémentaire) et cohérent avec le MVP
# actuel. Passez scheduler_on_dedicated_node à true pour isoler Beat/Flower
# sur un server séparé (utile si la charge scheduler/admin perturbe le
# node app, ou pour réduire le blast radius d'un redémarrage app).
#
# ── IDENTITÉ STABLE : for_each, pas count (2026-09-19, audit scale-out) ──
# Historiquement `count = var.app_node_count` (index 0..N-1). Pour un
# scale-out pur (ajout en fin de liste), `count` ne recrée PAS les
# instances de plus BAS index — ce n'est donc pas la cause du bug ayant
# motivé ce changement (voir plus bas, `lifecycle.ignore_changes`). On migre
# quand même vers `for_each` avec des clés STRING stables ("1", "2", "3"…)
# pour deux raisons qui, elles, comptent réellement :
#   1. Le futur SCALE-IN (retirer un node précis, ex. "app-2" unhealthy,
#      en gardant "app-3") — avec `count`, retirer autre chose que le
#      DERNIER index décale tous les index suivants et force leur
#      recréation. Avec `for_each`, l'adresse d'un node ne dépend QUE de
#      sa propre clé, jamais de la taille du cluster.
#   2. Documenter explicitement le concept "cattle nodes nommés" plutôt que
#      des positions numériques implicites.
# Migration faite avec `moved` (voir moved.tf) — AUCUNE destruction d'état,
# validé par `terraform plan` (0 to add/change/destroy sur la seule
# migration d'adresse, voir docs/ dans ce dossier / le rapport d'audit).
# ═════════════════════════════════════════════════════════════════════

resource "hcloud_ssh_key" "deploy" {
  name       = "ladini-deploy-key"
  public_key = var.ssh_public_key
  labels     = var.labels
}

locals {
  # Clés stables "1".."N" — PAS des index de liste. `tostring(i + 1)`
  # produit exactement les mêmes suffixes de nom que l'ancien schéma basé
  # sur `count.index + 1` ("1" = ladini-app-1, l'unique node de production
  # actuel), donc aucun renommage de server au premier `apply` qui suit
  # cette migration.
  #
  # ⚠️ Tri lexicographique au-delà de 9 nodes ("10" < "2") : SANS IMPACT
  # fonctionnel ici (chaque node est adressé par sa clé, jamais par un
  # ordre de tri), seulement sur l'ordre d'affichage de `terraform plan`/
  # `output`. Non corrigé délibérément (zero-padding ajouterait de la
  # complexité pour un cas hors scope actuel) — `hcloud_placement_group`
  # (placement.tf) plafonne déjà à 10 servers/groupe "spread" pour la même
  # raison de scope (< 100 utilisateurs, voir ce fichier).
  app_node_keys = toset([for i in range(var.app_node_count) : tostring(i + 1)])

  # Rôles par node app (clé "1" = app+scheduler+admin si le scheduler n'est
  # PAS dédié ; sinon tous les nodes app ne portent que 'app').
  app_node_roles = {
    for k in local.app_node_keys :
    k => (k == "1" && !var.scheduler_on_dedicated_node) ? ["app", "scheduler", "admin"] : ["app"]
  }

  placement_group_id = length(hcloud_placement_group.app) > 0 ? hcloud_placement_group.app[0].id : null
}

resource "hcloud_server" "app" {
  for_each     = local.app_node_keys
  name         = "ladini-app-${each.key}"
  server_type  = var.app_server_type
  image        = var.app_image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.deploy.id]
  firewall_ids = [hcloud_firewall.app.id]

  # ── Placement group : JAMAIS sur le node "1" — PRIORITÉ ABSOLUE = ──
  # aucune destruction/mutation à risque du serveur de production existant
  # (2026-09-19, audit scale-out). Le schéma du provider hcloud v1.69.0
  # (`terraform providers schema -json`) ne marque PAS `placement_group_id`
  # comme `force_new` et un `terraform plan` réel confirme un simple
  # changement en place (`~`, jamais `# forces replacement`) — mais en
  # l'absence d'un test EMPIRIQUE contre la vraie API Hetzner (aucun accès
  # réseau/identifiants pendant cet audit), on retient la position la plus
  # sûre demandée : le node "1" (production actuelle) ne rejoint JAMAIS
  # automatiquement le placement group. Les nodes "2", "3"… qui n'existent
  # pas encore aujourd'hui en bénéficient dès leur création (aucun risque,
  # `placement_group_id` est alors posé à la CRÉATION, jamais en mutation).
  # Si l'équipe veut un jour y intégrer le node "1", ce sera un choix
  # explicite et testé isolément — jamais un effet de bord d'un scale-out.
  placement_group_id = each.key == "1" ? null : local.placement_group_id

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  network {
    network_id = hcloud_network.main.id
    # Pas d'`ip` figé : laisse hcloud allouer dans app_subnet_ip_range.
    # Les IP privées réelles vont dans infra/inventory.yml (gitignored,
    # rempli après `terraform apply` — voir scripts/generate_inventory.py,
    # qui lit l'output `app_nodes` pour éliminer l'étape manuelle).
  }

  # (2026-09-17) — les labels Hetzner Cloud suivent la syntaxe des labels
  # Kubernetes : valeur alphanumérique + `-_.` uniquement, JAMAIS de virgule
  # (confirmé par un vrai `terraform plan` : "label value 'app,scheduler,
  # admin' (key: role) is not correctly formatted"). `node_roles` plus bas
  # (passé au cloud-init, pas un label Hetzner) garde la virgule — seul CE
  # label change de séparateur.
  labels = merge(var.labels, {
    role = join("-", local.app_node_roles[each.key])
  })

  user_data = templatefile("${path.module}/cloud-init/app-node.yaml.tpl", {
    deploy_user               = var.deploy_user
    ssh_public_key            = var.ssh_public_key
    github_actions_public_key = var.github_actions_public_key
    node_name                 = "ladini-app-${each.key}"
    node_roles                = join(",", local.app_node_roles[each.key])
    private_net_cidr          = var.network_ip_range
  })

  # ═══════════════════════════════════════════════════════════════════
  # CAUSE RACINE DU BUG DE SCALE-OUT (2026-09-19, audit) — PAS `count` vs
  # `for_each`, PAS `placement_group_id` : c'est CETTE ligne qui manquait.
  #
  # `user_data` ne s'exécute QU'UNE FOIS, au tout premier boot (cloud-init).
  # L'API Hetzner n'offre aucun moyen de "ré-appliquer" un user_data sur un
  # server existant — la SEULE façon dont le provider peut faire converger
  # un `user_data` différent est de DÉTRUIRE puis RECRÉER le server (confirmé
  # empiriquement : `terraform plan` marque cette ligne, et SEULEMENT
  # celle-ci, `# forces replacement`).
  #
  # Preuve que ceci est INDÉPENDANT de `app_node_count` (donc pas un effet
  # du scale-out en lui-même) : un `terraform plan` à `app_node_count = 1`
  # INCHANGÉ (aucun scale-out, configuration identique à la prod réelle)
  # affiche DÉJÀ ce même remplacement de `ladini-app-1`, avec le MÊME hash
  # de user_data. Root cause réelle : le commit `7dd474f` ("secure private
  # repository deployment bootstrap", 2026-09-17) a réécrit en profondeur
  # `cloud-init/app-node.yaml.tpl` — nouvelle variable
  # `github_actions_public_key`, suppression du `git clone` embarqué,
  # réécriture du pare-feu bootstrap — APRÈS que `ladini-app-1` avait déjà
  # été provisionné avec l'ANCIENNE version du template. Depuis ce commit,
  # N'IMPORTE QUEL `terraform plan` (scale-out ou non) redemande la
  # destruction/recréation de ce server pour "appliquer" un user_data que,
  # par nature, il ne peut de toute façon jamais réappliquer sans down time.
  #
  # Design retenu (PAS un cache-misère aveugle — voir docstring du fichier
  # cloud-init lui-même, déjà écrit dans cet esprit AVANT ce correctif) :
  # le bootstrap `cloud-init` est délibérément un événement UNIQUE, IMMUABLE,
  # par design "cattle" — toute évolution de configuration RUNTIME (clés,
  # pare-feu applicatif, checkout du dépôt…) est déjà prise en charge
  # ENSUITE par GitHub Actions / scripts/node_deploy.sh, jamais en
  # ré-exécutant cloud-init. Éditer ce template sert à préparer le bootstrap
  # des FUTURS nodes, jamais à muter rétroactivement un node déjà en vie —
  # `ignore_changes` encode exactement cette intention architecturale.
  # Conséquence assumée : une vraie rotation de clé SSH admin/CI nécessite
  # une action EXPLICITE et visible (`terraform apply -replace=
  # 'hcloud_server.app["1"]'`), jamais un `apply` de routine qui la
  # déclenche par accident.
  lifecycle {
    ignore_changes = [user_data]
  }

  # Le subnet doit exister avant qu'un server puisse s'y attacher.
  depends_on = [hcloud_network_subnet.app]
}

# ── Node scheduler DÉDIÉ (optionnel — var.scheduler_on_dedicated_node) ──
# count = 0 par défaut (Beat/Flower vivent sur ladini-app-1, voir locals
# ci-dessus). Passé à 1 uniquement si l'utilisateur isole volontairement
# ce rôle. JAMAIS > 1 : le singleton scheduler est une contrainte dure
# (scripts/validate_inventory.py), pas une question de scale — `count`
# (0/1, jamais un ensemble qui grandit) reste le bon outil ici, pas
# `for_each` (aucun bénéfice d'identité stable sur un singleton optionnel).
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
    deploy_user               = var.deploy_user
    ssh_public_key            = var.ssh_public_key
    github_actions_public_key = var.github_actions_public_key
    node_name                 = "ladini-scheduler-1"
    node_roles                = "scheduler,admin"
    private_net_cidr          = var.network_ip_range
  })

  # Même raisonnement que hcloud_server.app ci-dessus : bootstrap immuable,
  # jamais réappliqué après le premier boot.
  lifecycle {
    ignore_changes = [user_data]
  }

  depends_on = [hcloud_network_subnet.app]
}
