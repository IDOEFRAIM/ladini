# Runbook — Incident Ladini

Pour chaque cas : **symptôme → diagnostic → action sûre → quand rollback**.

Contexte : `cd "$DEPLOY_DIR"`. Raccourci : `dc(){ docker compose -f docker-compose.prod.yml "$@"; }`.
Postgres et Langfuse sont **externes** (managé / Cloud) — un incident sur eux
se traite chez le fournisseur, pas ici.

---

## API down

**Symptôme** : le webhook WhatsApp ne répond plus ; `curl 127.0.0.1:8000/health/live` échoue.

```bash
dc ps api                       # State / Health
dc logs --tail=200 api
docker inspect --format '{{.State.OOMKilled}} {{.RestartCount}}' "$(dc ps -q api)"
```

- `OOMKilled=true` → la limite mémoire est trop basse **ou** fuite. Voir *worker
  runaway*. Augmenter `deploy.resources.limits.memory` de `api` dans le compose,
  `dc up -d --no-deps api`.
- `/health/live` OK mais `/health/ready` = 503 → DB ou Redis. Voir sections dédiées.
- Crash au démarrage dans les logs → **release cassée** : `./scripts/rollback.sh`.

**Quand rollback** : crash-loop au démarrage, ou régression fonctionnelle
confirmée introduite par la dernière release.

---

## Worker down / n'exécute plus les tâches

**Symptôme** : les messages sont acceptés (webhook 200) mais aucune réponse
n'est envoyée ; Flower montre une file qui monte.

```bash
dc ps worker beat
dc exec worker celery -A ladini.api.celery_app inspect ping
dc exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning llen celery
dc logs --tail=200 worker | grep -iE 'error|traceback|oom'
```

- `inspect ping` KO mais conteneur `healthy` (le healthcheck ne sonde que
  `pgrep celery`, volontairement — voir compose) → le worker est vivant mais ne
  consomme plus : **broker injoignable** (voir *Redis*) ou deadlock. `dc restart worker`.
- `max-memory-per-child` recycle en boucle (logs « exceeded … restarting ») →
  fuite mémoire d'une tâche. Identifier la tâche dans Flower, corriger, release.
- `beat` down → les crons (réconciliation PROCUREMENT/PREORDER/SALES,
  sollicitations) ne tournent plus. `dc up -d beat`. Les drafts bloqués seront
  rattrapés au prochain passage.

**Quand rollback** : la dernière release a introduit une tâche qui plante
systématiquement (les tâches sont `acks_late` → elles sont **redélivrées en
boucle** et saturent le worker). Rollback puis corriger.

---

## Redis unavailable / Redis OOM

**Symptôme** : `/health/ready` = 503 avec `"redis": "error: …"` ; le worker
n'exécute plus rien ; erreurs `OOM command not allowed` dans les logs.

```bash
dc ps redis
dc exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping
dc exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning info memory | grep -E 'used_memory_human|maxmemory'
dc exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning info persistence | grep -E 'aof_|loading'
```

- **`maxmemory-policy` = `noeviction`** (voulu) : sous pression, Redis **refuse
  les écritures** au lieu d'évincer des tâches/verrous. C'est un **échec
  bruyant volontaire**. Actions :
  1. Purger ce qui est reconstructible : `redis-cli -a … --scan --pattern 'searchcache:*' | xargs redis-cli -a … del`
     (⚠️ **ne jamais** `FLUSHALL` / `FLUSHDB` : ça vide aussi le broker).
  2. Augmenter `REDIS_MAXMEMORY` dans `.env` (ex. `768mb`) + la limite mémoire du
     conteneur `redis` dans le compose, `dc up -d --no-deps redis`.
  3. Investiguer la cause : file `celery` anormalement longue ? verrous
     d'idempotence jamais expirés ?
- Conteneur down : `dc up -d redis`. L'AOF (`volume redis_data`) est rejoué au
  démarrage → le broker survit à un redémarrage. `aof_last_bgrewrite_status:ok`
  attendu.

**Quand rollback** : jamais pour Redis seul — c'est de l'infra, pas une release.
Sauf si la dernière release a introduit un usage Redis pathologique (nouveau
cache non borné, verrou sans TTL).

---

## DB unavailable

**Symptôme** : `/health/ready` = 503 avec `"database": "error: …"` ;
`pgbouncer` `unhealthy`.

```bash
dc ps pgbouncer
dc logs --tail=100 pgbouncer
dc exec pgbouncer psql "host=127.0.0.1 port=6432 user=$DB_USER dbname=pgbouncer" -c 'SHOW POOLS;' 2>&1 | head
# tester la DB managée directement (hors pooler) :
dc exec pgbouncer sh -lc 'nc -zv "$DB_HOST" "${DB_PORT:-25060}"'
```

- `nc` KO → problème **côté fournisseur managé** (maintenance, quota, IP
  bloquée). Consulter le dashboard du provider. Rien à corriger ici.
- `nc` OK mais PgBouncer KO → mauvais identifiants (`DB_USER/PASSWORD/NAME` dans
  `.env`) ou `SERVER_TLS_SSLMODE`. Corriger `.env`, `dc up -d --no-deps pgbouncer`.
