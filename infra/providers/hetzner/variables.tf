# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/variables.tf — toutes les entrées de ce module.
#
# Rappel (voir README.md du dossier parent) : ce Terraform provisionne
# UNIQUEMENT l'infrastructure (VMs, réseau privé, firewall cloud, load
# balancer, placement group). Il ne déploie JAMAIS l'application — ça reste
# le rôle de `scripts/deploy.sh` (build-once / run-everywhere, images GHCR),
# lancé APRÈS `terraform apply`, une fois les nodes prêts.
# ═════════════════════════════════════════════════════════════════════

# ── Authentification ─────────────────────────────────────────────────

variable "hcloud_token" {
  description = "API token Hetzner Cloud (scope read+write, projet dédié). Ne JAMAIS committer — passer via TF_VAR_hcloud_token ou un *.tfvars gitignored."
  type        = string
  sensitive   = true
}

variable "ssh_public_key" {
  description = "Clé publique SSH (contenu, ex. 'ssh-ed25519 AAAA... deploy@ladini') injectée sur tous les nodes pour l'accès admin/déploiement. Générez une paire dédiée au déploiement, ne réutilisez pas une clé personnelle."
  type        = string
}

# ── Localisation ────────────────────────────────────────────────────

variable "location" {
  description = "Datacenter Hetzner pour les servers et le Load Balancer (ex: nbg1, fsn1, hel1, ash, hil). Doit appartenir à network_zone."
  type        = string
  default     = "nbg1"
}

variable "network_zone" {
  description = "Zone réseau Hetzner du réseau privé et du subnet (ex: eu-central pour nbg1/fsn1/hel1, us-east pour ash, us-west pour hil)."
  type        = string
  default     = "eu-central"
}

# ── Réseau privé ────────────────────────────────────────────────────

variable "network_ip_range" {
  description = "CIDR global du réseau privé Hetzner. Doit englober app_subnet_ip_range. Cohérent avec infra/inventory.example.yml (nodes en 10.20.1.0/24)."
  type        = string
  default     = "10.20.0.0/16"
}

variable "app_subnet_ip_range" {
  description = "CIDR du subnet applicatif (nodes app/scheduler/admin), sous-ensemble de network_ip_range."
  type        = string
  default     = "10.20.1.0/24"
}

# ── Nodes applicatifs ───────────────────────────────────────────────

variable "app_node_count" {
  description = "Nombre de nodes portant le profil Compose 'app' (api+worker+mcp+pgbouncer). Défaut 1 — Phase 1 du chantier scale-out (< 100 utilisateurs, voir infra/inventory.example.yml). Montez à 2+ progressivement ; le Load Balancer et le Placement Group s'adaptent automatiquement."
  type        = number
  default     = 1

  validation {
    condition     = var.app_node_count >= 1
    error_message = "app_node_count doit être >= 1 (au moins un node sert le trafic)."
  }
}

variable "app_server_type" {
  description = "Type de server Hetzner Cloud pour les nodes app (ex: cx22, cx32, cx42). Défaut cx22 = le plus petit type actuel avec 4 Go RAM, suffisant en Phase 1 (< 100 utilisateurs) — ne pas sur-provisionner par défaut (voir README.md § coûts)."
  type        = string
  default     = "cx22"
}

variable "app_image" {
  description = "Image OS de base pour les nodes app (nom ou ID hcloud). Ubuntu LTS — cloud-init/app-node.yaml.tpl est écrit pour un dérivé Debian/Ubuntu (apt, systemd)."
  type        = string
  default     = "ubuntu-24.04"
}

variable "scheduler_on_dedicated_node" {
  description = "Si true, le rôle 'scheduler' (Celery Beat, singleton — voir scripts/validate_inventory.py) tourne sur un node Hetzner DÉDIÉ (server séparé, hors du pool app_node_count) plutôt que sur le premier node app. Défaut false : le 1er node app (index 0) cumule app+scheduler+admin, comme documenté dans infra/inventory.example.yml Phase 1. Passez à true uniquement si vous isolez volontairement Beat/Flower (charge, blast radius) — coûte 1 server de plus."
  type        = bool
  default     = false
}

