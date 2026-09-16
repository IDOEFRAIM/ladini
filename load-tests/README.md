# load-tests/ — Charge k6 sur le webhook inbound Ladini

> Objectif : vérifier que la couche webhook (FastAPI → idempotence Redis →
> `process_agent_task.delay(...)`) tient à 50 / 200 / 500 utilisateurs
> concurrents, et que Celery joue bien son rôle d'**amortisseur** de pic
> (le webhook enqueue et répond vite ; le LLM/l'envoi WhatsApp se font en
> asynchrone dans le worker — voir `backend/src/ladini/api/routes/
> twilio_webhook.py` et `whatsapp_webhook.py`, bloc "DÉLÉGATION À CELERY").

---

## ⚠️ Lire avant d'exécuter quoi que ce soit

Un message synthétique accepté par le webhook (signature valide) devient un
**VRAI** `process_agent_task` Celery : appel LLM réel (Groq facturé), et
potentiellement un vrai envoi WhatsApp/SMS via Twilio/Meta. Ce dossier
NE DOIT JAMAIS être pointé sur une stack utilisant des identifiants de
production (`GROQ_API_KEY`, `WHATSAPP_CLOUD_API_TOKEN`,
`WHATSAPP_APP_SECRET`, `TWILIO_AUTH_TOKEN` réels) sans savoir précisément ce
qu'on fait.

**Défaut sûr intégré aux scripts** : `SIGNING_SECRET` est vide par défaut.
Sans lui, `verify_twilio_signature`/`verify_whatsapp_cloud_signature`
(`backend/src/ladini/api/security.py`) sont **fail-closed** — chaque requête
est rejetée (403/503) **avant** l'enqueue Celery. Vous mesurez alors la
capacité HTTP/FastAPI/dépendances de la route sous charge (utile : reverse
proxy, overhead FastAPI, résolution DNS/connexions), **mais pas** le
pipeline complet idempotence → enqueue → worker → LLM → envoi.

Pour tester le pipeline complet : voir `load-tests/real-small-scale-test.md`
— un protocole séparé, volontairement minuscule (5 VUs × 1 itération), avec
avertissement de coût explicite. N'exécutez **jamais** `webhook-scenario.js`
ou `burst-scenario.js` avec `SIGNING_SECRET` renseigné sur une cible dont
vous n'êtes pas certain qu'elle utilise des identifiants sandbox.

---

## Installer k6

Ce dépôt n'embarque pas de binaire k6. Deux options :

```bash
# Windows (admin requis) :
choco install k6 -y
# ou winget :
winget install k6 --source winget

# macOS :
brew install k6

# Linux (Debian/Ubuntu) :
sudo gpg -k
sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install k6
```

Sans droits admin (ex : poste dev verrouillé) — binaire portable, aucune
installation système requise :

```bash
curl -sL -o k6.zip https://github.com/grafana/k6/releases/download/v0.54.0/k6-v0.54.0-<os>-<arch>.zip
unzip k6.zip
./k6-v0.54.0-<os>-<arch>/k6 version
```

