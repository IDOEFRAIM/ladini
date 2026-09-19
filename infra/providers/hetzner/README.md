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
| `servers.tf` | Nodes app (`for_each` sur des clés stables `"1".."N"`, voir § identité stable) + node scheduler optionnel |
| `moved.tf` | Migration d'état `count` → `for_each` (2026-09-19) — voir son en-tête, à retirer après le premier `apply` qui suit |
| `load_balancer.tf` | Load Balancer Hetzner → nodes app, TCP passthrough 80/443, health check `/health/ready` |
| `outputs.tf` | IP des nodes (privées/publiques), IP du LB, aide-mémoire SSH — consommé par `scripts/generate_inventory.py` |
| `environments/production.tfvars.example` | Valeurs d'exemple, aucun secret réel |
| `cloud-init/app-node.yaml.tpl` | Bootstrap d'un node, **immuable après le premier boot** (voir `lifecycle.ignore_changes` dans `servers.tf`) : Docker, user `deploy`, pare-feu hôte minimal, réseau privé. Ne clone jamais le dépôt — le checkout et le déploiement applicatif restent le rôle de GitHub Actions / `scripts/node_deploy.sh` |

Scripts associés (racine du dépôt, pas dans ce dossier — provider-neutral, voir `infra/providers/README.md`) :

| Script | Rôle |
|---|---|
| `scripts/generate_inventory.py` | Génère `infra/inventory.yml` depuis `terraform output -json` — plus d'édition manuelle des IP/rôles |
| `scripts/test_scale_out_plan.sh` | Verrou de non-régression : prouve qu'un `terraform plan` de scale-out ne détruit/remplace aucune ressource existante |
| `scripts/scale_in_drain.sh` | Drain applicatif d'un node (LB + Celery) AVANT de le retirer par Terraform — voir § Scale-in |

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

1. `python scripts/generate_inventory.py` génère `infra/inventory.yml`
   directement depuis `terraform output` (IP privées + rôles) — plus besoin
   de le copier/éditer à la main depuis `infra/inventory.example.yml`. Il
   auto-valide son propre résultat (`scripts/validate_inventory.py`) avant
   d'écrire quoi que ce soit.
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
`cloud-init` (réseau privé, Docker, pare-feu hôte minimal — voir
`cloud-init/app-node.yaml.tpl`) doit terminer. Suivez sa progression avec
`ssh deploy@<ip> 'cloud-init status --wait'`.

**⚠️ L'ORDRE 2 → 3 ci-dessus n'est pas arbitraire — ne l'inversez jamais**
(2026-09-17, audit bootstrap Cloudflare/LB). Caddy (étape 3) demande son
certificat Let's Encrypt (challenge HTTP-01) dès son démarrage, en visant le
VRAI nom de domaine public (`PUBLIC_DOMAIN`) — si le DNS (étape 2) n'existe
pas encore à ce moment-là, le challenge échoue et Caddy réessaie en
arrière-plan avec un backoff croissant (il ne plante pas, ne bloque pas
`docker compose up`, mais 443 restera un handshake TLS invalide jusqu'à
obtention du certificat). Respecter l'ordre documenté (DNS avant Caddy)
évite cette fenêtre "unhealthy" entièrement. Si vous l'inversez quand même
(ou si la propagation DNS/Cloudflare prend du temps) :
- `terraform apply` **n'échoue jamais** à cause de ça — le healthcheck HTTPS
  du Load Balancer (`load_balancer.tf`) est un état POST-apply, jamais une
  condition bloquante pour `apply` lui-même ;
- le check `/health/ready` du LB affichera simplement le target `unhealthy`
  jusqu'à ce que Caddy obtienne son certificat (généralement quelques
  minutes après que le DNS résout correctement) — aucune intervention
  manuelle requise au-delà d'attendre, pas de deadlock : Caddy retente son
  ACME tout seul, il n'a besoin d'aucun redémarrage.
