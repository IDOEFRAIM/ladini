# Runbook — Déploiement Ladini

> Objectif : à 2 h du matin, un opérateur suit ce document et retrouve un état
> connu fonctionnel en quelques commandes. **Pas de `git pull` + rebuild sur le
> serveur.** On déploie une **release immuable** déjà construite par la CI.

Toutes les commandes se lancent depuis `${DEPLOY_DIR}` (le checkout du dépôt sur
le VPS), en tant que l'utilisateur de déploiement.

---

## 0. Modèle mental

```
CODE → PR → CI (tests) → merge main → release.yml : BUILD ONCE → GHCR
                                                                    │
                          opérateur : deploy.yml (approbation) ─────┤
                                                                    ▼
   VPS :  scripts/deploy.sh <release>
          preflight → pull → migration → up -d → health → smoke → record
                                        │
                       échec après `up` │→ rollback APPLICATIF auto → release précédente
```

Une release = un tag **immuable** : `sha-a83f6c1` (SHA de commit court) ou
`release-2026.09.10-1`. **Jamais `latest`** (preflight le refuse).

---

## 1. Déployer

### 1.a — depuis GitHub (recommandé, avec approbation)

1. GitHub → **Actions** → *Deploy (manual approval)* → **Run workflow**.
2. `release` = le tag `sha-…` (visible dans le résumé du run *Release* correspondant).
   `target` = `production` (ou `staging`).
3. Un reviewer approuve l'environnement `production`. Le job SSH lance
   `scripts/deploy.sh` sur le VPS.

### 1.b — directement sur le VPS

```bash
cd "$DEPLOY_DIR"
./scripts/deploy.sh sha-a83f6c1
```

Sortie attendue :

```
╔══════════════════════════════════════════════════════════════════╗
  DEPLOYMENT SUCCESS
  Release   : sha-a83f6c1
  Previous  : sha-b921ea7
  Health    : OK   (/health/ready = 200)
  Smoke     : OK
  Rollback  : ./scripts/rollback.sh            # → sha-b921ea7
╚══════════════════════════════════════════════════════════════════╝
```

Ou, en cas d'échec :

```
  DEPLOYMENT FAILED
  Stage           : health | smoke | pull | preflight | migrate
  Reason          : <exact>
  Current good    : sha-b921ea7
  Rollback command: ./scripts/rollback.sh sha-b921ea7
```

`deploy.sh` fait **avant toute modification** : verrou (`flock`/lockdir),
`preflight.sh` (Docker, `.env`, images au registry, disque, ports, `compose
config`). Un échec ici ⇒ **rien n'a bougé**.

---

## 2. Quelle version tourne ?

```bash
cat deploy/releases/current            # RELEASE_VERSION, GIT_SHA, DEPLOYED_AT, DEPLOYED_BY
curl -s http://127.0.0.1:8000/version  # doit renvoyer le même RELEASE_VERSION
docker compose -f docker-compose.prod.yml ps
```

`deploy/releases/history.log` : une ligne par déploiement/rollback.

---

## 3. Vérifier que tout va bien

```bash
./scripts/smoke.sh                                  # < 20 s, non destructif
curl -s http://127.0.0.1:8000/health/ready | jq .   # {"database":"ok","redis":"ok"}
docker compose -f docker-compose.prod.yml ps        # tous "healthy"
```

---

## 4. Rollback

Voir `docs/runbooks/rollback.md`. En résumé :

```bash
./scripts/rollback.sh              # → deploy/releases/previous (dernière bonne)
./scripts/rollback.sh sha-XXXX     # → release explicite
```

⚠️ **Rollback APPLICATIF uniquement.** Ne restaure jamais la base de données.

---

