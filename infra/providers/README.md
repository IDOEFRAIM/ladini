# infra/providers/ — couche spécifique au fournisseur (mince)

> **99 % du déploiement est provider-neutral.** Ce dossier ne contient QUE ce
> qui dépend vraiment d'un fournisseur : provisionner la VM, le stockage, le
> réseau, la DB managée, le DNS. **Aucune logique de release** ici — celle-ci
> vit dans `scripts/`, `docker-compose.prod.yml`, les `Dockerfile`, les
> healthchecks, qui sont **identiques partout**.

## Ce qui NE change PAS entre fournisseurs

| Élément | Où |
|---|---|
| Construction des images (build-once) | `.github/workflows/release.yml`, `infra/docker/Dockerfile.*` |
| Registry (GHCR par défaut, surchargeable) | `REGISTRY` / `IMAGE_NAMESPACE` |
| Déploiement / rollback / preflight / smoke | `scripts/*.sh` |
| Compose de run | `docker-compose.prod.yml` |
| Reverse proxy + TLS | `infra/reverse-proxy/` (Caddy) |
| Pare-feu hôte | `infra/firewall/ufw.sh` (ufw) |
| Healthchecks, graceful shutdown, limites | `docker-compose.prod.yml` + Dockerfiles |
| Runbooks | `docs/runbooks/` |

## Ce qui change (et rien d'autre)

| Besoin | DigitalOcean | Hetzner | OVH | Scaleway | AWS EC2/Lightsail |
|---|---|---|---|---|---|
| Créer la VM Linux | droplet | Cloud server | Public Cloud instance | Instance | EC2 / Lightsail |
| Volume bloc (optionnel) | Volumes | Volumes | Additional Disk | Block Storage | EBS / Lightsail disk |
| Réseau / IP | Reserved IP | Primary IP | Floating IP | Flexible IP | Elastic IP |
| **DB Postgres managée** | Managed DB | (aucune — RDS-like absente : DB externe ou conteneur) | Cloud DB | Managed DB | RDS / Aurora |
| Firewall cloud (couche 2, optionnelle) | Cloud Firewall | Firewall | — | Security Group | Security Group |
| DNS | DO DNS | Hetzner DNS | OVH DNS | Domains | Route 53 |

### Convention

Si une automatisation propre à un fournisseur est nécessaire, la mettre dans
un sous-dossier dédié, **jamais** dans le chemin critique de `deploy.sh` :

```
infra/providers/
  digitalocean/     terraform / doctl scripts — provision VM + Managed DB + firewall
  aws/              terraform (déjà partiellement présent dans infra/terraform/) — EC2 + RDS + SG
  hetzner/          hcloud / terraform — Cloud server + volume
  README.md         (ce fichier)
```

Chaque sous-dossier expose **au minimum** :

- comment obtenir : `SSH host`, `SSH user`, `DB_HOST/PORT/USER/PASSWORD/NAME` ;
- comment ouvrir 22/80/443 côté firewall cloud (en plus de `infra/firewall/ufw.sh`) ;
- comment pointer le DNS `PUBLIC_DOMAIN` vers l'IP.

Une fois ces valeurs connues → `.env` + `./scripts/deploy.sh <release>`.
**Le reste est identique.**

## Test de portabilité (§46)

`scripts/test/run-scenarios.sh` prouve que `deploy.sh` / `rollback.sh` /
`preflight.sh` fonctionnent **sans** `doctl` / `aws` / `hcloud` (faux `docker`
+ faux `curl`, aucune metadata cloud). Sur une VM Linux nue avec Docker : la
seule dépendance est `docker`, `docker compose`, `curl`, `git`, coreutils.

## État actuel

- `infra/terraform/` : Terraform **AWS** existant (ECR + ECS + Lambda + S3 +
  SQS) — historique, orienté ECS. **Hors du chemin de déploiement Compose
  décrit ici.** À ranger sous `infra/providers/aws/` lors d'un prochain
  passage ; ne pas s'en servir pour le MVP VPS.
- `infra/aws/ladini_daily_advice/` : Lambda de conseil quotidien — fonction
  applicative séparée, sans lien avec la stack de release.
