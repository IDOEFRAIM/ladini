# Migration Redis Upstash → Valkey auto-hébergé (AWS EC2) — 2026-09-20

## 0. Contexte et cause

Quota gratuit Upstash dépassé : 602k commandes / mois (plafond 500k),
68 099 writes, 534 085 reads. Objectif : éliminer la dépendance à un Redis
managé payant en migrant vers un **Valkey auto-hébergé sur une instance AWS
EC2** créée par l'opérateur. La connectivité Hetzner → AWS a été validée
manuellement avant cette migration (`valkey-cli -h <IP> -p 6379 -a '<secret>'
ping` → `PONG` depuis le serveur Hetzner) : réseau, Security Group et
authentification Valkey étaient déjà confirmés fonctionnels avant que le code
applicatif ne soit touché.

Aucun changement destructif n'a été fait sur l'infrastructure existante
(pas de `terraform apply`, pas de suppression d'Upstash) — cette migration ne
couvre QUE le code applicatif, la config, les tests et cette documentation.

## 1. Ce qui a changé (et ce qui n'a PAS changé)

**Changé** : rien dans le code applicatif lui-même. `redis-py`/Celery
détectent déjà le schéma (`redis://` vs `rediss://`) tout seuls — voir
[api/celery_app.py::_redis_ssl_options](../backend/src/ladini/api/celery_app.py).
La migration Upstash → Valkey est donc, par construction, un simple
changement de **valeur** de `REDIS_URL` (et éventuellement de schéma, si
Valkey n'exige pas TLS alors qu'Upstash l'exigeait) — jamais un changement de
code.

**Retiré** : les champs de config morts `VALKEY_ENDPOINT`/`VALKEY_AUTH_TOKEN`/
`VALKEY_USE_TLS` dans
[core/settings.py](../backend/src/ladini/core/settings.py) — ajoutés à un
moment antérieur sans jamais être consommés nulle part (aucune référence
dans tout `src/`), ils suggéraient à tort qu'un Redis externe et un Valkey
externe se configurent différemment. `REDIS_URL` reste l'unique source de
vérité, quel que soit le fournisseur réel derrière.

**Renforcé** : `scripts/preflight.sh` valide désormais explicitement que
`REDIS_URL` a un schéma reconnu (`redis://` ou `rediss://`, §4ter) et ne
contient plus de placeholder embarqué au milieu de l'URL (§4quater, ex.
`redis://:CHANGE_ME@valkey-host:6379/0` copié tel quel depuis `.env.example`
sans être rempli) ; les `.env.example` (racine, `backend/`, `infra/
exporters/`) montrent désormais un exemple `redis://` (sans TLS) plutôt que
`rediss://`, cohérent avec un Valkey joint en réseau privé.

## 2. Cartographie des usages Redis

| Usage | Fichier | Rôle | Sync/Async | Critique | Compatible Valkey |
|---|---|---|---|---|---|
| Idempotence webhooks (claim/cache/increment) | [core/idempotency.py](../backend/src/ladini/core/idempotency.py) | `SET NX EX`, `GET`, `SETEX`, `INCR`+`EXPIRE`, fail-open | Sync (`redis-py`) | Critique (dédup webhooks) | Oui — commandes standard |
| Broker Celery | [api/celery_app.py](../backend/src/ladini/api/celery_app.py) | file de tâches (interactive/background/scheduled/celery) | Sync (kombu) | Critique | Oui — protocole Redis standard, aucune extension |
| Result backend Celery | [api/celery_app.py](../backend/src/ladini/api/celery_app.py) | résultats de tâches, TTL 1h | Sync (celery.backends.redis) | Non-critique (résultats rarement consultés) | Oui |
| Circuit breaker / health registry LLM Gateway | [llm_gateway/health_registry.py](../backend/src/ladini/graphs/agents/market_coach/llm_gateway/health_registry.py) | verrous de probe `SET NX EX`, état de santé par candidat | Sync | Important (repli multi-provider) | Oui |
| Client Redis LLM Gateway | [llm_gateway/redis_client.py](../backend/src/ladini/graphs/agents/market_coach/llm_gateway/redis_client.py) | factory `get_redis()`, redaction avant log | Sync | — (infra transverse) | Oui |
| Cache résultats de recherche | [services/search_results_cache.py](../backend/src/ladini/services/search_results_cache.py) | `SETEX` par téléphone, TTL court | Sync | Non-critique (recalculable) | Oui |
| Cible photo en attente | [services/pending_photo_target.py](../backend/src/ladini/services/pending_photo_target.py) | `SETEX` par téléphone, TTL court | Sync | Non-critique | Oui |
| Tâche upload photo produit | [workers/media/product_photo_task.py](../backend/src/ladini/workers/media/product_photo_task.py) | consomme Redis via idempotency/cache ci-dessus | Sync | Critique (livraison message) | Oui |
| Webhooks Twilio/WhatsApp | [api/routes/twilio_webhook.py](../backend/src/ladini/api/routes/twilio_webhook.py), [whatsapp_webhook.py](../backend/src/ladini/api/routes/whatsapp_webhook.py) | dédup via idempotency | Sync | Critique | Oui |
| Healthcheck `/health/ready` | [api/main.py](../backend/src/ladini/api/main.py) | `PING`, 2s timeout | Sync | Critique (probe orchestrateur) | Oui |
| Exporter Prometheus | [infra/exporters/docker-compose.exporters.yml](../infra/exporters/docker-compose.exporters.yml) | `INFO` protocole (oliver006/redis_exporter) | — | Observabilité | Oui — basé sur `INFO`, protocole standard |
| Scripts de debug RAG legacy (`agriconnect`) | `scripts/check_ingestion_integrity.py`, `debug_redis_sync.py`, `redis_probe_verbose.py`, `run_full_ingestion.py`, `run_retriever_query.py`, `check_redis_vectors.py`, `test_redissearch_direct.py` | commandes RediSearch (`FT.*`) | Sync | **Mort/orphelin** — non référencé par aucun Dockerfile, package `agriconnect` legacy | **Non applicable** — scripts morts, hors périmètre du système déployé |

**Aucune commande RediSearch/module Redis Stack n'est utilisée par le
système réellement déployé** — seuls des scripts de debug orphelins de
l'ancien package `agriconnect` (jamais construits dans une image Docker) y
font référence, et pointent déjà vers un module Python qui n'existe plus sur
disque. Ce point était le risque de compatibilité Valkey le plus sérieux a
priori (Valkey vanilla ne supporte pas les modules Redis Stack) — confirmé
sans objet après audit.

`VECTOR_BACKEND` est `pgvector` par défaut (Postgres, pas Redis) et
`RAG_REDIS_FALLBACK_MODE` par défaut (`manual`) n'utilise que `HGET` + calcul
Python — les deux sont Valkey-compatibles par construction.

**Conclusion compatibilité** : Valkey est protocole-compatible à 100 % avec
tous les usages réels (aucune extension, aucun module, uniquement des
commandes Redis de base : `GET`/`SET`/`SETEX`/`INCR`/`EXPIRE`/`PING`/`INFO`).
Aucun changement de bibliothèque cliente (`redis-py`) n'a été nécessaire.

## 3. TLS — logique scheme-aware, sans hack Upstash-spécifique

`redis-py` et Celery lisent le schéma de `REDIS_URL` eux-mêmes :

- `redis://` → aucune option SSL n'est passée (Celery lève lui-même une
  erreur si des options SSL traînent sur un schéma non-TLS).
