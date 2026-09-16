# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/providers.tf — configuration du provider hcloud.
#
# Le token N'EST JAMAIS en dur ici : `var.hcloud_token` (sensitive), fourni
# via TF_VAR_hcloud_token, un fichier *.tfvars non commité, ou l'environnement
# CI (secret). Voir README.md pour les 3 façons de le passer.
# ═════════════════════════════════════════════════════════════════════
provider "hcloud" {
  token = var.hcloud_token
}