- `SHOW POOLS` : `sv_active` plafonné à `DEFAULT_POOL_SIZE` en permanence +
  `cl_waiting` > 0 → saturation. Augmenter `PGBOUNCER_DEFAULT_POOL_SIZE`
  (≤ `max_connections` du plan − marge admin).

**Quand rollback** : si la dernière release a fait exploser le nombre de
requêtes/connexions (N+1, pool mal configuré). Sinon, traiter côté infra.

---

## MCP unavailable

**Symptôme** : les actions de l'agent échouent avec des erreurs outil ;
`dc ps mcp` `unhealthy`.

```bash
dc ps mcp
dc exec mcp curl -fsS http://localhost:8003/health
dc logs --tail=200 mcp | grep -iE 'critical|error|auth'
```

- Log `MCP HTTP daemon NON AUTHENTIFIÉ` → `MCP_HTTP_AUTH_TOKEN` absent de
  `.env`. **Le déploiement n'aurait pas dû passer** (`:?` dans le compose) —
  corriger `.env`, redéployer.
- `/health` interne KO mais conteneur up → pool DB non amorcé (voir *DB
  unavailable* : MCP dépend de `pgbouncer`). `dc restart mcp` une fois la DB OK.
- **Ne jamais** `--scale mcp=N` ni `--workers>1` : singleton process-local.

**Quand rollback** : rarement — MCP ne porte pas de logique métier propre. Sauf
si la dernière release a changé le contrat MCP.

---

## Disk full

**Symptôme** : `deploy.sh` refuse en preflight (`espace disque`), ou
`no space left on device` dans les logs, ou conteneurs qui ne démarrent plus.

```bash
df -h /
docker system df
```

Action sûre (préserve le rollback) :

```bash
CUR=$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/current)
PREV=$(sed -n 's/^RELEASE_VERSION=//p' deploy/releases/previous)
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep ladini- \
 | grep -vE ":($CUR|$PREV|latest)\b" | awk '{print $2}' | xargs -r docker rmi
docker image prune -f
docker builder prune -f
# logs déjà bornés (20m×5) ; si besoin : truncate -s 0 $(docker inspect --format='{{.LogPath}}' <cid>)
```

**Jamais** `docker system prune -a --volumes` (détruit `redis_data`).

---

## Release broken (constaté après coup)

**Symptôme** : le déploiement a « réussi » (health + smoke OK) mais un parcours
métier est cassé en prod.

```bash
cat deploy/releases/current           # confirmer la release fautive
./scripts/rollback.sh                 # → previous
```

Puis : reproduire en local/staging, corriger, PR, CI, **nouvelle** release.

---

## Migration failed

**Cas A — `deploy.sh` s'arrête à `Stage: migrate`** : la migration a échoué
**avant** la bascule. La prod tourne toujours sur l'ancien code. Corriger la
migration, republier, redéployer. Aucun rollback nécessaire.

**Cas B — une release passée a appliqué une migration destructive** et un
rollback est demandé : `rollback.sh` **s'interrompt** avec
`MIGRATION_REQUIRES_MANUAL_RECOVERY`. Le rollback applicatif seul **ne
restaurera pas** le schéma.
→ `docs/runbooks/database-restore.md` (restaurer un dump/PITR **antérieur** à la
migration destructive), puis redéployer l'ancienne release.

Ce cas doit être **rare** : `scripts/check_migrations.sh` (CI) bloque une
contraction dans la même release que le code qui l'utilise. S'il est arrivé,
c'est que la contraction a été explicitement approuvée
(`migration-contract-approved:`) alors que du code l'utilisait encore →
post-mortem sur le process de revue.

---

## LLM provider down

**Symptôme** : réponses lentes puis génériques ; logs `circuit OPEN` ; alerte
`ADMIN_ALERT_WEBHOOK_URL`.

```bash
curl -s -H "X-Admin-Token: $ADMIN_API_TOKEN" http://127.0.0.1:8000/admin/llm/health | jq .
```

- Le **LLM Gateway** bascule seul sur les candidats de repli (`LLM_FAST_FALLBACK_*`
  / `LLM_REASONING_FALLBACK_*`) ; le disjoncteur partagé (Redis) rouvre après
  `LLM_CIRCUIT_COOLDOWN_SECONDS`.
- Si **tous** les candidats d'un profil sont down (`status: DOWN`) : ajouter/activer
  un candidat de repli dans `.env` (`groq:…`), `dc up -d --no-deps api worker`.
- **Aucun rollback** : c'est externe. Rien à redéployer.

---

## WhatsApp provider down (Meta / Twilio)

**Symptôme** : les réponses ne partent plus ; logs `whatsapp` / `twilio` en 5xx
ou timeout à l'envoi.

- Vérifier le statut du fournisseur (Meta Business status / Twilio status page).
- Les réponses sont émises via l'**Outbox** (worker) → elles sont **rejouées**
  quand le fournisseur revient (rien n'est perdu tant que le worker tourne).
- Bascule de secours : `MESSAGING_PROVIDER=twilio` (ou l'inverse) dans `.env` +
  `dc up -d --no-deps api worker beat`, si l'autre canal est configuré.
- **Aucun rollback** applicatif.
