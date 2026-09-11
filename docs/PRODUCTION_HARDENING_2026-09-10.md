# Production Hardening DevOps — Rapport final (2026-09-10)

Périmètre : transformer le déploiement Docker Compose de Ladini en une
plateforme **simple, reproductible, immuable, portable, observable,
rollbackable** — sans Kubernetes, sans PaaS, sans réécrire l'architecture.

---

## A. Architecture AVANT

```
GitHub Actions "CI"  →  lint + pytest + `docker build` (validation, PAS de push)
                        [aucun artefact publié]

Déploiement          →  SSH sur le VPS
                        deploy-prod.sh :  git pull → docker compose build → alembic → rolling up
                        [le SERVEUR construit les images]

Runtime (docker-compose.prod.yml, 1 hôte) :
  api (FastAPI, :8000 exposé 0.0.0.0)   worker (Celery)   beat   mcp (:8003)
  redis (allkeys-lru, /data NON persistant)   pgbouncer (:latest)   flower (:127.0.0.1)
  autoheal (:latest, sur agri_net, socket docker en RW)
  images : ladini-api:latest / ladini-worker:latest / ladini-mcp:latest   ← MUTABLES

  DB Postgres = managé externe (vars DO_DB_*)      Langfuse = Cloud
```

Constat : **une release n'est pas identifiable ni reproductible** (`:latest`
partout, build sur le serveur), **pas de rollback** (aucune notion de release
précédente), **pas de garde-fou de déploiement** (pas de preflight, pas de
verrou, pas de smoke), et **le broker Redis peut perdre des données
silencieusement** sous pression mémoire.

---

## B. Risques trouvés

