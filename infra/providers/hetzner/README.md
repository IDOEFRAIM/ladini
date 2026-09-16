# `infra/providers/hetzner/` — Terraform (hcloud) pour le chantier scale-out

> Voir d'abord `infra/providers/README.md` (contrat provider-neutral). Ce
> dossier est le **seul** endroit spécifique à Hetzner. Il provisionne
> **UNIQUEMENT l'infrastructure** — VM, réseau privé, firewall cloud, Load
> Balancer, placement group. **Il ne déploie jamais l'application.** Le
> déploiement applicatif (pull d'images GHCR, migration, `up -d`, healthcheck,
> smoke test, rollback) reste **exclusivement** le rôle de `scripts/deploy.sh`,
> lancé séparément, après que les nodes existent et sont accessibles en SSH.

## Fichiers

| Fichier | Rôle |
|---|---|
| `versions.tf` | Contraintes de version Terraform + provider `hcloud` |
| `providers.tf` | Config du provider `hcloud` (token via variable, jamais en dur) |
| `variables.tf` | Toutes les entrées (token, clé SSH, CIDR admin, taille du cluster…) |
| `network.tf` | Réseau privé Hetzner `10.20.0.0/16` + subnet `10.20.1.0/24` |
| `firewall.tf` | Firewall Cloud Hetzner — default-deny, 22 (admin_cidrs)/80/443/443-udp/icmp seulement |
| `placement.tf` | Spread Placement Group — actif seulement si `app_node_count >= 2` |
| `servers.tf` | Nodes app (`count = var.app_node_count`) + node scheduler optionnel |
| `load_balancer.tf` | Load Balancer Hetzner → nodes app, TCP passthrough 80/443, health check `/health/ready` |
| `outputs.tf` | IP des nodes (privées/publiques), IP du LB, aide-mémoire SSH |
| `environments/production.tfvars.example` | Valeurs d'exemple, aucun secret réel |
| `cloud-init/app-node.yaml.tpl` | Bootstrap d'un node : Docker, user `deploy`, checkout du dépôt, `infra/firewall/ufw.sh`, placeholder Alloy |

## Prérequis

- Un compte Hetzner Cloud + un projet dédié.
- Un token API (scope read+write) généré dans la console du projet.
- Une paire de clés SSH **dédiée au déploiement** (pas votre clé perso) :
  `ssh-keygen -t ed25519 -f ~/.ssh/ladini_deploy -C "deploy@ladini"`.
- `terraform` >= 1.7 (ou `tofu` >= 1.7 — ce module n'utilise aucune syntaxe
  spécifique à l'un ou l'autre).

## Utilisation

```bash
cd infra/providers/hetzner
cp environments/production.tfvars.example environments/production.tfvars
$EDITOR environments/production.tfvars   # ssh_public_key, admin_cidrs, public_domain…

# Le token ne va JAMAIS dans un fichier .tfvars, même gitignored :
export TF_VAR_hcloud_token="votre_token_hcloud"   # PowerShell : $env:TF_VAR_hcloud_token = "..."

terraform init
terraform plan  -var-file=environments/production.tfvars
terraform apply -var-file=environments/production.tfvars
```

Après `apply` :

1. `terraform output app_nodes` donne les IP (publiques + privées) et rôles
   de chaque node → reportez-les dans `infra/inventory.yml` (copié depuis
   `infra/inventory.example.yml`, gitignored), puis validez :
   `python scripts/validate_inventory.py`.
2. Pointez l'enregistrement DNS A de `PUBLIC_DOMAIN` vers
   `terraform output load_balancer_ipv4`.
3. Sur **chaque** node : `cd infra/reverse-proxy && cp .env.example .env &&
   $EDITOR .env && docker compose up -d` (TLS/Caddy — cycle de vie séparé
   de l'appli, voir son propre README/commentaires).
4. Remplissez `.env` (secrets applicatifs — jamais dans Terraform/cloud-init,
   voir `.env.example` à la racine) sur chaque node app.
5. `./scripts/deploy.sh <release>` depuis un node (ou via CI/SSH) — c'est
   **cette étape**, pas Terraform, qui démarre réellement l'application.

Attendez ~1-2 minutes après la création d'un node avant de vous y connecter :
`cloud-init` (Docker, checkout, `ufw.sh`) doit terminer. Suivez sa progression
avec `ssh deploy@<ip> 'cloud-init status --wait'`.

## Comment `app_node_count` scale (1 → 2 → N)

1. **Phase 1 (défaut, < 100 utilisateurs)** : `app_node_count = 1`. Ce node
   unique cumule les 3 profils Compose (`app`, `scheduler`, `admin`) — voir
   `infra/inventory.example.yml`. Aucun Load Balancer n'est *nécessaire* à ce
   stade mais il est créé quand même (coût faible, simplifie la bascule vers
   la Phase 2 : le DNS pointe déjà vers le LB, jamais vers une IP de node).

2. **Phase 2** : montez `app_node_count` (ex: `2`). Terraform :
   - crée un nouveau `hcloud_server.app[1]` (aucun changement sur `[0]`,
     `count` ajoute sans recréer les nodes existants) ;
   - crée automatiquement le `hcloud_placement_group` (condition
     `app_node_count >= 2`, voir `placement.tf`) et y assigne tous les nodes
     app existants ⚠️ **cela force un remplacement (`ForceNew`) des nodes déjà
     créés**, car `placement_group_id` ne peut être défini qu'à la création
     d'un server côté API Hetzner — `terraform plan` vous le montrera
     explicitement avant tout `apply` (nodes détruits/recréés). Si vous
     voulez éviter cette recréation en Phase 1→2, démarrez directement à
     `app_node_count = 2` dès le premier `apply`.
   - ajoute automatiquement le nouveau node comme target du Load Balancer
     (`hcloud_load_balancer_target`, `count = var.app_node_count`).
   - le node d'index 0 **reste** le seul à porter `scheduler`+`admin` (voir
     `servers.tf` locals) — jamais un 2e Beat.
   - reportez le nouveau node dans `infra/inventory.yml` avec `roles: [app]`
     uniquement, puis `python scripts/validate_inventory.py`.