- le healthcheck LUI-MÊME ne dépend jamais du DNS : le Load Balancer parle
  DIRECTEMENT à l'IP privée du node (`use_private_ip = true`), avec
  `domain = PUBLIC_DOMAIN` utilisé seulement comme SNI/Host — la résolution
  DNS publique n'entre jamais en jeu pour CE check précis, seule l'obtention
  du certificat par Caddy en dépend.

## Identité stable des nodes (`for_each`, pas `count`)

`hcloud_server.app` et `hcloud_load_balancer_target.app` sont indexés par une
clé **string stable** (`"1"`, `"2"`, `"3"`…, dérivée de `app_node_count` mais
jamais recalculée à l'envers) — pas par position de liste. `ladini-app-1`
(clé `"1"`) reste `"1"` quel que soit le nombre total de nodes, aujourd'hui
et pour toujours. Migration effectuée le 2026-09-19 depuis l'ancien schéma
`count` via des blocs `moved` (`moved.tf`) — **aucune destruction d'état**,
validée par un `terraform plan` qui ne montre que des renommages d'adresse
(`has moved to`), jamais un `add`/`destroy`.

## ⚠️ Pourquoi `ladini-app-1` semblait devoir être recréé — cause racine (2026-09-19)

Un `terraform plan` (même sans AUCUN scale-out, `app_node_count` inchangé)
proposait de **détruire et recréer** `ladini-app-1` — et pas seulement au
moment d'un scale-out, ce qui a été mal interprété au départ. Root cause
identifiée et corrigée :

- **Ce n'était PAS `app_node_count`.** Preuve : un `terraform plan` à
  `app_node_count = 1`, configuration par ailleurs strictement identique à
  la prod réelle, proposait déjà exactement le même remplacement.
- **Ce n'était PAS `placement_group_id`.** Ce champ n'est pas `force_new`
  dans le schéma du provider `hcloud` v1.69.0 et un `terraform plan` réel
  le confirme (changement en place `~`, jamais `# forces replacement`).
- **La vraie cause : `user_data`.** Le commit `7dd474f` ("secure private
  repository deployment bootstrap", 2026-09-17) a réécrit en profondeur
  `cloud-init/app-node.yaml.tpl` (nouvelle variable
  `github_actions_public_key`, suppression du `git clone` embarqué,
  réécriture du pare-feu bootstrap) **après** que `ladini-app-1` avait déjà
  été provisionné avec l'ancienne version du template. `user_data` ne
  s'exécute qu'**une seule fois**, au premier boot — Hetzner n'offre aucun
  moyen de le "réappliquer" sur un server vivant, donc la SEULE façon dont
  Terraform peut faire converger un `user_data` différent est de détruire
  et recréer le server (`terraform plan` le marque explicitement
  `# forces replacement`, et seulement lui).
- **Correctif** : `lifecycle { ignore_changes = [user_data] }` sur
  `hcloud_server.app`/`hcloud_server.scheduler` (voir `servers.tf`, section
  commentée "CAUSE RACINE"). Design assumé, pas un cache-misère : le
  bootstrap cloud-init est un événement **unique et immuable** par nature
  ("cattle", pas "pet") — toute évolution de configuration RUNTIME (clés,
  checkout du dépôt…) passe déjà par GitHub Actions/`scripts/node_deploy.sh`,
  jamais par une ré-exécution de cloud-init. Éditer ce template prépare le
  bootstrap des **futurs** nodes, jamais une mutation rétroactive d'un node
  déjà vivant. Une vraie rotation de clé SSH admin/CI nécessite désormais un
  geste **explicite** : `terraform apply -replace='hcloud_server.app["1"]'`
  (jamais un `apply` de routine qui la déclenche par accident).

## SCALE OUT — procédure exacte (1 → 2 → N)

1. Modifiez `app_node_count` dans `environments/production.tfvars` (ex: `1` → `2`).
2. `terraform plan -var-file=environments/production.tfvars`
3. **Vérifiez `0 to destroy`** dans le résumé du plan (et aucune ligne
   `must be replaced` sur un node existant) — sinon **n'appliquez pas**,
   investiguez (voir `scripts/test_scale_out_plan.sh` pour automatiser
   exactement cette vérification).
4. `terraform apply -var-file=environments/production.tfvars`
5. `python scripts/generate_inventory.py` — régénère `infra/inventory.yml`
   depuis `terraform output` (IP privées + rôles, plus d'édition manuelle).
   Le nouveau node hérite automatiquement de `roles: [app]` uniquement —
   jamais `scheduler`/`admin` (voir `servers.tf::local.app_node_roles`,
   seule la clé `"1"` les porte).
6. `python scripts/validate_inventory.py` — doit confirmer exactement un
   node `scheduler` dans tout le cluster.
7. Déployez la release courante sur le cluster (inclut le nouveau node) :
   `./scripts/cluster_deploy.sh <release>` — valide l'inventaire, la
   connectivité SSH de TOUS les nodes, migre la DB une seule fois, déploie
   node par node, vérifie `/health/ready` sur chacun puis (si
   `PUBLIC_DOMAIN` est fourni) le chemin public/LB.
8. Confirmez : `terraform output ssh_command_hint`, `curl
   https://<PUBLIC_DOMAIN>/health/ready`, et dans la console Hetzner que le
   nouveau target du Load Balancer est `healthy`.

**Isoler `scheduler`/`admin` sur un node dédié** (optionnel, à tout
moment) : passez `scheduler_on_dedicated_node = true`. Un
`hcloud_server.scheduler` séparé est créé (hors `app_node_count`, hors
Load Balancer — il ne sert jamais de trafic HTTP) ; régénérez l'inventaire
(étape 5 ci-dessus) — le node app `"1"` ne porte alors plus que `[app]`.

## SCALE IN — procédure exacte et volontaire (N → N-1)

**Jamais** un simple `app_node_count -= 1` suivi d'un `apply` à froid : le
node visé (le plus haut numéroté, ex. `ladini-app-3` sur un cluster de 3)
peut porter du trafic HTTP en cours ou des tâches Celery en vol. Procédure :

1. **Drain applicatif** — `./scripts/scale_in_drain.sh <node_name>` :
   arrête `api` (le Load Balancer cesse de router du nouveau trafic dès son
   prochain health check `/health/ready` en échec), attend une marge de
   sécurité, puis arrête `mcp`/`worker` (grace period respectée, tâches
   Celery déjà dispatchées vers ce node laissées se terminer). Refuse
   explicitement de draîner un node qui porte `scheduler` — voir sa sortie
   pour la marche à suivre (réassigner Beat AVANT de continuer).
2. Décrémentez `app_node_count` dans `environments/production.tfvars`.
3. `terraform plan -var-file=environments/production.tfvars` — vérifiez
   que **seules** les ressources du node drainé apparaissent en
   `destroy`/`will be destroyed` (son `hcloud_server.app[...]` et son
   `hcloud_load_balancer_target.app[...]`), **rien d'autre**.
4. `terraform apply -var-file=environments/production.tfvars`
5. `python scripts/generate_inventory.py` puis
   `python scripts/validate_inventory.py` — le node retiré disparaît de
   `infra/inventory.yml` automatiquement (il n'existe plus dans le state
   Terraform, donc plus dans `terraform output app_nodes`).
6. Confirmez dans la console Hetzner que le Load Balancer n'a plus ce
   target, et que le reste du cluster reste `healthy`.

## Notes de conception importantes

- **Chemin complet, TLS terminé deux fois — délibéré, pas un doublon
  accidentel** (2026-09-17, audit Cloudflare/LB) :
  ```
  Client
    → TLS #1 : Cloudflare edge (certificat géré par Cloudflare)
    → Cloudflare (WAF/cache/proxy — record DNS "proxied", nuage orange)
    → TLS #2 (nouvelle connexion) : Cloudflare → Hetzner LB (IP publique du LB)
    → Load Balancer Hetzner : TCP passthrough pur, AUCUNE terminaison TLS
    → réseau privé Hetzner (10.20.0.0/16) → node (IP PRIVÉE)
    → Caddy sur le node : termine réellement TLS #2 (certificat Let's Encrypt,
      ACME HTTP-01) → reverse_proxy en clair vers api:8000 (réseau Docker interne)
  ```
  **Réglage Cloudflare requis : SSL/TLS mode = `Full` ou `Full (strict)` —
  JAMAIS `Flexible`.** `Flexible` enverrait du HTTP EN CLAIR de Cloudflare
  vers le LB/node (Caddy n'écoute qu'en HTTPS sur 443 ici) — la connexion
  échouerait purement et simplement. `Full (strict)` est recommandé : le
  certificat Let's Encrypt de Caddy est un vrai certificat validé par une CA
  publique, donc `strict` (qui exige un certificat d'origine valide, pas
  seulement présent) fonctionne sans compromis.
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
- **80/443/443-udp NE sont PAS ouverts publiquement sur les nodes**
  (2026-09-17, audit Cloudflare/LB — corrigé, ils l'étaient avant). Fait
  Hetzner documenté : un Cloud Firewall ne filtre QUE l'interface PUBLIQUE
  d'un server, jamais le réseau privé — le LB route vers ce node via SA
  SEULE IP PRIVÉE (`use_private_ip = true`), donc fermer ces ports au monde
  n'affecte EN RIEN le trafic LB → node, mais empêche structurellement de
  contourner Cloudflare + le LB en appelant directement l'IP publique du
  node. `infra/firewall/ufw.sh` (couche hôte) applique la même restriction
  via `PRIVATE_NET_CIDR` (voir cloud-init). Aucun impact sur ACME : Let's
  Encrypt valide via le VRAI enregistrement DNS public (donc via Cloudflare
  → LB → node en privé), jamais en visant l'IP du node directement.
- **443/udp (HTTP/3) retiré, pas seulement restreint.** Le Load Balancer
  Hetzner ne fait passer QUE du TCP (ses 2 services sont `protocol = "tcp"`,
  voir `load_balancer.tf`) — aucun trafic QUIC n'atteint jamais un node.
  Cloudflare négocie HTTP/3 avec le CLIENT uniquement ; sa propre connexion
  vers l'origine (le LB) reste HTTP(S) standard. Un `443/udp` public ne
  servait donc à rien d'autre qu'à élargir la surface de bypass ci-dessus.
- **IPv4 publique du node conservée, délibérément.** Un Hetzner Cloud
  Server sans IP publique n'a AUCUN accès sortant par défaut (pas de
  passerelle NAT managée côté Hetzner pour les servers privés-seuls) — donc
  `apt-get`, les `docker pull` depuis GHCR, et les appels sortants de
  l'appli (Groq/Bedrock, WhatsApp Cloud API, Paydunya, Sentry, Langfuse,
  Grafana Cloud) en ont besoin de toute façon. Retirer l'IP publique
  exigerait une passerelle NAT dédiée (un server de plus, complexité inutile
  au stade MVP). Le firewall (ci-dessus) est le bon niveau pour fermer ce
  qui doit l'être, pas l'absence d'IP publique.
- **`admin_cidrs` est vide par défaut et Terraform refuse `0.0.0.0/0`**
  (validation dans `variables.tf`) — vous DEVEZ renseigner explicitement vos
  IP/CIDR admin, jamais d'ouverture SSH universelle par défaut.
- **cloud-init ne clone plus le dépôt** (corrigé 2026-09-17, commit
  `7dd474f` — avant cela il clonait puis exécutait `infra/firewall/ufw.sh`
  tel quel ; ce paragraphe était resté périmé jusqu'à cet audit). Le
  bootstrap applique désormais un pare-feu hôte **minimal et autonome**
  directement en `runcmd` (SSH + 80/443 réseau privé uniquement — voir
  `cloud-init/app-node.yaml.tpl`), sans dépendre du dépôt GitHub. Le
  checkout réel et tout le reste du déploiement applicatif restent pris en
  charge ensuite par GitHub Actions/`scripts/node_deploy.sh`.
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

*(Section mise à jour 2026-09-19, audit scale-out — la version précédente,
écrite avant que ce module soit réellement appliqué, affirmait à tort que
`terraform` n'était pas disponible et que rien n'avait pu être testé.)*

- `terraform fmt`/`validate` **exécutés et passants** sur l'ensemble du
  module (v1.16.2).
- `terraform plan` **réellement exécuté**, à plusieurs reprises, contre le
  **vrai state de production** (`terraform.tfstate` local — `ladini-app-1`,
  server id `166245083`) :
  - `-refresh=false` (jeton syntaxiquement valide mais non réel — voir
    `scripts/test_scale_out_plan.sh` pour le pourquoi ; ce module n'a
    aucune `data "hcloud_*"`, donc aucun appel réseau n'est nécessaire pour
    ce mode) : reproduit à l'identique le bug rapporté (`5 to add, 1 to
    change, 2 to destroy` à `app_node_count = 2`, et confirmé que le MÊME
    remplacement de `ladini-app-1` apparaissait déjà à `app_node_count = 1`
    inchangé — la preuve empirique de la cause racine `user_data`
    ci-dessus), puis validé le correctif (`0 to destroy` à 1→2 et à 1→3).
  - **Non exécuté** : un `terraform plan` avec **refresh réel** (jeton
    Hetzner valide + accès réseau à l'API) — aucun accès identifiants/réseau
    disponible pendant cet audit. C'est la limite honnête de cette
    validation : un refresh réel pourrait révéler un drift EXTERNE
    (modification faite dans la console Hetzner, hors Terraform) que
    `-refresh=false` ne peut pas voir. **Avant le premier `apply` qui suit
    ce chantier**, lancez `./scripts/test_scale_out_plan.sh --refresh` (ou
    un simple `terraform plan` normal) avec un jeton réel pour la
    confirmation finale.
- Aucun `terraform apply` n'a été exécuté (ni avant, ni pendant cet audit) —
  conformément à la consigne, aucune infrastructure réelle n'a été modifiée.
- Tous les noms de ressources/arguments nouvellement introduits ou modifiés
  par ce chantier (`for_each` sur `hcloud_server`/`hcloud_load_balancer_target`,
  bloc `moved`, `lifecycle.ignore_changes`) sont standards Terraform (pas
  spécifiques au provider `hcloud`) et ont été exercés par les `plan` réels
  ci-dessus, pas seulement lus dans la documentation.

## Observabilité — quand envisager un scale-out (manuel, pas automatique)

Pas d'autoscaling aujourd'hui (délibéré — voir plus haut, "scaling manuel
contrôlé mais rapide"). Signaux à surveiller (Prometheus/Grafana déjà en
place, voir `infra/grafana/dashboards/03-infrastructure.json`) pour décider
**manuellement** d'augmenter `app_node_count` :

| Composant | Signal | Seuil indicatif |
|---|---|---|
| API (FastAPI/Uvicorn) | CPU | > 75 % durablement (plusieurs minutes, pas un pic) |
| API | RAM | > 80 % |
| API | Latence p95/p99 | en hausse soutenue par rapport à la baseline |
| Worker Celery | Longueur de queue | croissante sur la durée (pas un pic ponctuel) |
| Worker Celery | CPU | élevé et soutenu |
| Worker Celery | Latence des tâches | en hausse |
| PostgreSQL | Saturation du pool de connexions (PgBouncer) | proche de la limite configurée |
| PostgreSQL | Latence des requêtes | en hausse |
| Redis | Latence / mémoire / connexions | en hausse par rapport à la baseline |

Un ou plusieurs de ces signaux SOUTENUS (pas un pic isolé) → augmentez
`app_node_count` (voir § SCALE OUT ci-dessus). Aucun de ces seuils ne
déclenche quoi que ce soit automatiquement — c'est un guide de décision
humaine, pas un contrôleur.