- `rediss://` → `ssl_cert_reqs` explicite (`REDIS_TLS_CERT_REQS`, défaut
  `required` = `CERT_REQUIRED`), calculé indépendamment pour le broker ET le
  backend (ils peuvent diverger).

Ce mécanisme existait déjà (`_redis_ssl_options`,
[api/celery_app.py](../backend/src/ladini/api/celery_app.py)), introduit
suite à un incident réel de production (sha-891cb2f, 2026-09-18) : Celery
`RedisBackend` lève un `ValueError` bloquant sur `rediss://` sans SSL
explicite, alors que kombu (broker) se contente d'un warning et d'un repli
silencieux sur `CERT_NONE`. **Aucun hack spécifique à Upstash n'existait et
aucun n'a été ajouté** — la logique est déjà 100 % pilotée par le schéma de
l'URL, donc immédiatement valide que le Redis en face soit Upstash, Valkey,
ou tout autre fournisseur.

Le Valkey AWS EC2 de cette migration n'exposant pas TLS en interne (réseau
privé/VPC), l'URL cible utilise `redis://` — mais `rediss://` reste
pleinement supporté si un futur Redis/Valkey managé public l'exige.

## 4. Sécurité

- **Aucun secret en dur dans ce dépôt.** `REDIS_URL` (avec mot de passe
  inclus) vit exclusivement dans `.env` (jamais commité) en local/staging, et
  dans le secret GitHub `LADINI_APP_ENV_B64` en production (voir §7).
