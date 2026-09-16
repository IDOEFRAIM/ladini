# Runbook — Scaling (Hetzner, multi-node)

> Procédures opérationnelles pour ajouter/retirer de la capacité. Contexte
> architectural complet : `docs/architecture/deployment-hetzner.md`. Ce
> runbook suppose `infra/inventory.yml` déjà en place (copié depuis
> `infra/inventory.example.yml`) et validé (`python
> scripts/validate_inventory.py`).

---

## Ajouter un node app

### 1. Terraform — provisionner le nouveau server

```bash
cd infra/providers/hetzner
# éditer environments/production.tfvars : app_node_count = 2 (ou N+1)
terraform plan  -var-file=environments/production.tfvars -var="hcloud_token=$TF_VAR_hcloud_token"
```

**Lire le plan avant d'appliquer.** Si c'est le passage de 1 → 2 nodes app,
Terraform va **recréer** le(s) node(s) app existant(s) — `placement_group_id`
ne peut être posé qu'à la création d'un server côté API Hetzner
(`infra/providers/hetzner/placement.tf`, condition `app_node_count >= 2`).
Le plan l'affiche explicitement (destroy + create). À partir de 2 nodes,
tout passage à N+1 n'affecte plus les nodes existants (`count` ajoute sans
recréer).

```bash
terraform apply -var-file=environments/production.tfvars
```

`cloud-init` (`infra/providers/hetzner/cloud-init/app-node.yaml.tpl`) clone
le dépôt et prépare le node au premier boot — attendre que le server soit
`running` et joignable en SSH avant l'étape suivante.

### 2. Déclarer le rôle dans l'inventaire

```yaml
# infra/inventory.yml
  - name: node-b
    host: 10.20.1.11    # IP privée — sortie Terraform `app_nodes`, voir outputs.tf
    roles: [app]         # JAMAIS scheduler/admin ici — voir §"scheduler" plus bas
```

```bash
python scripts/validate_inventory.py infra/inventory.yml
```

### 3. Déployer la release courante sur le cluster mis à jour

```bash
./scripts/cluster_deploy.sh <release> infra/inventory.yml
```

`cluster_deploy.sh` pilote `node_deploy.sh <release> app --skip-migrate` sur
CHAQUE node de l'inventaire (SSH, dans l'ordre déclaré, scheduler en
dernier) — les migrations DB ne sont PAS refaites par node (déjà faites une
fois par l'orchestrateur).

### 4. Vérifier la santé

```bash
# Sur le nouveau node (ou via SSH) :
SMOKE_CHECK_SCHEDULER=0 ./scripts/smoke.sh        # ce node n'a pas beat
./scripts/smoke_observability.sh                   # /metrics + Alloy si présent
```

Le Load Balancer Hetzner ajoute automatiquement le nouveau node comme cible
(`hcloud_load_balancer_target`, `count = var.app_node_count`,
`infra/providers/hetzner/load_balancer.tf`) — aucune action manuelle de
registration LB. Vérifier que le node apparaît "healthy" côté LB (console
Hetzner ou `terraform output`, quelques secondes après que son healthcheck
HTTPS passe).

### 5. Confirmer visible côté observabilité

Si `infra/alloy/docker-compose.alloy.yml` est déployé sur le nouveau node
(`cd infra/alloy && docker compose -f docker-compose.alloy.yml up -d`) :
vérifier dans Grafana Cloud qu'une nouvelle série apparaît pour ce host
(label `instance`/`host` selon la config Alloy — voir `infra/alloy/`).

**⚠️ Recalculer `PGBOUNCER_DEFAULT_POOL_SIZE` avant/après cette procédure** —
voir `docker-compose.prod.yml` §"DIMENSIONNEMENT MULTI-NODE" :
`N_nodes × PGBOUNCER_DEFAULT_POOL_SIZE <= max_connections(plan managé) − marge_admin`.
Ajouter un node SANS baisser cette variable sur TOUS les nodes existants
peut saturer `max_connections` du Postgres managé.

---

## Ajouter de la capacité worker (sans ajouter de node)

Avant de provisionner un node entier, vérifier si la capacité Celery
existante est le vrai goulot — c'est un changement de variable
d'environnement, pas un nouveau server :

| Variable | Défaut | Effet |
|---|---|---|
| `CELERY_WORKER_CONCURRENCY` | `4` | Nombre de process prefork par conteneur `worker` — augmenter consomme plus de CPU/RAM sur CE node (`deploy.resources.limits` du service `worker`, `docker-compose.prod.yml`, actuellement `cpus: 3.0`/`memory: 2560m` — vérifier la marge avant de monter). |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | Nombre de tâches pré-récupérées par process avant traitement — laisser à `1` pour des tâches longues/coûteuses (LLM) ; augmenter favorise le débit sur des tâches courtes au prix d'une distribution moins équitable entre workers. |
| `CELERY_WORKER_MAX_TASKS_PER_CHILD` | `200` | Recyclage préventif d'un process worker après N tâches (fuite mémoire lente — voir `infra/docker/Dockerfile.worker` pour la justification d'origine). |
| `CELERY_WORKER_MAX_MEMORY_PER_CHILD` | `450000` (KB) | Recyclage si un process dépasse ce seuil mémoire. |
| `CELERY_WORKER_QUEUES` | `interactive,background,scheduled,celery` | Quelles files ce worker consomme — un déploiement peut dédier des workers à une file spécifique (ex: un pool `interactive` seul, pour ne jamais faire attendre un utilisateur derrière une tâche `background` longue). |

