# Grafana Alloy — collecteur d'observabilité par host

Stack séparée (`docker-compose.alloy.yml`), même pattern que
`infra/reverse-proxy/` : cycle de vie indépendant du compose applicatif.
Un `alloy` + un `docker-socket-proxy` tournent **par host** (pas un
singleton cluster-wide) — chaque node Hetzner du chantier scale-out a son
propre Alloy, qui scrape son propre `api` local et pousse vers le même
compte Grafana Cloud.

## Démarrage

```bash
cd infra/alloy
cp .env.example .env && $EDITOR .env   # identifiants Grafana Cloud
docker compose -f docker-compose.alloy.yml up -d
```

Prérequis : le réseau `agri_net` doit déjà exister (créé par
`docker-compose.prod.yml` du node applicatif) — Alloy le rejoint en
`external: true`.

## Récupérer les identifiants Grafana Cloud

Chemin recommandé (le plus fiable — génère les noms/valeurs exacts, plutôt
que de les reconstruire à la main) :

1. Grafana Cloud → **Connections → Add new connection**.
2. Chercher **"Alloy"** (l'intégration officielle Grafana Alloy) — elle
   génère un `config.alloy` de référence avec le remote_write Prometheus,
   le `loki.write`, et l'endpoint OTLP déjà remplis pour VOTRE stack.
   Recopier uniquement les URLs/user IDs qu'elle affiche dans `.env` (les
   noms de variable dans `.env.example` de ce dossier suivent la même
   convention que cette page d'onboarding).
3. Alternative si seuls Prometheus/Loki sont nécessaires : **Connections →
   Add new connection → "Hosted Prometheus metrics"** et **"Hosted Logs"**
   séparément — chaque page affiche l'URL du endpoint + le "User" (instance
   ID) à utiliser en Basic Auth, le mot de passe étant une clé API créée
   sous **My Account → Security → API Keys** (scope minimal :
   `metrics:write`, `logs:write`, `traces:write` — jamais `admin`).

## Least privilege — qui reçoit les identifiants Grafana Cloud

**Seul le conteneur `alloy`** détient `GRAFANA_CLOUD_*` /
`GRAFANA_CLOUD_API_KEY`. Ce n'est *pas* une simplification arbitraire :
l'application (`api`, `worker`, `mcp`, `flower` — tous dans
`docker-compose.prod.yml`) pousse ses traces OTLP vers Alloy **en local**,
sur le réseau Docker interne `agri_net`, **sans authentification** (voir
`OTEL_EXPORTER_OTLP_ENDPOINT` → `http://alloy:4318`). C'est Alloy, seul,
qui s'authentifie ensuite vers Grafana Cloud avec la clé API. Si un
conteneur applicatif était compromis, un attaquant n'obtiendrait donc
**aucun** identifiant Grafana Cloud — seulement la capacité d'envoyer de
fausses traces à l'Alloy local (déjà le cas aujourd'hui pour n'importe quel
service sur `agri_net`, rien de nouveau exposé).

**Ne jamais** ajouter `GRAFANA_CLOUD_*` à `x-full-app-env` dans
`docker-compose.prod.yml` — ce serait donner à 4 processus applicatifs
(dont 2 traitent des webhooks publics) un accès en écriture à l'observabilité
de toute la flotte, sans aucun bénéfice fonctionnel.

## Threat model — le socket proxy Docker

Alloy **ne monte jamais** `/var/run/docker.sock` directement. Il parle à
`docker-socket-proxy` (`tecnativa/docker-socket-proxy`), qui est le seul
conteneur à monter le socket réel (`:ro`), et qui ne relaie que les
endpoints **en lecture** nécessaires à cadvisor (stats CPU/RAM par
conteneur) et à `loki.source.docker` (flux de logs) : `CONTAINERS=1`,
`INFO=1`. Tout le reste est explicitement à `0` — en particulier `EXEC=0`,
`POST=0`, `BUILD=0`, `COMMIT=0`, `VOLUMES=0`, `NETWORKS=0`, `IMAGES=0`,
`SECRETS=0`.

Le proxy tourne sur un réseau Docker isolé (`alloy_internal`, `internal:
true`) partagé uniquement entre `alloy` et `docker-socket-proxy` — il
n'est **jamais** sur `agri_net` et n'a donc aucune route réseau vers
`redis`/`pgbouncer`/`mcp`/l'API applicative (même raisonnement que le
service `autoheal` dans `docker-compose.prod.yml`, qui reste volontairement
hors `agri_net`).