## 5. Voir les logs

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=200 api
docker compose -f docker-compose.prod.yml logs --since=15m worker
docker compose -f docker-compose.prod.yml logs mcp | grep -i error
```

Rotation : 20 Mo × 5 fichiers par conteneur (driver `json-file`, voir compose).

---

## 6. Redémarrer un seul service

```bash
docker compose -f docker-compose.prod.yml restart worker
# recréation (rare — normalement seul deploy.sh recrée) :
RELEASE_VERSION="$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)" \
  docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate worker
```

---

## 7. Vérifier Redis

```bash
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli -a "$REDIS_PASSWORD" --no-auth-warning info | grep -E 'used_memory_human|maxmemory_policy|loading|aof_'
# doit montrer  maxmemory_policy:noeviction   et   aof_enabled:1
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli -a "$REDIS_PASSWORD" --no-auth-warning llen celery   # profondeur de file broker
```

Le volume `redis_data` porte l'AOF (durable). `docker compose down` **sans**
`-v` le préserve. **Ne jamais** `docker compose down -v` en production.

---

## 8. Vérifier PgBouncer

```bash
docker compose -f docker-compose.prod.yml exec pgbouncer \
  psql "host=127.0.0.1 port=6432 user=$DB_USER dbname=pgbouncer" -c 'SHOW POOLS;'
# cl_active / sv_active : si sv_active plafonne à DEFAULT_POOL_SIZE en permanence,
# augmenter PGBOUNCER_DEFAULT_POOL_SIZE (≤ max_connections du plan managé − marge).
```

---

## 9. Vérifier Celery

```bash
docker compose -f docker-compose.prod.yml exec worker \
  celery -A ladini.api.celery_app inspect ping
docker compose -f docker-compose.prod.yml exec worker \
  celery -A ladini.api.celery_app inspect active     # tâches en cours
# Flower (tunnel SSH) :
ssh -L 5555:127.0.0.1:5555 user@vps   # puis http://127.0.0.1:5555
```

---

## 10. Que faire si la migration échoue

`deploy.sh` applique les migrations **dans un conteneur éphémère, AVANT** la
bascule. Si `alembic upgrade head` échoue :

- la prod **tourne toujours sur l'ancien code** (aucun conteneur applicatif
  n'a été recréé) ;
- le script sort en `Stage: migrate`, la release cible n'est **pas** enregistrée ;
- corriger la migration, republier une release, redéployer.

Si une migration **destructive** a déjà été appliquée par une release
précédente (le guard CI `scripts/check_migrations.sh` doit l'empêcher — voir
`docs/runbooks/incident.md` → *migration failed*) : le rollback applicatif ne
suffit pas. Suivre `docs/runbooks/database-restore.md`.

---

## 11. Nettoyage disque (à faire hors incident)

`deploy.sh` **ne supprime jamais** d'image. Pour récupérer de l'espace **sans
casser le rollback** (on garde `current` + `previous`) :

```bash
CUR=$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)
PREV=$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/previous)
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
 | grep ladini- | grep -vE ":($CUR|$PREV|latest)\b" | awk '{print $2}' | xargs -r docker rmi
docker image prune -f      # layers dangling
```

---

## 12. Première installation sur un nouveau VPS

```bash
# 1. Docker + Compose v2 (méthode officielle, provider-neutral)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" ; newgrp docker

# 2. Pare-feu (voir infra/firewall/ufw.sh — 22/80/443 seulement)
sudo bash infra/firewall/ufw.sh

# 3. Checkout + .env
git clone https://github.com/IDOEFRAIM/AgriConnect "$DEPLOY_DIR" && cd "$DEPLOY_DIR"
cp .env.example .env && "${EDITOR:-vi}" .env      # remplir TOUS les [REQUIS]

# 4. Login registry (pour pull)
echo "$GHCR_PAT" | docker login ghcr.io -u <user> --password-stdin

# 5. Reverse proxy TLS (voir infra/reverse-proxy/)
cd infra/reverse-proxy && docker compose up -d && cd -

# 6. Premier déploiement
./scripts/deploy.sh <release>
```