(remplacer `<os>-<arch>` par ex. `windows-amd64`, `linux-amd64`,
`macos-amd64`/`macos-arm64` — voir https://github.com/grafana/k6/releases).

---

## Scripts

### `webhook-scenario.js` — charge constante à 50 / 200 / 500 VUs

```bash
TARGET_URL=http://127.0.0.1:8000 SCENARIO=50  k6 run load-tests/webhook-scenario.js
TARGET_URL=http://127.0.0.1:8000 SCENARIO=200 k6 run load-tests/webhook-scenario.js
TARGET_URL=http://127.0.0.1:8000 SCENARIO=500 k6 run load-tests/webhook-scenario.js
TARGET_URL=http://127.0.0.1:8000 SCENARIO=all DURATION=60s k6 run load-tests/webhook-scenario.js  # les 3 à la suite
```

| Variable | Défaut | Rôle |
|---|---|---|
| `TARGET_URL` | `http://127.0.0.1:8000` | Base URL de l'API — **jamais** une URL prod par défaut. |
| `PROVIDER` | `twilio` | `twilio` (form-encoded) ou `whatsapp_cloud` (JSON Graph API). |
| `SCENARIO` | `50` | `50` \| `200` \| `500` \| `all`. |
| `DURATION` | `30s` | Durée de charge constante par palier. |
| `SIGNING_SECRET` | *(vide)* | `TWILIO_AUTH_TOKEN` ou `WHATSAPP_APP_SECRET` — voir avertissement plus haut. |
| `TWILIO_PUBLIC_BASE_URL` | = `TARGET_URL` | URL publique utilisée pour signer (doit matcher ce que l'API valide, voir `security.py::_candidate_signed_urls`). |

### `burst-scenario.js` — rampe 10 → 50 → 200 → 500 VUs

```bash
TARGET_URL=http://127.0.0.1:8000 k6 run load-tests/burst-scenario.js
```

Rampe fixe (pas de `SCENARIO`/`DURATION`) : 10 → 50 → 200 → 500 VUs par
paliers de 20-40s, retour à 0 en fin de run. Vérifie que la latence HTTP du
webhook reste plate pendant la montée — si elle grimpe avec les VUs, le
webhook n'est plus purement "enqueue-and-return" (régression à investiguer).

### `real-small-scale-test.md`

Runbook (pas un script) pour un test à coût réel borné (5 VUs, 1 itération)
contre de vraies intégrations sandbox. Lire l'avertissement de coût avant
d'exécuter quoi que ce soit qui s'en inspire.

---

## Métriques à capturer — checklist

Pour chaque run, noter ces métriques et **où** les lire. `k6`, à lui seul,
ne voit que le webhook HTTP — tout ce qui se passe côté worker/DB/Redis/LLM
se lit ailleurs.

| Métrique | Où la lire |
|---|---|
| **HTTP p50/p95/p99** (webhook) | Résumé `k6` en fin de run : `http_req_duration` (et la métrique custom `ladini_webhook_duration_ms`, tags identiques). Aussi visible en continu côté serveur via le Prometheus histogram `ladini_http_request_duration_seconds{route="/api/webhook/..."}` (`backend/src/ladini/core/telemetry.py`) — Grafana Cloud si Alloy est branché (voir `docs/architecture/deployment-hetzner.md`), sinon `curl -s http://127.0.0.1:8000/metrics \| grep ladini_http_request_duration_seconds`. |
| **Nombre de 5xx** | `k6` : `http_req_failed` (attention : inclut aussi les 403/503 volontaires du fail-closed, voir les compteurs custom `ladini_webhook_accepted_total`/`ladini_webhook_rejected_total` pour distinguer). Côté serveur : Prometheus `ladini_http_5xx_total{route=...}`. |
| **Profondeur de la file Celery (queue depth)** | **PAS visible côté k6** — c'est justement ce qu'on veut voir absorber le pic. Flower : `http://127.0.0.1:5555` (tunnel SSH en prod, voir `docker-compose.prod.yml` commentaire service `flower`) → onglet "Broker" montre la taille des files (`interactive`, `background`, `scheduled`). Ou `celery -A ladini.api.celery_app inspect active_queues` / `redis-cli LLEN <queue>` sur le Redis broker. |
| **Temps d'attente en file (queue waiting time)** | Différence entre l'heure d'enqueue (`ladini_webhooks_received_total` timestamp) et le début d'exécution de la tâche — pas de métrique Prometheus dédiée aujourd'hui dans `telemetry.py`. À défaut : Flower montre le "received"→"started" par tâche pour un échantillon, ou activer `task_sent`/`task_prerun` côté Celery signals si un suivi précis est requis (hors scope de ce chantier). |
| **Temps d'exécution des tâches (task execution time)** | Flower (par tâche, colonne "runtime"), ou `celery -A ladini.api.celery_app events` en direct. Pas de métrique Prometheus dédiée dans `telemetry.py` aujourd'hui — `ladini_llm_duration_seconds` couvre le sous-segment "appel LLM" de la tâche, pas la tâche entière. |
| **Débit worker (worker throughput)** | Flower (tâches/min par queue), ou `docker compose -f docker-compose.prod.yml exec worker celery -A ladini.api.celery_app inspect stats` (compteurs cumulés par worker). |
| **CPU / RAM** | `docker stats` en direct pendant le run (aucune config requise). En continu : `cadvisor` (`docker-compose.monitoring.yml`, déjà dans ce repo) exporte vers Prometheus/Grafana si ce stack est démarré ; sinon Grafana Cloud via `node-exporter`/Alloy s'il est branché. |
| **Connexions DB (DB connections)** | Postgres managé : `SELECT count(*) FROM pg_stat_activity;` côté DB. Local : `docker compose exec pgbouncer psql ...` non applicable (pgbouncer n'a pas de shell psql) — utiliser la commande admin PgBouncer (`SHOW POOLS;` sur le port admin) ou le dashboard du provider managé. |
| **Attentes PgBouncer (PgBouncer waits)** | Commande admin PgBouncer `SHOW POOLS;` → colonnes `cl_waiting`/`sv_active`/`sv_idle`. Pas exposé en Prometheus par défaut dans ce repo — `pgbouncer_exporter` n'est pas installé (hors scope de ce chantier ; à ajouter si ce chiffre devient un vrai SLO). |
| **Latence / erreurs Redis** | `redis-cli --latency -h <host>` pendant le run. Erreurs applicatives : grep logs `TWILIO_WEBHOOK_REDIS_ERROR`/`WHATSAPP_WEBHOOK_REDIS_ERROR` (`routes/twilio_webhook.py`/`whatsapp_webhook.py`, fail-open documenté) — pas de compteur Prometheus dédié aujourd'hui. |
| **Concurrence LLM / 429** | Prometheus : `ladini_llm_calls_total{status="error"}` (tous statuts confondus — filtrer les logs pour isoler les 429 précisément, le status label ne distingue pas le code d'erreur), `ladini_llm_fallback_total` (bascules de repli, un proxy indirect de pression/429 sur le candidat primaire), `ladini_llm_circuit_open_total` (le disjoncteur LLM Gateway s'est ouvert — signal fort de saturation). Langfuse (si activé) donne le détail par Generation, y compris le message d'erreur exact. |

**Résumé pratique** : k6 seul suffit pour la couche HTTP (p50/p95/p99, 5xx,
latence webhook). Tout ce qui concerne le TRAITEMENT du message (queue,
worker, LLM, DB) doit être lu **en parallèle** du run k6, pas dans son
résumé — ouvrir Flower + `docker stats` + Grafana (si branché) avant de
lancer le script, jamais après coup.

---

## Exécuter contre la stack locale (avec prudence)

Si une stack `docker-compose.prod.yml` + `docker-compose.dev.yml` tourne déjà
en local (`docker compose -f docker-compose.prod.yml -f docker-compose.dev.yml
--profile app --profile scheduler --profile admin ps`), il est tentant d'y
pointer directement `TARGET_URL=http://127.0.0.1:8000`. **Vérifiez d'abord
`.env`** : si `GROQ_API_KEY`/`WHATSAPP_CLOUD_API_TOKEN`/`WHATSAPP_APP_SECRET`
ressemblent à de vraies clés (pas des valeurs `test`/`sandbox` explicites),
NE PASSEZ PAS `SIGNING_SECRET` — laissez le comportement fail-closed par
défaut absorber les requêtes de test sans jamais atteindre Celery. C'est
volontairement le comportement par défaut de ces deux scripts.