**Ce qu'Alloy PEUT faire même totalement compromis** (RCE dans le process
Alloy lui-même) :
- Lister les conteneurs et lire leurs métadonnées/stats (`docker inspect`,
  `docker stats` en lecture).
- Lire les flux de logs stdout/stderr de tous les conteneurs de l'host.
- Émettre des requêtes réseau sortantes vers Grafana Cloud avec la clé API
  configurée (donc : pousser de fausses métriques/logs/traces, ou faire
  échouer temporairement l'observabilité — pas un accès aux données
  applicatives elles-mêmes).

**Ce qu'Alloy NE PEUT PAS faire, même compromis**, grâce au proxy :
- Démarrer, arrêter, ou exécuter une commande dans un conteneur
  (`EXEC=0`, pas d'accès `docker exec`/`docker run`).
- Lire le contenu d'un volume monté par un autre conteneur (`VOLUMES=0`) —
  en particulier pas d'accès aux secrets injectés par variables
  d'environnement d'un autre conteneur, ni aux fichiers de configuration
  montés.
- Construire ou pousser une image (`BUILD=0`, `COMMIT=0`) — pas de vecteur
  pour remplacer une image en cours d'exécution.
- Toucher au réseau Docker (`NETWORKS=0`) — pas de pivot réseau via le
  démon Docker lui-même (le pivot direct sur `agri_net`, s'il existe,
  viendrait du fait qu'Alloy y est déjà légitimement pour scraper `api`,
  pas d'un abus du socket).
- Lire les secrets/configs Docker Swarm (`SECRETS=0`, `CONFIGS=0` —
  non pertinent ici, cette stack n'utilise pas Swarm, mais posé par
  défense en profondeur si ça changeait un jour).

Autrement dit : le pire cas realiste est "Alloy ment à Grafana Cloud" ou
"Alloy peut lire tous les logs de l'host", jamais "Alloy peut prendre le
contrôle d'un autre conteneur ou lire ses secrets".

## Composants intégrés plutôt que des sidecars séparés

`prometheus.exporter.unix` (host) et `prometheus.exporter.cadvisor`
(conteneurs) sont des **composants Alloy natifs** — pas de conteneur
`node-exporter` ni `cadvisor` séparé à opérer/patcher/exposer en plus.
Un seul binaire Alloy par host couvre : métriques host, métriques
conteneurs, logs conteneurs, et réception OTLP.

## Exporters tiers (queue depth, Redis, PgBouncer)

`infra/exporters/docker-compose.exporters.yml` (stack séparée, voir son
propre header) ajoute `celery-exporter`, `redis_exporter` et
`pgbouncer_exporter` — 3 gaps que `telemetry.py` ne peut pas combler
lui-même (ce sont des composants tiers, pas du code applicatif à
instrumenter). `config.alloy` les scrape déjà (cibles
`celery-exporter:9808`, `redis-exporter:9121`, `pgbouncer-exporter:9127`)
mais reste silencieusement inoffensif si cette stack n'est pas déployée sur
un host donné (échec de scrape loggé, pas d'impact sur les autres cibles).

## Gap connu : métriques `ladini_llm_*` du worker Celery

`ladini/api/main.py` n'expose `/metrics` que pour le service `api`. Le
service `worker` (process Celery pur, aucun serveur HTTP) **enregistre**
bien les métriques LLM (`record_generation()` tourne côté worker, pas côté
api — c'est le worker qui traite le graphe LangGraph et appelle le LLM
Gateway) mais **rien ne les expose sur le réseau** : le registre
`prometheus_client` du worker vit uniquement dans sa mémoire de process,
jamais scrapable tel quel.

**Corriger ce gap correctement nécessite** le mode *multiprocess* de
`prometheus_client` (`PROMETHEUS_MULTIPROC_DIR` + agrégation via un
`multiprocess.MultiProcessCollector`, potentiellement compliqué avec le
pool `prefork` de Celery — chaque enfant a son propre process, et
`--max-tasks-per-child` recycle régulièrement ces enfants) **ou** un
serveur HTTP de métriques dédié démarré une fois par `worker_process_init`
(risque de conflit de port entre les 4 enfants `prefork` du même
conteneur — nécessiterait un port par enfant, ou un agrégateur côté hôte).

**NON FAIT ici** — flagué comme travail futur plutôt que de faire semblant
avec une config qui scraperait un endpoint inexistant. `config.alloy` ne
scrape donc QUE `api:8000/metrics`. Le dashboard `04-llm.json`
(`infra/grafana/dashboards/`) reste construit sur les noms de métriques
réels de `telemetry.py`, mais restera vide en production tant que ce gap
n'est pas comblé — voir le panneau de texte en tête de ce dashboard.