variable "scheduler_server_type" {
  description = "Type de server Hetzner pour le node scheduler DÉDIÉ (utilisé seulement si scheduler_on_dedicated_node = true). Beat/Flower sont légers — un type plus petit que app_server_type suffit."
  type        = string
  default     = "cx22"
}

# ── Firewall cloud (couche 2, en plus de infra/firewall/ufw.sh sur l'hôte) ──

variable "admin_cidrs" {
  description = "Liste de CIDR autorisés en SSH (port 22) sur les nodes, ex: [\"203.0.113.4/32\"] (votre IP fixe) ou un CIDR de bastion/VPN. JAMAIS 0.0.0.0/0 par défaut — laissé vide intentionnellement : Terraform refuse d'ouvrir le SSH tant que vous ne le renseignez pas explicitement (voir la validation ci-dessous)."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.admin_cidrs) > 0
    error_message = "admin_cidrs est vide — renseignez au moins un CIDR admin (ex: votre IP en /32) avant `terraform apply`, sinon le firewall Hetzner refuse la règle SSH (source_ips vide)."
  }

  validation {
    condition     = !contains(var.admin_cidrs, "0.0.0.0/0") && !contains(var.admin_cidrs, "::/0")
    error_message = "admin_cidrs ne doit JAMAIS contenir 0.0.0.0/0 ou ::/0 — restreignez le SSH à des IP/CIDR connus (votre poste, un bastion, un VPN)."
  }
}

# ── Load Balancer ───────────────────────────────────────────────────

variable "load_balancer_type" {
  description = "Type de Load Balancer Hetzner (lb11 = le plus petit, jusqu'à 25 services/targets — largement suffisant pour ce cluster à 1 seul service exposé)."
  type        = string
  default     = "lb11"
}

variable "public_domain" {
  description = "Nom de domaine public servi par le reverse proxy Caddy sur chaque node (infra/reverse-proxy/Caddyfile, variable PUBLIC_DOMAIN de son .env). Utilisé ici uniquement pour le health check HTTPS du Load Balancer (SNI) — Terraform ne gère PAS le DNS ni le certificat, voir README.md."
  type        = string
  default     = "api.ladini.com"
}

variable "acme_email" {
  description = "Adresse email de contact ACME utilisée par Caddy pour la gestion des certificats TLS."
  type        = string
}


# ── Déploiement (référencé par cloud-init, pas dupliqué) ────────────

variable "git_repo_url" {
  description = "URL HTTPS du dépôt Ladini cloné par cloud-init sur chaque node (checkout requis par scripts/deploy.sh, infra/firewall/ufw.sh, infra/reverse-proxy/ — tous des chemins relatifs à un checkout). Dépôt public : clone anonyme suffit. Dépôt privé : passez un token dans l'URL via une variable sensible séparée (non fait ici par défaut, voir README.md)."
  type        = string
  default     = "https://github.com/IDOEFRAIM/ladini.git"
}

variable "git_ref" {
  description = "Branche/tag cloné par cloud-init au premier boot. Un premier `git checkout` seulement — les déploiements suivants sont le rôle de scripts/deploy.sh, PAS de ce Terraform (qui ne tourne pas à chaque release)."
  type        = string
  default     = "main"
}

variable "deploy_user" {
  description = "Utilisateur Linux non-root créé sur chaque node, propriétaire du checkout et autorisé à lancer docker compose (groupe docker). C'est ce user que scripts/deploy.sh utilise via SSH."
  type        = string
  default     = "deploy"
}

# ── Labels ───────────────────────────────────────────────────────────

variable "labels" {
  description = "Labels hcloud communs appliqués à toutes les ressources (network, firewall, servers, load balancer, placement group) — filtrage/coût dans la console Hetzner."
  type        = map(string)
  default = {
    project     = "ladini"
    managed_by  = "terraform"
    environment = "production"
  }
}