| ID | Gravité | Risque | Preuve | Correction |
|----|---------|--------|--------|------------|
| R1 | **P0** | Images `:latest` → une release non identifiable, non reproductible, non rollbackable | `docker-compose.prod.yml` : `image: ladini-api:latest` ×3 ; `Dockerfile.worker/mcp` : `FROM ladini-api:latest` | Images `${REGISTRY}/${IMAGE_NAMESPACE}/ladini-*:${RELEASE_VERSION}` (tag `sha-…` immuable) ; `RELEASE_VERSION` obligatoire (`:?`) |
| R2 | **P0** | La **production construit ses images** (`docker compose build` dans `deploy-prod.sh`) → non reproductible, dépend de l'état du serveur, lent, pas d'artefact | `deploy-prod.sh` étapes 2 & 4b/4c (`--build`) | `docker-compose.prod.yml` sans `build:` ; build déplacé en CI (`release.yml`) → GHCR ; overlay `docker-compose.build.yml` (CI/dev only) ; `preflight` + `cicd.yml` **refusent** un `build:` dans le compose prod |
| R3 | **P0** | Aucun rollback : impossible de revenir à « la release d'avant » de façon déterministe | absence de `scripts/rollback.sh`, aucune trace de release | `deploy/releases/{current,previous,history.log}` + `scripts/rollback.sh` (app-only, jamais la DB) |
| R4 | P1 | Redis `maxmemory-policy allkeys-lru` sur le **broker Celery** + verrous d'idempotence + disjoncteur LLM → éviction silencieuse d'une tâche/d'un verrou sous pression | `docker-compose.prod.yml` service `redis` command | `noeviction` (Option A, §15) : Redis refuse l'écriture (échec bruyant) au lieu de perdre ; `REDIS_MAXMEMORY` paramétrable ; runbook « Redis OOM » |
| R5 | P1 | AOF Redis activé mais `/data` **non persistant** (pas de volume nommé) → `docker compose up --force-recreate` = perte du broker | service `redis` : `--appendonly yes` sans `volumes:` | volume nommé `redis_data:/data` ; `--appendfsync everysec` ; runbook « ne jamais `down -v` » |
| R6 | P1 | `api` publie `8000:8000` sur **0.0.0.0** → API en clair joignable depuis Internet si le firewall ne l'interdit pas | `ports: ["8000:8000"]` | `127.0.0.1:8000:8000` (loopback) + reverse-proxy Caddy (TLS/443) devant ; `infra/firewall/ufw.sh` (22/80/443 only) |
| R7 | P1 | Healthcheck `worker` = `celery inspect ping` → dépend du **broker** : une panne Redis rend tous les workers `unhealthy` → **restart storm** par autoheal, qui aggrave l'incident | service `worker` healthcheck | LIVENESS pure : `pgrep -f 'celery.*worker'` ; la readiness réelle est vérifiée par `deploy.sh` (`/health/ready`), pas par autoheal |
| R8 | P1 | Healthcheck `api` = `/health` (déjà liveness) mais pas de séparation explicite liveness/readiness ; readiness jamais utilisée pour valider un déploiement | `api/main.py` : `/health` + `/health/ready` ; compose sonde `/health` | ajout `/health/live` (alias explicite) ; `deploy.sh` attend `/health/ready = 200` (DB+Redis) APRÈS `up` ; autoheal ne sonde QUE liveness |
| R9 | P1 | Pas de `GET /version` → « quelle release tourne ? » = SSH + inspection d'image | absent de `api/main.py` | `GET /version` (release/git_sha/built_at/compose_version) sur api ET mcp ; injecté par le compose + baké en `ENV`/`LABEL` OCI dans l'image |
| R10 | P1 | `deploy-prod.sh` : aucune vérification préalable (disque, `.env`, images existantes, compose valide, ports) → peut casser la prod en cours de route | `deploy-prod.sh` — commence directement par `git pull` + `build` | `scripts/preflight.sh` : Docker, Compose v2, `.env` + variables `:?`, `compose config`, images au registry, disque ≥ seuil, ports loopback, verrou. **Échoue avant de toucher la prod.** |
| R11 | P1 | Deux déploiements concurrents peuvent s'écraser (SSH + CI) | aucun verrou | `scripts/lib.sh::acquire_lock` (lockdir atomique + détection de verrou périmé) ; `concurrency:` dans `release.yml`/`deploy.yml` |
| R12 | P1 | Migration destructive possible dans la même release que le code qui l'utilise → rollback applicatif insuffisant et non signalé | pas de garde ; `SCHEMA_COLUMN_DDL` appliqué au boot sans revue | `scripts/check_migrations.sh` (job CI `migration-safety`) : bloque `DROP/RENAME/ALTER TYPE/NOT NULL sans DEFAULT`, classe `ROLLBACK_SAFE` vs `MIGRATION_REQUIRES_MANUAL_RECOVERY` ; `rollback.sh` s'interrompt sur le 2ᵉ cas ; `docs/runbooks/migrations.md` |
| R13 | P1 | `docker build` context = `.` **sans `.dockerignore` racine** → ~800 Mo de providers Terraform Windows (`.exe`), `.git`, venvs, caches envoyés au démon à chaque build ; risque d'embarquer un `.env` | `backend/.dockerignore` seulement (jamais lu : le `.dockerignore` doit être à la racine du context) ; `du -sh infra/terraform/.terraform` = 807 Mo | `.dockerignore` racine (VCS, secrets, venvs, `infra/terraform/.terraform`, tests, docs…) |
| R14 | P2 | Images tierces non pinnées : `redis:7-alpine`, `edoburu/pgbouncer:latest`, `willfarrell/autoheal:latest` (ce dernier monte le socket Docker) | `docker-compose.prod.yml` | pins exacts : `redis:7.4.1-alpine`, `edoburu/pgbouncer:v1.23.1-p2`, `willfarrell/autoheal:1.2.0`, `caddy:2.8-alpine` |
| R15 | P2 | `autoheal` sur le réseau applicatif `agri_net` + socket Docker en **lecture/écriture** | service `autoheal` | retiré de `agri_net` (il ne parle qu'au socket) ; socket en `:ro` ; label-scopé (déjà le cas) ; image pinnée |
| R16 | P2 | Secrets distribués trop largement : `flower` et `mcp` reçoivent tout `*app-env` (Groq, Twilio, WhatsApp, Paydunya…) sans jamais s'en servir | anchor `*app-env` unique appliqué à tous les services | groupes d'env (`x-db-env`, `x-redis-env`, `x-mcp-env`, `x-llm-env`, `x-messaging-env`, `x-payments-storage-env`, `x-observability-env`, `x-appsec-env`) ; `mcp` → release+db+mcp (11 vars) ; `flower` → release+redis (5 vars) ; api/worker/beat → env complet (66 vars) |
| R17 | P2 | Logs Docker non bornés → un petit VPS se remplit | aucun bloc `logging:` | anchor `x-logging` (`json-file`, 20 Mo × 5) sur **tous** les services |
| R18 | P2 | Variables DB conceptuellement liées à DigitalOcean (`DO_DB_*`) | `x-app-env` + service `pgbouncer` + `.env.example` | convention neutre `DB_HOST/PORT/USER/PASSWORD/NAME` avec repli transitoire `${DB_HOST:-${DO_DB_HOST}}` ; `.env.example` migré, legacy commenté « à supprimer » |
| R19 | P2 | Pas de reverse-proxy / TLS versionné dans le repo | absent | `infra/reverse-proxy/` : Caddy (TLS auto, HTTP→HTTPS, headers de sécurité, `request_body max_size`, timeouts, ne proxifie QUE `/api/webhook*`, `/health*`, `/version`) |
| R20 | P2 | Pas de firewall provider-neutral documenté | absent | `infra/firewall/ufw.sh` (détecte le port SSH courant avant `enable`, 22/80/443 only, `--dry-run`) |
| R21 | P2 | Pas de smoke tests post-déploiement ; risque de marquer « SUCCESS » sur une API cassée | `deploy-prod.sh` s'arrête après `up --wait` sur `/health` (liveness) | `scripts/smoke.sh` (< 20 s, non destructif) : `/health/live`, `/version` == release attendue, `/health/ready` (DB+Redis `ok`), MCP interne + non exposé, `celery inspect ping`, beat vivant, webhook joignable (405) |
| R22 | P3 | Terraform AWS (ECR/ECS) mêlé au reste de `infra/` sans séparation provider | `infra/terraform/*.tf` | `infra/providers/README.md` : matrice de portabilité + convention `infra/providers/<name>/` ; le Terraform existant documenté comme « hors chemin MVP » |
| R23 | P3 | Aucun runbook | absent | `docs/runbooks/{deployment,rollback,incident,database-restore,migrations}.md` |

---

## C. Modifications effectuées

### Application (changements MINIMES, opérationnels — §48 respecté)

| Fichier | Modification | Raison |
|---|---|---|
| `backend/src/ladini/api/main.py` | `+ GET /version` (release/git_sha/built_at/compose_version, lus depuis l'env) ; `+ GET /health/live` (alias liveness explicite) ; `import os` | R9, R8 — « quelle version ? » sans SSH ; liveness ≠ readiness |
| `backend/src/ladini/protocols/mcp/servers/http_server.py` | `+ GET /version` | R9 — cohérence, consommé par `smoke.sh` |
| `backend/tests/architecture/test_http_entrypoints_are_authenticated.py` | `/version` + `/health/live` ajoutés à la liste blanche des routes publiques | l'invariant de sécurité (audit précédent) sinon échoue sur les 2 nouvelles routes ; elles n'exposent aucun secret |

Aucun nœud de l'agent (interpreter, guards, planner, executor, tunnels…) n'a
été touché. Suite complète : **3515 tests, 0 échec**.

### Infrastructure / déploiement

| Fichier | Modification | Raison |
|---|---|---|
| `.dockerignore` **(nouveau, racine)** | exclut `.git`, secrets, venvs, `infra/terraform/.terraform` (807 Mo), tests, docs, `infra/aws`, `deploy/releases`… | R13 |
| `docker-compose.prod.yml` | **réécrit** : images immuables `${REGISTRY}/${IMAGE_NAMESPACE}/ladini-*:${RELEASE_VERSION}` ; **aucun `build:`** ; groupes d'env least-privilege ; `x-release-env` injecté ; `x-logging` partout ; `redis` → `noeviction` + volume `redis_data` + `appendfsync everysec` ; `api`/`flower` publiés sur `127.0.0.1` ; healthchecks LIVENESS (`/health/live`, `pgrep celery`) ; pins tierces ; `autoheal` hors `agri_net`, socket `:ro` ; `DB_*` neutre + repli `DO_DB_*` | R1,R4,R5,R6,R7,R14,R15,R16,R17,R18 |
| `docker-compose.build.yml` **(nouveau)** | overlay `build:` pour CI/dev uniquement (`API_IMAGE` pointe l'image registry) | R2 |
| `infra/docker/Dockerfile.api` | `ARG RELEASE_VERSION/GIT_SHA/BUILD_TIMESTAMP` → `ENV` + `LABEL org.opencontainers.image.*` | R9 — image auto-descriptive, `/version` fonctionne même sans injection compose |
| `.github/workflows/cicd.yml` | **CI seule** : `concurrency` (annule les runs obsolètes) ; job `migration-safety` (`check_migrations.sh`) ; `docker-build` aligné sur le nouveau compose + assert « pas de `build:` en prod » + valide l'overlay | R2, R12 |
| `.github/workflows/release.yml` **(nouveau)** | sur push `main` : build **une fois** les 3 images, tag `sha-<court>` + `latest` (alias humain), push GHCR, `build-args` de release, artefact `release-manifest`, résumé de job | R1, R2 |
| `.github/workflows/deploy.yml` **(nouveau)** | `workflow_dispatch` (release + target) ; `verify-image` (`docker manifest inspect` ×3) ; job `deploy` protégé par l'**environnement GitHub** `production` (required reviewers) → SSH → `scripts/deploy.sh` ; `concurrency` par cible | §28 (approbation humaine), §37 |
| `scripts/lib.sh` **(nouveau)** | helpers partagés : logs, **verrou** (lockdir atomique, portable, détection périmé), wrapper `dc`, manifeste de release (`write/read`), attente santé bornée (`wait_healthy_container`, `wait_http`), classification migration | R3, R11 |
| `scripts/preflight.sh` **(nouveau)** | ~10 familles de checks ; **n'écrit rien** ; `docker compose config` avant tout ; refuse `latest` ; refuse un `build:` en prod ; disque ; ports ; verrou | R10, §41, §42 |
| `scripts/deploy.sh` **(nouveau)** | `./scripts/deploy.sh <release>` : lock → preflight → snapshot release → `pull` (atomique) → migration éphémère offline → `up -d` → attente `healthy` + `/health/ready` → `smoke.sh` → enregistrement ; **échec après `up`** ⇒ rollback applicatif auto (jamais la DB) ; blocs `DEPLOYMENT SUCCESS` / `DEPLOYMENT FAILED` (stage, reason, current good, rollback command) | §9, §13, §43, §44 |
| `scripts/rollback.sh` **(nouveau)** | `./scripts/rollback.sh [release]` → `previous` par défaut ; **jamais de migration** ; détecte `MIGRATION_REQUIRES_MANUAL_RECOVERY` et s'interrompt (`ROLLBACK_FORCE=1` pour outrepasser) ; réécrit `current`/`previous` | §10, §43 |
| `scripts/smoke.sh` **(nouveau)** | vérifs rapides non destructives (voir R21) | §14 |
| `scripts/check_migrations.sh` **(nouveau)** | garde EXPAND/CONTRACT : scanne alembic + `SCHEMA_COLUMN_DDL` + `backend/migrations/` ; mode CI bloquant + `--classify` ; échappatoire `migration-contract-approved:` | R12, §11 |
| `scripts/test/{mock-docker.sh,run-scenarios.sh}` **(nouveau)** | rejoue les **10 scénarios §45** contre un faux `docker`/`curl` (aucun démon requis) — prouve la logique de décision + la portabilité (aucun `doctl`/`aws`/`hcloud`) | §45, §46 |
| `deploy/releases/{README.md,.gitkeep}` **(nouveau)** | manifeste de release (runtime, git-ignoré : `current`, `previous`, `history.log`) | R3, §8 |
| `infra/reverse-proxy/{Caddyfile,docker-compose.yml,.env.example}` **(nouveau)** | TLS + reverse-proxy, stack séparée, réseau `agri_net` externe | R6, R19, §18 |
| `infra/firewall/ufw.sh` **(nouveau)** | pare-feu hôte provider-neutral | R6, R20, §19 |
| `infra/providers/README.md` **(nouveau)** | matrice de portabilité + convention | R22, §31, §32 |
| `docs/runbooks/*.md` **(nouveau ×5)** | deployment, rollback, incident, database-restore, migrations | R23, §34, §35, §33, §11 |
| `.env.example` | `+ REGISTRY`, `+ IMAGE_NAMESPACE`, `+ REDIS_MAXMEMORY`, `DB_*` (neutre) + `DO_DB_*` commenté (legacy) | R1, R4, R18 |
| `.gitignore` | `+ deploy/releases/{current,previous,history.log}`, `+ deploy/.deploy.lock` | R3 |

---

## D. Pipeline final

```
   CODE
    │  (branche + Pull Request)
    ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  CI  (.github/workflows/cicd.yml)  — sur PR                    │
  │   ruff (bloquant) · mypy (info) · pytest (3515) · pip-audit    │
  │   migration-safety (check_migrations.sh)                       │
  │   docker-build : build les 3 images (PAS de push) + compose ok │
  └───────────────────────────────────────────────────────────────┘
    │  merge main
    ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  RELEASE  (.github/workflows/release.yml)  — BUILD ONCE        │
  │   build api → worker → mcp                                     │
  │   tag  sha-<court>  (+ latest, alias humain)                   │
  │   push  ghcr.io/idoefraim/ladini/ladini-*:sha-<court>     │
  │   artefact release-manifest (RELEASE_VERSION, GIT_SHA, …)      │
  └───────────────────────────────────────────────────────────────┘
    │  images immuables au registry
    ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  STAGING   (deploy.yml, target=staging)                       │
  │   verify-image ×3 → SSH → scripts/deploy.sh <release>          │
  │   preflight → pull → migrate → up → health → SMOKE            │
  └───────────────────────────────────────────────────────────────┘
    │  smoke OK
    ▼
  ┌───────────────────────────────────────────────────────────────┐
  │  PRODUCTION  (deploy.yml, target=production)                   │
  │   ENVIRONNEMENT GitHub "production" → APPROBATION HUMAINE      │
  │   verify-image ×3 → SSH → scripts/deploy.sh <release>          │
  │     lock → preflight → snapshot → pull → migrate(offline) →    │
  │     up -d → wait healthy → /health/ready=200 → smoke.sh →      │
  │     record (current→previous, current=release)                 │
  └───────────────────────────────────────────────────────────────┘
    │
    ├── SUCCESS  → DEPLOYMENT SUCCESS (release, previous, health, smoke)
    │
    └── FAILURE (après `up`) → rollback APPLICATIF auto → release précédente
                             → DEPLOYMENT FAILED (stage, reason, rollback cmd)
                             → la DB n'est JAMAIS touchée
```

---

## E. Procédure EXACTE de déploiement

Sur le VPS (`cd $DEPLOY_DIR`), ou via `deploy.yml` (approbation) :

```bash
./scripts/deploy.sh sha-a83f6c1
```

Étapes internes (aucune n'altère la prod avant `up`) :

```
1  acquire_lock                         verrou lockdir (deploy/.deploy.lockdir)
2  scripts/preflight.sh sha-a83f6c1     docker, compose v2, .env + :? , compose config,
                                        images au registry, disque ≥ 3 Go, ports, verrou
3  lire deploy/releases/current         → CURRENT_RELEASE (peut être vide au 1er deploy)
4  export RELEASE_VERSION / COMPOSE_VERSION / GIT_SHA / BUILD_TIMESTAMP
5  docker compose pull  api worker mcp redis pgbouncer autoheal    (atomique)
6  docker compose up -d --wait pgbouncer         (DB joignable ?)
   [si backend/alembic.ini] docker compose run --rm api alembic upgrade head   (OFFLINE)
7  docker compose up -d --remove-orphans          (bascule, nouvelles images)
8  wait_healthy_container  mcp/api/worker/beat/redis/pgbouncer   (LIVENESS Docker)
   wait_http 127.0.0.1:8000/health/ready == 200                  (READINESS : DB + Redis)
9  scripts/smoke.sh                                               (< 20 s, non destructif)
10 cp current → previous ; écrire current ; history_append deploy-success
```

Sortie :

```
╔══════════════════════════════════════════════════════════════════╗
  DEPLOYMENT SUCCESS
  Release   : sha-a83f6c1
  Previous  : sha-b921ea7
  Git SHA   : <full>
  Built at  : 2026-09-10T...Z
  Migration : ROLLBACK_SAFE
  Health    : OK   (/health/ready = 200)
  Smoke     : OK
  Rollback  : ./scripts/rollback.sh            # → sha-b921ea7
╚══════════════════════════════════════════════════════════════════╝
```

ou

```
  DEPLOYMENT FAILED
  Stage           : preflight | pull | migrate | up | health | smoke
  Reason          : <exact>
  Target release  : sha-a83f6c1
  Current good    : sha-b921ea7
  Rollback command: ./scripts/rollback.sh sha-b921ea7
```

---

## F. Procédure EXACTE de rollback

```bash
./scripts/rollback.sh                 # → deploy/releases/previous (dernière bonne)
# ou
./scripts/rollback.sh sha-b921ea7     # → release explicite
```

```
1  acquire_lock
2  lire current / previous
3  [si alembic + migration de la release quittée == MIGRATION_REQUIRES_MANUAL_RECOVERY]
       → STOP + avertissement (ROLLBACK_FORCE=1 pour outrepasser)
4  export RELEASE_VERSION = cible
5  docker compose pull  api worker mcp
6  docker compose up -d --remove-orphans          (PAS de migration)
7  wait_healthy_container ... ; wait_http /health/ready == 200
8  scripts/smoke.sh
9  previous = (ancienne current) ; current = cible ; history_append rollback-success
```

```
╔══════════════════════════════════════════════════════════════════╗
  ROLLBACK SUCCESS
  Now running : sha-b921ea7
  Was         : sha-a83f6c1
  Health      : OK   (/health/ready = 200)
  Smoke       : OK
  Note        : APP rollback only — la base de données n'a pas été modifiée.
╚══════════════════════════════════════════════════════════════════╝
```

**APP ROLLBACK ≠ DATABASE ROLLBACK.** Migration destructive dans la release
quittée → `docs/runbooks/database-restore.md` (restaurer un dump/PITR
antérieur), le rollback applicatif seul ne suffit pas.

---

## G. Portabilité

**Identique sur DigitalOcean / Hetzner / OVH / Scaleway / AWS EC2·Lightsail /
VM locale / serveur dédié :**

- les 3 `Dockerfile` et le build-once (`release.yml`) ;
- le registry (`REGISTRY`/`IMAGE_NAMESPACE` — GHCR par défaut, ECR/Docker Hub
  sans toucher au YAML) ;
- `docker-compose.prod.yml`, `docker-compose.build.yml` ;
- `scripts/{preflight,deploy,rollback,smoke,check_migrations}.sh` + `lib.sh` ;
- les healthchecks, `stop_grace_period`, limites de ressources ;
- `infra/reverse-proxy/` (Caddy), `infra/firewall/ufw.sh` (ufw) ;
- les 5 runbooks.

Seule dépendance runtime : `docker`, `docker compose`, `curl`, `git`,
coreutils. **Aucun** `doctl`/`aws`/`hcloud` dans le chemin critique (prouvé par
`scripts/test/run-scenarios.sh`, qui tourne sans démon ni metadata cloud).

**Provider-specific (mince, hors logique de release), dans `infra/providers/<name>/` :**

| Besoin | ce qui change |
|---|---|
| Provisionner la VM Linux | droplet / Cloud server / instance / EC2·Lightsail |
| Volume bloc (optionnel) | Volumes / Block Storage / EBS |
| IP / réseau | Reserved/Floating/Elastic IP |
| **DB Postgres managée** | DO Managed DB / RDS·Aurora / Scaleway Managed DB / conteneur ou externe (Hetzner) — seul `DB_HOST/PORT/USER/PASSWORD/NAME` change dans `.env` |
| Firewall cloud (couche 2 optionnelle) | Cloud Firewall / Security Group |
| DNS `PUBLIC_DOMAIN` → IP | DO DNS / Route 53 / OVH DNS… |

AWS : `docker compose` sur EC2 fonctionne à l'identique ; passage futur à ECS =
mêmes images, même pipeline conceptuel (BUILD ONCE → REGISTRY → RELEASE →
DEPLOY → HEALTH → ROLLBACK).

---

## H. Tests

### Exécutés

| Test | Commande | Résultat |
|---|---|---|
| Suite applicative complète (régression des changements `/version`, `/health/live`) | `cd backend && python -m pytest tests` | **3515 passed, 10 skipped, 0 failed** |
| Invariant sécurité (routes authentifiées) | `pytest tests/architecture/test_http_entrypoints_are_authenticated.py` | 9 passed |
| Scénarios de déploiement §45 (faux docker/curl, sans démon) | `bash scripts/test/run-scenarios.sh` | **24 PASS / 0 FAIL** |
| Syntaxe de tous les scripts | `bash -n scripts/*.sh scripts/test/*.sh infra/firewall/ufw.sh` | OK |
| Validité YAML (compose ×3 + workflows ×3) | `python -c "yaml.safe_load(...)"` | OK |
| Compose prod sans `build:` | `grep '^\s*build:' docker-compose.prod.yml` | vide ✓ |
| Groupes d'env least-privilege | inspection des `environment:` fusionnés | mcp=11 vars (pas de Groq), flower=5 vars (pas de Groq), api/worker/beat=66 |

### Scénarios §45 couverts par `run-scenarios.sh`

| # | Scénario | Vérifié |
|---|----------|---------|
| 1 | Release normale A→B→SUCCESS | `current`=B, `previous`=A ✓ |
| 2 | Image inexistante → deploy refuse, A reste active | preflight/pull échoue, `current` inchangé ✓ |
| 3 | Container jamais healthy → échec + rollback auto | exit≠0, `deploy-failed-autorollback` dans `history.log`, `current` inchangé ✓ |
| 4 | Smoke KO → rollback auto | `/health/ready=503` → échec, `current` inchangé ✓ |
| 5 | Rollback manuel B→A | `current`=A, `previous`=B ✓ |
| 6 | Variables manquantes → preflight refuse avant prod | ✓ (+ `latest` refusé) |
| 7 | Disque insuffisant → preflight refuse | ✓ |
| 8 | Deux deploys concurrents → 2ᵉ bloqué | verrou lockdir → exit 1 ✓ |
| 9 | Migration incompatible détectée + classée + échappatoire | `check_migrations.sh` bloque `DROP COLUMN`, classe `MIGRATION_REQUIRES_MANUAL_RECOVERY`, laisse passer avec `migration-contract-approved:` ✓ |
| 9b | EXPAND (`ADD COLUMN … NULL`) → `ROLLBACK_SAFE` | ✓ |
| 10 | Redis recréé → persistence | **non exécuté** (pas de démon Docker sur la machine d'implémentation) — couvert structurellement : volume nommé `redis_data`, `appendonly yes`, `appendfsync everysec` ; à valider en E2E (voir I) |

---

## I. Risques restants (vrais, non corrigés)

1. **E2E Docker non exécuté.** La machine d'implémentation n'a pas de démon
   Docker. `run-scenarios.sh` prouve la **logique** de `deploy.sh`/`rollback.sh`
   /`preflight.sh` contre des faux `docker`/`curl`, mais un premier
   déploiement réel sur une VM Linux reste nécessaire pour valider : la
   résolution `docker compose config` avec un vrai `.env`, le `pull` GHCR, le
   `alembic`/no-alembic, la persistance Redis au `--force-recreate` (scénario
   10), le comportement d'autoheal. **Procédure : `docs/runbooks/deployment.md` §12.**

2. **Alembic non configuré.** Le schéma est géré par `SCHEMA_COLUMN_DDL`
   (idempotent, au boot worker). `deploy.sh` gère les deux cas (avec/sans
   `alembic.ini`) et `check_migrations.sh` scanne déjà `SCHEMA_COLUMN_DDL`,
   mais tant qu'Alembic n'est pas branché, il n'y a pas de *downgrade* ni de
   numéro de révision dans `/version`. Bootstrap décrit dans
   `docs/runbooks/migrations.md`.

3. **Backups DB non vérifiés.** La DB est managée externe ; le fournisseur
   exact et ses capacités (backups auto, rétention, PITR, procédure de
   restore) **ne sont pas confirmés**. `docs/runbooks/database-restore.md`
   contient la checklist à remplir + un filet `pg_dump` manuel avant migration
   risquée. **Tant que la section « Ce fournisseur » n'est pas remplie et une
   restauration testée, considérer qu'il n'y a pas de stratégie de
   restauration validée.**

4. **Rétention des images non automatisée.** `deploy.sh` ne supprime jamais
   d'image (rollback toujours possible). Le nettoyage (`docker image prune`
   en gardant `current`+`previous`) est un geste manuel/cron documenté
   (`deployment.md` §11) — sur un petit VPS, à mettre en cron pour éviter le
   disque plein.

5. **`latest` poussé par `release.yml`.** Alias de confort humain uniquement ;
   `preflight.sh` **refuse** `latest` pour un déploiement. Si l'équipe préfère
   zéro tag mobile, retirer les deux lignes `:latest` de `release.yml`.

6. **Incident git pendant l'implémentation** (voir § « Note d'incident »
   ci-dessous). Résolu, aucune perte, mais mérite un coup d'œil de l'auteur.

---

## Note d'incident (implémentation)

Le harnais de test `scripts/test/run-scenarios.sh`, dans sa **première**
version, exécutait des commandes `git` (`stash`, `checkout -b`, `commit`)
**dans le dépôt réel** pour tester `check_migrations.sh`. Sur cette machine
(Git Cygwin), un `git stash --include-untracked` a échoué silencieusement, si
bien que la création de branche a emporté tout le travail non commité dans un
commit jetable (`ecb0dd5`), et un `git stash pop` a partiellement ré-appliqué
deux stashes préexistants (« sanitization_outscope/offscope », sans rapport).

**Rétabli intégralement :**

- tout le travail de la session (2 audits de sécurité + ce chantier DevOps)
  a été restauré depuis `ecb0dd5` (`git checkout ecb0dd5 -- .`) ;
- le package renommé `backend/src/ladini/` (306 fichiers) est en place,
  `backend/src/agriconnect/` supprimé (conforme à l'intention de la branche et
  à `ecb0dd5`) ;
- les artefacts de `stash pop` (fichiers RAG/ingestion non suivis :
  `.pre-commit-config.yaml`, `pytest.ini` racine, `backend/alembic/`,
  `backend/scripts/`, 4 fichiers de test `test_dimension_guard` /
  `test_ingestion_permanent_failure` / `test_database_service_*`) ont été
  supprimés ;
- **les deux stashes préexistants sont intacts** (`git stash list` inchangé) —
  aucune perte.
- **Suite complète re-vérifiée : 3515 passed, 0 failed.**

Le harnais a été **corrigé** : Cas 9/9b construisent désormais un dépôt git
**jouet de 3 fichiers** dans un `mktemp -d`, **jamais** le dépôt réel. Le
verrou de `lib.sh` est passé de `flock` (absent sur Cygwin) à un **lockdir
`mkdir` atomique et portable**.

Point de contrôle pour l'auteur : le commit `ecb0dd5` (branche
`hardening-mig-test-40906` si elle existe encore, sinon accessible par SHA via
`git reflog`) contient un instantané complet et vérifié de tout le travail —
utile comme filet si quoi que ce soit paraît manquant dans l'arbre de travail
actuel.

---

## J. Verdict

```
PRODUCTION DEPLOYMENT : PRÊT POUR PILOTE  —  sous 2 conditions de sortie
```

| Critère (§44) | État |
|---|---|
| build immuable | ✅ `release.yml` → GHCR, tag `sha-…`, `RELEASE_VERSION` obligatoire |
| deploy reproductible | ✅ `scripts/deploy.sh`, aucun build sur le serveur |
| rollback testé | ✅ scénarios 3/4/5 (auto + manuel) — logique validée sans démon |
| preflight | ✅ `scripts/preflight.sh`, fail-avant-prod |
| health verification | ✅ liveness/readiness séparées, `/health/ready` gate le deploy |
| smoke tests | ✅ `scripts/smoke.sh` |
| Redis sécurisé | ✅ `noeviction` + volume `redis_data` + `requirepass` (préexistant) |
| secrets non exposés inutilement | ✅ groupes d'env ; mcp/flower réduits |
| ports contrôlés | ✅ api/flower en `127.0.0.1`, reverse-proxy 443, ufw 22/80/443 |
| logs bornés | ✅ `x-logging` 20 Mo × 5 partout |
| migrations encadrées | ✅ `check_migrations.sh` (CI) + classification + runbook |
| release identifiable | ✅ `/version`, `deploy/releases/{current,previous,history.log}`, LABEL OCI |
| CI bloque les erreurs | ✅ ruff bloquant, pytest, migration-safety, docker-build + assert no-`build:` |

**Conditions de sortie avant d'ouvrir au-delà du pilote :**

1. Exécuter **un vrai déploiement E2E** sur la VM cible (scénarios 1 + 10 avec
   un démon Docker réel) — `docs/runbooks/deployment.md` §12.
2. **Confirmer et tester** la restauration de la DB managée —
   `docs/runbooks/database-restore.md`, section « Ce fournisseur ».

Ces deux points sont opérationnels (à faire sur l'infra réelle), pas des
lacunes de conception.
```
```