3. **N nodes** : répétez — augmentez `app_node_count`, `apply`, mettez à jour
   `infra/inventory.yml`. Le Load Balancer et le firewall n'ont besoin
   d'aucun changement manuel.

4. **Isoler `scheduler`/`admin` sur un node dédié** (optionnel, à tout
   moment) : passez `scheduler_on_dedicated_node = true`. Un
   `hcloud_server.scheduler` séparé est créé (hors `app_node_count`, hors
   Load Balancer — il ne sert jamais de trafic HTTP) ; retirez alors
   `scheduler`/`admin` du node app d'index 0 dans `infra/inventory.yml`.

## Notes de conception importantes

- **TCP passthrough, pas de TLS au niveau du Load Balancer.** Chaque node
  fait tourner son propre Caddy (`infra/reverse-proxy/`) qui gère lui-même
  le certificat Let's Encrypt. Terminer le TLS au LB casserait le
  renouvellement ACME (challenge HTTP-01 par node) — voir le commentaire en
  tête de `load_balancer.tf`.
- **Health check du LB = `/health/ready`** (vraie vérification DB+Redis,
  `backend/src/ladini/api/main.py`), en HTTPS direct vers Caddy sur chaque
  node (port 443, `tls = true` sans validation de CA — Hetzner ignore la
  chaîne de confiance, seule la réponse HTTP compte). Le service port 80 se
  contente d'un health check TCP (`/health/live` répondrait par une
  redirection HTTPS automatique de Caddy, jamais un 200 — voir commentaire
  dans `load_balancer.tf`).
- **Firewall cloud = couche 2**, en plus de `infra/firewall/ufw.sh` sur
  chaque hôte (défense en profondeur). Aucun des deux n'ouvre jamais Redis
  (6379), PgBouncer (6432), MCP (8003) ou Flower (5555) publiquement — ils
  restent sur le réseau privé Hetzner (`network.tf`) ou en loopback (voir
  `docker-compose.prod.yml`, tunnel SSH pour Flower).
- **`admin_cidrs` est vide par défaut et Terraform refuse `0.0.0.0/0`**
  (validation dans `variables.tf`) — vous DEVEZ renseigner explicitement vos
  IP/CIDR admin, jamais d'ouverture SSH universelle par défaut.
- **cloud-init ne duplique pas** `infra/firewall/ufw.sh` : il clone le dépôt
  puis exécute ce script tel quel. Si le script évolue, le comportement du
  node évolue avec, sans toucher à ce Terraform.
- **Aucun secret dans `user_data`/cloud-init.** Le `.env` applicatif (clés
  LLM, tokens webhooks, mot de passe DB/Redis…) n'est jamais écrit par
  Terraform — `user_data` est lisible par l'API metadata Hetzner depuis le
  node lui-même, ce n'est pas un canal pour des secrets à protéger.

## Coûts — ne pas sur-provisionner par défaut

- Défaut : **1 node `cx22`** (~4 Go RAM) + **1 Load Balancer `lb11`** (le
  plus petit type). Pas de placement group tant que `app_node_count < 2`
  (une ressource Hetzner de plus, sans bénéfice à 1 seul node).
- Le réseau privé et le firewall cloud sont gratuits chez Hetzner — aucun
  coût à les garder même en Phase 1.
- N'augmentez `app_node_count`, `app_server_type` ou
  `scheduler_on_dedicated_node` que lorsque la charge réelle le justifie
  (voir les métriques Prometheus/Sentry déjà en place côté appli). Ce
  module est conçu pour scaler à la demande, pas pour partir large.

## Ce qui a été validé dans cet environnement (et ce qui ne l'a pas été)

- `terraform`/`tofu` ne sont **pas installés** sur la machine où ce module a
  été écrit — `terraform init`/`validate`/`fmt` n'ont **pas pu être
  exécutés**. Tous les noms de ressources/arguments (`hcloud_network`,
  `hcloud_network_subnet`, `hcloud_firewall`, `hcloud_placement_group`,
  `hcloud_server`, `hcloud_load_balancer`, `hcloud_load_balancer_network`,
  `hcloud_load_balancer_target`, `hcloud_load_balancer_service`,
  `hcloud_ssh_key`, le bloc `algorithm { type = "least_connections" }`, le
  bloc `health_check`/`http` avec `tls`) ont été vérifiés contre la
  documentation officielle du provider `hetznercloud/hcloud` (version
  `v1.69.0`, la plus récente au 2026-09-16) directement sur GitHub — pas
  deviné. **Avant le premier `apply` réel**, lancez `terraform init &&
  terraform validate` localement (où le binaire est disponible) pour
  confirmer, et committez le `.terraform.lock.hcl` généré.
- Aucun `terraform apply`/`plan` n'a été exécuté contre de vraies
  identifiants Hetzner — aucune infrastructure réelle n'a été provisionnée.
