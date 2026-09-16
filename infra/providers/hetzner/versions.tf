# ═════════════════════════════════════════════════════════════════════
# infra/providers/hetzner/versions.tf — contraintes de version (§ IaC
# Hetzner, chantier scale-out). Épinglé volontairement : un `terraform init`
# reproductible ne doit jamais dépendre de "la dernière version au moment du
# apply". Le lockfile (.terraform.lock.hcl) est COMMIT — voir README.md.
#
# Provider hcloud : dernière version stable vérifiée le 2026-09-16 sur le
# registre Terraform (registry.terraform.io/providers/hetznercloud/hcloud) :
# v1.69.0. Contrainte pessimiste `~> 1.69` : accepte les correctifs 1.69.x,
# refuse tout saut mineur (1.70+) qui pourrait renommer/déprécier un
# argument sans revue explicite.
# ═════════════════════════════════════════════════════════════════════
terraform {
  required_version = ">= 1.7.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.69"
    }
  }
}