Procédure :
```bash
# 1. Ajuster la/les variable(s) dans .env (ou le secret manager du provider CI/CD)
# 2. Redéployer — AUCUN changement d'image, juste un redémarrage du service worker
#    avec la nouvelle config :
./scripts/node_deploy.sh <release_courante_inchangée> app   # 1 node
./scripts/cluster_deploy.sh <release_courante_inchangée>     # cluster
```

Observer l'effet : profondeur de file (Flower) et débit worker avant/après
— voir la checklist `load-tests/README.md` pour savoir précisément où lire
chaque métrique.

---

## Retirer un node

1. **Drainer** : retirer le node du Load Balancer AVANT de l'éteindre.
   Aujourd'hui, cela signifie faire échouer volontairement son healthcheck
   LB (ex: `docker compose --profile app -f docker-compose.prod.yml stop
   caddy` si le reverse-proxy est le composant sondé) plutôt qu'un arrêt
   sec — sinon des requêtes en vol vers ce node sont perdues.
2. **Attendre les tâches en vol** : `worker` a un `stop_grace_period: 90s`
   (`docker-compose.prod.yml`) — un `docker compose stop worker` sur ce
   node laisse le temps aux tâches déjà acquittées de finir (Celery "warm
   shutdown", `task_acks_late=True` redélivre à un autre worker toute tâche
   NON acquittée si le node meurt brutalement à la place).
3. **Éteindre les conteneurs applicatifs** sur ce node :
   ```bash
   docker compose --profile app -f docker-compose.prod.yml down
   ```
4. **Retirer de l'IaC** : baisser `app_node_count`
   (`infra/providers/hetzner/variables.tf`) et `terraform apply` — Terraform
   détruit le(s) server(s) d'index le(s) plus haut en premier (comportement
   standard de `count`). Si un node spécifique (pas le dernier index) doit
   partir, utiliser `terraform state rm`/`terraform import` ou
   `-target` avec prudence (hors scope de ce runbook — vérifier le plan).
5. **Retirer de `infra/inventory.yml`** et relancer
   `python scripts/validate_inventory.py`.

Recalculer `PGBOUNCER_DEFAULT_POOL_SIZE` dans l'autre sens (le budget par
node peut remonter) — même formule que ci-dessus.

---

## Le scheduler est EXACTEMENT 1 — jamais 0, jamais 2

`scripts/validate_inventory.py` refuse tout `infra/inventory.yml` où :
- aucun node ne porte `scheduler` → Celery Beat ne tourne nulle part, plus
  aucune tâche planifiée ne se déclenche (silencieux — aucune erreur
  visible tant que personne ne remarque l'absence d'un cron) ;
- **plusieurs** nodes portent `scheduler` → Beat n'est PAS conçu pour
  tourner en plusieurs exemplaires (pas de leader election dans ce projet,
  §9/§44 du chantier scale-out) : chaque tâche planifiée se déclencherait
  deux fois (double notification, double réconciliation de paiement...).

```bash
python scripts/validate_inventory.py infra/inventory.yml
```

à lancer **avant** tout `cluster_deploy.sh` — le script d'orchestration ne
le fait pas automatiquement à ce jour, c'est une étape manuelle du
checklist de déploiement.

Pour déplacer le rôle `scheduler` d'un node à un autre (ex: avant de
décommissionner le node qui le porte) : ajouter `scheduler` sur le node
cible, retirer `scheduler` de l'ancien, valider, puis
`cluster_deploy.sh` (le nouveau node scheduler démarre `beat`, l'ancien
l'arrête au prochain déploiement de son rôle réduit). Ne jamais avoir les
deux en même temps plus longtemps que nécessaire pour la bascule — un
répertoire `infra/inventory.yml` avec temporairement 2 `scheduler` doit
être corrigé avant le prochain `cluster_deploy.sh`, jamais déployé tel
quel (le garde-fou `validate_inventory.py` l'empêcherait de toute façon en
CI si câblé, ou doit être lancé manuellement sinon).

Alternative structurelle : `scheduler_on_dedicated_node = true`
(`infra/providers/hetzner/variables.tf`) crée un server Hetzner séparé,
hors `app_node_count`, hors Load Balancer (il ne sert jamais de trafic
HTTP) — utile si `beat`/`flower` doivent être isolés du blast radius d'un
node app (charge, incident).