- **Jamais loggé en clair.** [llm_gateway/redis_client.py::_redact](../backend/src/ladini/graphs/agents/market_coach/llm_gateway/redis_client.py)
  masque les credentials avant tout log applicatif ; Alloy applique en plus
  sa propre regex de redaction sur tout stdout de conteneur avant expédition
  vers Grafana Cloud Loki
  (`(?i)(rediss?|postgres(?:ql)?(?:\+\w+)?)://[^:/@\s]*:[^@\s]+@`, voir
  [infra/alloy/config.alloy](../infra/alloy/config.alloy)).
- **`/health/ready` ne fuite jamais l'URL ni le mot de passe** — seul
  `type(exc).__name__` apparaît en cas d'erreur Redis (`ConnectionError`,
  `AuthenticationError`, ...), jamais `str(exc)` qui pourrait contenir
  l'URL. Verrouillé par [backend/tests/unit/test_health_ready.py](../backend/tests/unit/test_health_ready.py)
  (nouveau, cette migration).
- **Security Group AWS : DOIT rester restreint aux IP publiques des nodes
  Hetzner sur le port 6379** — jamais `0.0.0.0/0`. Ce point n'a **pas** été
  modifié par cette migration (aucun `terraform apply`, aucun changement
  d'infrastructure AWS) : c'est une vérification à faire manuellement côté
  Security Group AWS avant de considérer la migration comme sûre en
  continu. **Recommandation pour plus tard, non imposée maintenant** : un
  tunnel WireGuard/VPN entre Hetzner et l'instance Valkey réduirait la
  surface d'exposition réseau au-delà du filtrage par IP source — pas fait
  dans cette migration (hors scope explicite), à évaluer séparément.
- `REDIS_PASSWORD` (variable dev-only, `docker-compose.dev.yml`) reste
  inchangé et n'est jamais lu en production.

## 5. Persistance et tradeoffs acceptés

Le Valkey AWS EC2 est configuré avec `requirepass` + `appendonly yes` +
`appendfsync everysec` :

- **Fenêtre de perte potentielle : jusqu'à ~1s** de commandes en cas de
  crash brutal (fsync toutes les secondes, pas à chaque écriture). Acceptable
  ici car **toutes** les données portées par Redis/Valkey dans ce système
  sont soit éphémères et recalculables (cache de recherche, cible photo en
  attente — TTL courts), soit re-émises par l'appelant en cas de perte
  (idempotence webhooks : au pire une commande dupliquée retraitée, pas une
  perte de données métier — les données métier vivent en Postgres, jamais
  dans Redis).
- **Broker Celery ≠ RabbitMQ** : Redis/Valkey comme broker n'a pas les
  garanties de livraison d'un vrai message broker (RabbitMQ). Tradeoff déjà
  accepté avant cette migration (architecture inchangée) — `task_acks_late=
  True` + `visibility_timeout=660` limitent le risque de perte de tâche en
  cas de crash worker (voir [api/celery_app.py](../backend/src/ladini/api/celery_app.py)).
  Introduire RabbitMQ est explicitement HORS SCOPE de cette migration (et de
  Ladini aujourd'hui).
- Classification complète des données par clé — voir §8.

## 6. Celery — confirmation de non-régression

Aucun changement de routage, de nom de file, de scheduler ou de comportement
worker n'a été introduit par cette migration :

- Files inchangées : `interactive` / `background` / `scheduled` / `celery`
  (défaut). Verrouillé par
  [backend/tests/unit/test_celery_queue_routing.py](../backend/tests/unit/test_celery_queue_routing.py)
  (nouveau, cette migration) — compare `TASK_ROUTES` à l'ensemble attendu et
  verrouille explicitly le routage des tâches critiques (`process_agent_task`
  → `interactive`, médias/paiements → `background`, crons → `scheduled`).
- `task_acks_late=True`, `broker_connection_retry=True`,
  `broker_connection_max_retries=2`, `visibility_timeout=660` — tous
  inchangés et re-vérifiés par le même fichier de test.
- Beat, Flower, worker : aucune modification de code. Le marqueur de
  démarrage réussi (`WORKER_READY_MARKER`, healthcheck worker) reste
  inchangé — il ne dépend pas du fournisseur Redis derrière `REDIS_URL`.

## 7. Healthcheck `/health/ready`

Comportement (inchangé, déjà correct avant cette migration, désormais
couvert par des tests dédiés qui n'existaient pas) :

- DB **et** Redis OK → `200`, `{"status": "ready", "components": {...}}`.
- DB **ou** Redis en erreur (connexion refusée, timeout, échec
  d'authentification, URL malformée) → `503`, `{"status": "degraded", ...}`.
- Le corps de réponse n'expose jamais que `type(exc).__name__` — jamais
  `str(exc)` (pourrait contenir l'URL/le mot de passe Redis en clair).

Nouveaux tests (6 cas) dans
[backend/tests/unit/test_health_ready.py](../backend/tests/unit/test_health_ready.py) :
DB+Redis ok (200), Redis connexion refusée (503), Redis timeout (503), Redis
échec d'authentification sans fuite du mot de passe dans le corps de
réponse (503), DB down même si Redis ok (503), URL Redis malformée (503, pas
de crash 500).

## 8. Classification des données Redis — migration à vide, en toute connaissance de cause

**Aucune supposition que Redis/Valkey est vide n'a été faite.** Analyse
explicite, par clé, de ce qui vit dans Redis aujourd'hui (Upstash) et si un
démarrage à vide côté Valkey est acceptable :

| Donnée | TTL | Perte si instance vide au démarrage | Verdict |
|---|---|---|---|
| Verrous d'idempotence webhooks (`claim_once`) | 3600s | Une commande déjà traitée pourrait être retraitée UNE fois après bascule — le code applicatif doit déjà tolérer un retraitement (webhooks Twilio/WhatsApp/PayDunya redélivrent naturellement) | **Sans risque** — comportement déjà toléré par construction |
| Cache résultats de recherche | TTL court (`_TTL_SECONDS`) | Un prochain appel recalcule et repeuple le cache | **Sans risque** — pur cache |
| Cible photo en attente (`pending_photo_target`) | TTL court | Un utilisateur en plein envoi de photo au moment exact de la bascule devrait renvoyer sa photo | **Risque mineur, transitoire** — fenêtre de bascule à minimiser (voir §9, migration sans coupure) |
| État santé / verrous de probe LLM Gateway | TTL court, recalculé en continu | Repart à un état "neutre", se reconstruit en quelques cycles | **Sans risque** |
| File Celery (tâches en attente non consommées) | — | Toute tâche `.delay()`-ée mais pas encore consommée avant la bascule serait perdue si l'ancien broker est coupé trop tôt | **Risque réel si mal séquencé** — c'est justement pourquoi la procédure §9 laisse Upstash actif en parallèle plusieurs heures avant toute coupure |
| Résultats de tâches Celery (result backend) | 3600s | Un résultat pas encore consulté serait perdu | **Risque mineur, transitoire** — peu de code consulte réellement les résultats de façon bloquante |

**Conclusion** : démarrer Valkey à vide est sûr pour la quasi-totalité des
données (courte durée de vie, recalculables, ou déjà tolérantes au
retraitement). Le seul risque réel — des tâches Celery en vol au moment
précis de la bascule du broker — est traité par la procédure sans coupure
ci-dessous (bascule un service à la fois, fenêtre de recouvrement, jamais de
coupure brutale d'Upstash).

## 9. Procédure de migration sans coupure (A→K)

**A. Vérifier la connectivité** (déjà fait avant cette migration, à
   reconfirmer juste avant de commencer) :
```bash
valkey-cli -h <IP_VALKEY> -p 6379 -a '<SECRET>' ping   # -> PONG
```

**B. Tester Valkey avec une clé de test temporaire**, avant de router le
   moindre trafic applicatif dessus :
```bash
valkey-cli -h <IP_VALKEY> -p 6379 -a '<SECRET>' set ladini:migration:test "ok" EX 60
valkey-cli -h <IP_VALKEY> -p 6379 -a '<SECRET>' get ladini:migration:test
```

**C. Mettre à jour `.env` en production** (sur le node, jamais commité) :
```
REDIS_URL=redis://:<SECRET>@<IP_VALKEY>:6379/0
```
Voir [.env.example](../.env.example) pour le format exact attendu. Régénérer
et mettre à jour `LADINI_APP_ENV_B64` (secret GitHub environment
`production`) — voir §10 pour la procédure exacte.

**D. Recréer les conteneurs** service par service, jamais tous en même
   temps, pour garder Upstash comme filet à chaque étape :
```bash
docker compose -f docker-compose.prod.yml up -d --no-deps api
# vérifier /health/ready (étape E) avant de continuer
docker compose -f docker-compose.prod.yml up -d --no-deps worker
docker compose -f docker-compose.prod.yml up -d --no-deps beat
docker compose -f docker-compose.prod.yml up -d --no-deps flower
```

**E. Vérifier `/health/ready`** après CHAQUE redéploiement de l'API :
```bash
curl -fsS https://<host>/health/ready | python3 -m json.tool
# attendu : {"status": "ready", "components": {"database": "ok", "redis": "ok"}}
```

**F. Vérifier `celery inspect ping`** (les workers répondent bien via le
   nouveau broker) :
```bash
docker compose -f docker-compose.prod.yml exec worker celery -A ladini.api.celery_app inspect ping
```

**G. Vérifier Beat** (planifie toujours ses tâches — surveiller les logs
   pour une exécution du prochain cron programmé, pas d'erreur de connexion
   broker) :
```bash
docker compose -f docker-compose.prod.yml logs -f beat --since 5m
```

**H. Vérifier Flower** (dashboard accessible, workers visibles, connecté au
   nouveau broker) :
```bash
curl -fsS -u "$FLOWER_USER:$FLOWER_PASSWORD" https://<host>:5555/api/workers
```

**I. Vérifier une vraie requête applicative de bout en bout** — envoyer un
   message WhatsApp/webchat de test réel et confirmer la réponse de l'agent
   (preuve que idempotence + broker + LLM gateway fonctionnent ensemble sur
   Valkey).

**J. Garder Upstash intact plusieurs heures** (rollback immédiat possible,
   §10) — ne RIEN désactiver côté Upstash à ce stade, même si tout semble
   fonctionner sur Valkey.

**K. Seulement après plusieurs heures stables sur Valkey** : désactiver/
   supprimer Upstash manuellement (action de l'opérateur, hors du scope
   automatisable de cette migration — cette procédure ne l'exécute jamais
   automatiquement).

## 10. Rollback

Si `/health/ready` reste `503` sur `redis` après l'étape D/E, ou si
`celery inspect ping` ne répond pas, ou si des tâches s'accumulent sans être
consommées :

1. Restaurer `REDIS_URL=<ancienne valeur Upstash>` dans `.env` (encore
   disponible tant qu'Upstash n'a pas été désactivé — voir §9.J, c'est
   précisément pourquoi Upstash est gardé actif plusieurs heures).
2. Régénérer `LADINI_APP_ENV_B64` avec cette valeur restaurée, mettre à jour
   le secret GitHub environment `production` (voir §11).
3. `docker compose -f docker-compose.prod.yml up -d --no-deps api worker beat
   flower` — même ordre que la migration, un service à la fois.
4. Reconfirmer `/health/ready` → `200`, `celery inspect ping` → réponse des
   workers.
5. Aucune donnée applicative n'est perdue par ce rollback : Postgres n'est
   jamais touché par cette migration, et les données Redis/Valkey sont par
   construction éphémères ou tolérantes au retraitement (voir §8).

## 11. `LADINI_APP_ENV_B64` — modifier sans jamais commiter de secret

Le secret GitHub environment `production` `LADINI_APP_ENV_B64` est décodé au
déploiement en `.env` sur le node (voir `.github/workflows/deploy.yml`,
étape "Configuration applicative" : `base64 -d` → `$DEPLOY_DIR/.env`).

1. Modifier un fichier `.env` LOCAL (jamais commité — vérifier `.gitignore`
   avant toute manipulation) contenant la nouvelle `REDIS_URL`.
2. Régénérer l'encodage :
   ```bash
   base64 -w0 .env > .env.b64
   ```
3. Copier le contenu de `.env.b64` dans le secret GitHub `LADINI_APP_ENV_B64`
   de l'environnement `production` (Settings → Environments → production →
   Secrets, sur GitHub — jamais via `gh secret set` avec la valeur en
   argument shell en clair, préférer `gh secret set LADINI_APP_ENV_B64 --env
   production < .env.b64` pour éviter qu'elle apparaisse dans l'historique
   shell).
4. Supprimer le fichier `.b64` local immédiatement après (`rm .env.b64`) —
   ne doit jamais traîner sur disque plus longtemps que nécessaire.
5. Redéployer (le pipeline (`.github/workflows/deploy.yml`) décode
   `LADINI_APP_ENV_B64` en `.env` sur le node au déploiement).

## 12. Observabilité

`redis_exporter` (`oliver006/redis_exporter:v1.66.0`, déjà déployé dans
[infra/exporters/docker-compose.exporters.yml](../infra/exporters/docker-compose.exporters.yml))
est basé sur la commande protocole `INFO`, standard et déjà Valkey-compatible
sans aucune modification — seule la valeur de `REDIS_ADDR`/`REDIS_URL` change
(même variable d'environnement, nouvelle valeur). Métriques déjà exposées et
scrapées par Alloy (`job_name = "ladini/redis"`,
[infra/alloy/config.alloy](../infra/alloy/config.alloy)) puis expédiées vers
Grafana Cloud : clients connectés, mémoire utilisée, commandes/sec,
évictions, hit/miss ratio, connexions rejetées, latence. **Grafana n'est PAS
déployé sur AWS** — Grafana Cloud reste l'unique destination, conformément à
la contrainte explicite de cette migration.

## 13. Coût

Upstash facturait à la commande au-delà du quota gratuit (602k commandes/mois
mesurées, plafond gratuit 500k). L'instance AWS EC2 Valkey a un coût fixe
mensuel prévisible (taille de l'instance choisie par l'opérateur), sans
plafond de commandes — élimine le risque de facture variable imprévisible
lié au volume de trafic WhatsApp/webchat.

## 14. Scaling

Le Valkey actuel est une instance EC2 unique, cohérent avec l'architecture
Redis externe partagée existante (un seul cluster Redis pour tous les nodes
Hetzner, voir
[docs/architecture/deployment-hetzner.md](architecture/deployment-hetzner.md)
§5). Si le volume de commandes dépasse la capacité d'une instance unique :
option la plus simple = upgrader la taille d'instance EC2 (vertical) avant
d'envisager Valkey Cluster (horizontal, changement d'architecture plus lourd,
hors scope de cette migration).

## 15. Troubleshooting

| Symptôme | Cause probable | Action |
|---|---|---|
| `/health/ready` → `503`, `redis: "error: ConnectionRefusedError"` | Security Group AWS bloque le port 6379 depuis l'IP du node Hetzner, ou Valkey down | Vérifier le SG AWS ; `valkey-cli -h <IP> -p 6379 -a '<SECRET>' ping` depuis le node Hetzner |
| `/health/ready` → `503`, `redis: "error: AuthenticationError"` | `REDIS_URL` a un mauvais mot de passe (`requirepass` Valkey) | Vérifier `.env` sur le node, jamais logguer le mot de passe pour déboguer — comparer avec la valeur connue côté opérateur |
| `celery inspect ping` ne répond pas | Worker toujours connecté à l'ancien broker (pas redéployé), ou `REDIS_URL` non propagée au conteneur worker | `docker compose ps`, vérifier que le worker a bien été recréé (§9.D) avec la nouvelle valeur |
| Tâches qui s'accumulent sans être consommées | Un worker écoute une file différente de celle attendue, ou reste sur l'ancien broker | Vérifier `CELERY_WORKER_QUEUES` et `TASK_ROUTES` (voir [test_celery_queue_routing.py](../backend/tests/unit/test_celery_queue_routing.py)) |
| `preflight.sh` échoue sur "REDIS_URL a un schéma valide" | `.env` a une URL malformée ou un schéma inattendu | Corriger `REDIS_URL` pour commencer par `redis://` ou `rediss://` |
| `preflight.sh` échoue sur "REDIS_URL ne contient aucun placeholder d'exemple" | `.env.example` copié sans remplir `CHANGE_ME`/`valkey-host` | Remplacer par la vraie valeur |

## 16. Tests ajoutés/modifiés par cette migration

- [backend/tests/unit/test_health_ready.py](../backend/tests/unit/test_health_ready.py) (nouveau, 6 tests) — branche Redis de `/health/ready`.
- [backend/tests/unit/test_celery_queue_routing.py](../backend/tests/unit/test_celery_queue_routing.py) (nouveau, 9 tests) — non-régression du routage/files Celery.
- [backend/tests/architecture/test_compose_deployment_invariants.py](../backend/tests/architecture/test_compose_deployment_invariants.py) (étendu, +4 tests) — pas de fallback `localhost`, api/worker/beat/flower partagent la même source `REDIS_URL`/`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`.
- [scripts/test/test-preflight-redis-url-checks.sh](../scripts/test/test-preflight-redis-url-checks.sh) (nouveau, 9 cas) — schéma `redis://`/`rediss://` accepté, schéma invalide rejeté, placeholder embarqué détecté, pas de faux positif sur une vraie URL.
- Tests TLS déjà existants ([backend/tests/unit/test_celery_tls_config.py](../backend/tests/unit/test_celery_tls_config.py)) et redaction de logs ([backend/tests/unit/test_log_redaction.py](../backend/tests/unit/test_log_redaction.py)) — vérifiés toujours au vert, non modifiés (déjà scheme-aware, déjà valides pour Valkey sans changement).

## 17. Ce qui n'a PAS été fait (hors scope, respecté explicitement)

Pas de RabbitMQ, pas de Kubernetes, pas de changement Terraform Hetzner non
nécessaire, aucune IP ni mot de passe en dur dans ce dépôt, aucun `.env`
commité, aucun `terraform apply`, aucun changement de nom/routage de file
Celery, aucun schéma DB touché, Upstash **non** désactivé/supprimé
automatiquement par ce travail (action manuelle de l'opérateur après la
fenêtre de stabilité, §9.K), aucun changement AWS destructif, Grafana
**non** déployé sur AWS (Grafana Cloud conservé).
