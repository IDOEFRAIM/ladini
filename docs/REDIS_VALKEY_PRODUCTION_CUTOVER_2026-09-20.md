# Bascule production Upstash → Valkey AWS via tunnel WireGuard — Runbook 2026-09-20

Complète [docs/REDIS_VALKEY_MIGRATION_2026-09-20.md](REDIS_VALKEY_MIGRATION_2026-09-20.md)
(architecture, TLS, compatibilité Valkey, cartographie des usages Redis —
toujours valide, non dupliqué ici) avec ce qui a changé depuis : **Valkey
n'est plus joint sur IP publique** mais via un tunnel WireGuard chiffré
(voir [infra/wireguard/README.md](../infra/wireguard/README.md)), le secret
de test doit être considéré compromis et régénéré, la procédure de bascule
Celery est reprise avec une vraie stratégie de cutover en 5 phases, et ce
document ajoute le hardening, l'observabilité, les alertes, le double
rollback, les tests de panne et la checklist GO/NO-GO demandés pour le
déploiement en production réelle.

**Ce document est la RÉFÉRENCE OPÉRATIONNELLE pour la bascule de demain.**

---

## A. Fichiers modifiés/créés par ce travail

| Fichier | Rôle |
|---|---|
| [infra/wireguard/README.md](../infra/wireguard/README.md) | Doc complète tunnel WireGuard, plan d'adressage, ordre d'installation, Security Group |
| [infra/wireguard/aws-wg0.conf.template](../infra/wireguard/aws-wg0.conf.template) | Template config wg0 côté AWS (placeholders, jamais de vraie clé) |
| [infra/wireguard/hetzner-wg0.conf.template](../infra/wireguard/hetzner-wg0.conf.template) | Template config wg0 côté Hetzner |
| [scripts/wireguard_setup_aws.sh](../scripts/wireguard_setup_aws.sh) | Installation + génération de clés côté AWS (idempotent, jamais de régénération accidentelle) |
| [scripts/wireguard_setup_hetzner.sh](../scripts/wireguard_setup_hetzner.sh) | Installation + génération de clés côté Hetzner |
| [scripts/check_valkey_network.sh](../scripts/check_valkey_network.sh) | Validation tunnel + Valkey de bout en bout, jamais de secret affiché, `exit 0`/`1` |
| [scripts/check_celery_broker_state.sh](../scripts/check_celery_broker_state.sh) | `inspect active/reserved/scheduled` + longueur de chaque file, broker jamais affiché |
| [scripts/preflight.sh](../scripts/preflight.sh) | +§4quinquies (host:port REDIS_URL joignable) +§4sexies (gate WireGuard optionnel, `WIREGUARD_REQUIRED=1`) |
| [.env.example](../.env.example) | +`WIREGUARD_REQUIRED` documenté |
| [backend/tests/architecture/test_compose_deployment_invariants.py](../backend/tests/architecture/test_compose_deployment_invariants.py) | +test MCP ne référence aucune variable Redis (invariant explicite) |
| [scripts/test/test-preflight-network-checks.sh](../scripts/test/test-preflight-network-checks.sh) | Régression sur §4quinquies/§4sexies |
| [scripts/predeploy_check.sh](../scripts/predeploy_check.sh), [.github/workflows/cicd.yml](../.github/workflows/cicd.yml) | Wiring des nouveaux tests |
| **Ce document** | Runbook opérationnel complet |

**Corrections apportées suite à revue (5 blockers, cette révision) :**

| # | Fichier | Rôle |
|---|---|---|
| 1 (cutover) | [backend/src/ladini/core/maintenance.py](../backend/src/ladini/core/maintenance.py) | Mécanisme de pause des producteurs Celery (fichier drapeau, `is_celery_producer_paused()`) |
| 1 | [scripts/celery_maintenance_mode.sh](../scripts/celery_maintenance_mode.sh) | Bascule `on`/`off`/`status`, instantané (`docker compose exec`, aucun redémarrage) |
| 1 | `api/routes/{market,paydunya_webhook,twilio_webhook,whatsapp_webhook}.py` | Chaque producteur Celery vérifie le mode maintenance AVANT `.delay()` — 503 (retry côté fournisseur) si actif |
| 1 | [backend/tests/unit/test_maintenance.py](../backend/tests/unit/test_maintenance.py), [test_maintenance_mode_producer_gate.py](../backend/tests/unit/test_maintenance_mode_producer_gate.py) | 13 tests — mécanisme + les 5 points d'entrée |
| 2 (.env réel) | [scripts/apply_env_change_on_node.sh](../scripts/apply_env_change_on_node.sh) | Backup + upsert + permissions 600 + vérification redactée sur le VRAI `.env` du node |
| 2 | [scripts/check_redis_target_consistency.sh](../scripts/check_redis_target_consistency.sh) | Confirme que tous les conteneurs ciblent le même host:port Redis, jamais le secret affiché |
| 2 | [scripts/test/test-apply-env-change-on-node.sh](../scripts/test/test-apply-env-change-on-node.sh) | 5 cas — backup, upsert, non-duplication, non-fuite de secret |
| 3 (maxmemory) | [scripts/compute_valkey_maxmemory.sh](../scripts/compute_valkey_maxmemory.sh) | Calcule `maxmemory` depuis la RAM réelle (`/proc/meminfo`), fraction par palier, jamais une valeur en dur |
| 3 | [scripts/test/test-compute-valkey-maxmemory.sh](../scripts/test/test-compute-valkey-maxmemory.sh) | 5 cas — paliers 40/50/60 %, `noeviction` toujours présent, surcharge manuelle |
| 4 (systemd) | [scripts/detect_valkey_systemd_unit.sh](../scripts/detect_valkey_systemd_unit.sh) | Détecte le VRAI nom d'unit (`valkey.service` vs `valkey-server.service`), échoue explicitement si ambigu/absent |
| 5 (bind order) | [scripts/configure_valkey_bind_wireguard.sh](../scripts/configure_valkey_bind_wireguard.sh) | Refuse de toucher `valkey.conf` tant que l'IP WireGuard n'existe pas réellement sur `wg0` |
| 4+5 | [scripts/test/test-valkey-bind-wireguard-order.sh](../scripts/test/test-valkey-bind-wireguard-order.sh) | 6 cas — refus wg0 absent/mauvaise IP, détection unit seul/absent/ambigu |
| — | [infra/wireguard/README.md](../infra/wireguard/README.md) | Ordre d'installation corrigé (WireGuard UP avant toute modif Valkey) |

**Aucun changement de schéma DB** — voir §H, la garantie Postgres durable
demandée existe déjà pour toutes les opérations critiques identifiées,
aucune nouvelle table/migration n'était nécessaire (audit détaillé ci-dessous).

---

## B. Architecture finale

```
Cloudflare
  → Hetzner Load Balancer
    → Hetzner app node(s) [10.200.0.1, .3, .4, ... sur le tunnel]
      → Caddy → FastAPI / Celery Worker / Beat / Flower
        │
        │  WireGuard UDP 51820, chiffré, IP privée tunnel
        ▼
      AWS EC2 [10.200.0.2 sur le tunnel]
        → Valkey :6379 (bind UNIQUEMENT sur 10.200.0.2 + 127.0.0.1)
  → PostgreSQL externe (inchangé, hors scope Redis)
```

`REDIS_URL` finale : `redis://:PASSWORD@10.200.0.2:6379/0` — jamais l'IP
publique AWS, jamais après retrait de l'exposition publique (§D). Pas de
`rediss://` : le tunnel WireGuard chiffre déjà tout le trafic ; le code
reste scheme-aware et `rediss://` continue de fonctionner sans régression
si la topologie change un jour (voir §E).

---

## C. WireGuard — voir infra/wireguard/README.md pour le détail complet

Résumé : plan d'adressage `10.200.0.0/24` vérifié **sans conflit** avec le
réseau privé Hetzner existant (`10.20.0.0/16`, deuxième octet différent) ni
avec le réseau Docker `agri_net` (allocation `172.x` par défaut). AWS =
`10.200.0.2` (hub, plusieurs peers possibles pour le scale-out), Hetzner
= `10.200.0.1` (premier node), `.3`/`.4`/... pour des nodes additionnels.

Installation : `scripts/wireguard_setup_aws.sh` puis
`scripts/wireguard_setup_hetzner.sh` (génèrent chacun leur propre paire de
clés, jamais transmise ni commitée — seules les clés PUBLIQUES s'échangent),
puis `--activate` des deux côtés avec la clé publique de l'autre, puis
`scripts/check_valkey_network.sh` pour valider AVANT tout changement de
Security Group.

---

## D. Sécurité AWS — Security Group, ordre exact

1. **AJOUTER D'ABORD** : inbound `UDP 51820`, source = `<IP_PUBLIQUE_HETZNER>/32`.
2. Valider `scripts/check_valkey_network.sh` → doit sortir `0`.
3. **SEULEMENT ENSUITE** retirer : inbound `TCP 6379` public (celui utilisé
   pour le test initial `valkey-cli ... ping` avant cette migration).
4. Re-valider `scripts/check_valkey_network.sh` → doit rester `0`.
5. `sudo ss -lntp | grep 6379` sur l'EC2 → attendu `10.200.0.2:6379` et/ou
   `127.0.0.1:6379`, **jamais** `0.0.0.0:6379` ni l'IP publique.

**Ne jamais inverser cet ordre.** Ne jamais retirer 6379 public avant que
l'étape 2 confirme le tunnel opérationnel — sinon perte totale d'accès à
Valkey (voir §L, rollback réseau).

---

## E. Valkey — configuration, secret, TLS

### Ordre obligatoire (corrige un risque réel identifié en revue)

**Ne JAMAIS configurer `bind 10.200.0.2 ...` avant que `wg0` soit UP et
porte réellement cette adresse** — un `bind()` sur une IP absente de toute
interface fait échouer Valkey au démarrage (ou pire, laisse `valkey.conf`
incohérent si le restart échoue à mi-chemin). Ordre imposé, dans CET ordre :

1. WireGuard UP des deux côtés (§C), `ip addr show wg0` confirme `10.200.0.2`
   présente côté AWS.
2. Régénérer le secret Valkey (ci-dessous) — peut se faire AVANT ou APRÈS
   le WireGuard, sans dépendance.
3. `scripts/configure_valkey_bind_wireguard.sh` — **refuse structurellement**
   de toucher `valkey.conf` si `10.200.0.2` n'existe pas encore sur `wg0`
   (vérifié programматiquement, pas supposé) ; détecte le VRAI nom d'unit
   systemd (jamais `valkey` en dur, voir plus bas) ; restart ; `PONG` local
   ; `PONG` via l'IP WireGuard.
4. `PONG` depuis Hetzner, à travers le tunnel (`scripts/check_valkey_network.sh`).
5. **Seulement ensuite**, retirer l'exposition publique 6379 (§D).

### Secret — considéré compromis, régénération obligatoire

Le mot de passe utilisé pendant les tests de connectivité initiaux (exposé
sur IP publique) est **compromis par construction** — ne jamais le
réutiliser.

```bash
# Sur l'EC2 AWS :
NEW_SECRET="$(openssl rand -hex 32)"
echo "Nouveau secret généré (à copier immédiatement dans un gestionnaire de secrets, PAS dans un fichier texte durable) :"
echo "$NEW_SECRET"
sudo sed -i "s/^requirepass .*/requirepass ${NEW_SECRET}/" /etc/valkey/valkey.conf   # chemin exact selon le paquet, voir ci-dessous
UNIT="$(bash scripts/detect_valkey_systemd_unit.sh)" && sudo systemctl restart "$UNIT"
# Test IMMÉDIAT depuis l'EC2 lui-même (avant même de toucher REDIS_URL) :
REDISCLI_AUTH="$NEW_SECRET" valkey-cli -h 127.0.0.1 -p 6379 --no-auth-warning ping   # -> PONG
unset NEW_SECRET
```

Puis, depuis un node Hetzner (via le tunnel, une fois WireGuard validé) :

```bash
REDISCLI_AUTH='<NOUVEAU_SECRET>' valkey-cli -h 10.200.0.2 -p 6379 --no-auth-warning ping   # -> PONG
```

Mettre à jour `.env` **RÉEL sur le node** (pas seulement le secret GitHub —
voir §F "REDIS_URL réel sur le node" pour la correction exacte de ce trou)
puis régénérer `LADINI_APP_ENV_B64` (voir §11 de
[docs/REDIS_VALKEY_MIGRATION_2026-09-20.md](REDIS_VALKEY_MIGRATION_2026-09-20.md)
pour la procédure exacte, inchangée par ce travail).

### Config Valkey (chemin exact selon le paquet — `/etc/valkey/valkey.conf` le plus courant)

```
bind 10.200.0.2 127.0.0.1
protected-mode yes
requirepass <NOUVEAU SECRET>
appendonly yes
appendfsync everysec
maxmemory <calculé — voir ci-dessous, JAMAIS une valeur en dur>
maxmemory-policy noeviction      # même politique que docker-compose.dev.yml::redis (§ raison : Valkey est le
                                  # broker Celery + verrous d'idempotence + disjoncteur LLM — une éviction
                                  # silencieuse sous pression mémoire serait pire qu'un OOM bruyant sur écriture)
dir /var/lib/valkey
logfile /var/log/valkey/valkey.log
```

**Jamais `bind 0.0.0.0`** — défense en profondeur indépendante du Security
Group (voir §C/§D). `protected-mode yes` refuse toute connexion sans
`requirepass` explicite même si `bind` était mal configuré par erreur.
**Appliquer via** `scripts/configure_valkey_bind_wireguard.sh` (voir
"Ordre obligatoire" ci-dessus) — jamais à la main, pour garantir le garde
sur l'ordre WireGuard.

### maxmemory — calculé depuis la RAM réelle, jamais en dur

**Ne jamais écrire `maxmemory 1gb`/`2gb`/... sans avoir vérifié la RAM
réelle de l'instance.** Sur l'EC2, AVANT de configurer :

```bash
bash scripts/compute_valkey_maxmemory.sh              # affiche seulement, ne modifie rien
sudo bash scripts/compute_valkey_maxmemory.sh --apply  # calcule ET écrit valkey.conf (backup automatique)
```

Règle : 40 % de la RAM totale si ≤ 2 Go, 50 % si 2-8 Go, 60 % si > 8 Go —
le reste est réservé au noyau Linux, à WireGuard, à `redis_exporter`, à la
fragmentation mémoire Valkey, et SURTOUT au `fork()` d'AOF rewrite (peut
transitoirement approcher 2× la mémoire résidente sur un dataset à forte
écriture — le pire cas à ne jamais sous-provisionner, un OOM killer qui
tue Valkey PENDANT un rewrite AOF est le scénario à éviter). Détail
complet du raisonnement dans l'en-tête du script. `maxmemory-policy
noeviction` toujours conservé.

**Monitoring avant saturation** (voir §J) : alerte WARNING à 75-80 % de
`maxmemory`, CRITICAL à 90 %.

### systemd — nom d'unit RÉEL, jamais supposé

**Ne jamais écrire `systemctl restart valkey` en dur** — certaines
installations Debian/Ubuntu exposent `valkey-server.service`, pas
`valkey.service` (dépend de la provenance du paquet : dépôt Debian vs
upstream Valkey). Détecter D'ABORD :

```bash
UNIT="$(bash scripts/detect_valkey_systemd_unit.sh)"
echo "Unit détectée : ${UNIT}"   # ex: valkey-server.service
sudo systemctl enable "$UNIT"
sudo systemctl status "$UNIT"   # Restart=always (ou équivalent) déjà fourni par l'unit par défaut du paquet — vérifier :
systemctl show "$UNIT" -p Restart -p RestartSec
```

`scripts/detect_valkey_systemd_unit.sh` échoue EXPLICITEMENT (jamais un
nom deviné) si aucune unit Valkey n'est trouvée, ou si plusieurs unites
distinctes existent (ambigu — décision manuelle requise). Si l'unit
détectée n'a pas `Restart=always` : créer un override
(`sudo systemctl edit "$UNIT"`) :
```ini
[Service]
Restart=always
RestartSec=5
```

### Disque — EBS persistant

Le volume EBS attaché à l'instance EC2 est persistant par construction
(survit à un arrêt/redémarrage de l'instance, contrairement au stockage
d'instance éphémère) — vérifier que `/var/lib/valkey` (chemin AOF) est bien
sur le volume EBS root ou un volume EBS additionnel monté, **pas** sur un
`instance store` éphémère. Permissions : `chown valkey:valkey
/var/lib/valkey`, `chmod 700`.

**Snapshot/sauvegarde** : programmer un snapshot EBS périodique (AWS Backup
ou un cron `aws ec2 create-snapshot`, hors scope de ce dépôt applicatif — à
documenter côté runbook infra AWS de l'opérateur). Acceptable pour Ladini
aujourd'hui : les données Valkey sont éphémères/recalculables ou protégées
au niveau Postgres (voir §H) — un snapshot EBS est une protection
supplémentaire contre une perte totale d'instance, pas une garantie
métier.

### TLS — inchangé, scheme-aware

`redis://` (tunnel WireGuard chiffré, choix actuel) → aucune option SSL
passée à `redis-py`/Celery. `rediss://` reste pleinement supporté sans
régression (`api/celery_app.py::_redis_ssl_options`, tests dans
[backend/tests/unit/test_celery_tls_config.py](../backend/tests/unit/test_celery_tls_config.py),
non modifiés, toujours verts) — utile si un futur Redis/Valkey managé public
remplace ce montage WireGuard.

---

## F. REDIS_URL — source unique, invariance testée

`REDIS_URL` reste l'unique source de vérité ; `CELERY_BROKER_URL`/
`CELERY_RESULT_BACKEND` restent dérivés par défaut
(`core/settings.py::celery_broker/celery_backend`, inchangé). Invariance
vérifiée par [test_compose_deployment_invariants.py](../backend/tests/architecture/test_compose_deployment_invariants.py) :

- `api`/`worker`/`beat` partagent **littéralement** l'ancre `x-full-app-env`
  (pas 3 copies indépendantes qui pourraient diverger).
- `flower` référence les MÊMES noms de variable (`${REDIS_URL...}`,
  `${CELERY_BROKER_URL...}`, `${CELERY_RESULT_BACKEND...}`), jamais une
  valeur en dur.
- `mcp` — **audité, ne touche pas Redis** (aucun import de
  `core.idempotency`/`redis` sous `protocols/mcp/`) — le nouveau test
  verrouille explicitement l'ABSENCE de ces variables sur `mcp`, pour que
  toute évolution future qui lui donnerait un besoin Redis soit un choix
  conscient (mise à jour du test), jamais une divergence silencieuse.
- `redis_exporter` (`infra/exporters/`) lit la même variable `REDIS_URL`
  (fichier `.env` séparé par construction du repo — les deux compose
  files doivent être déployés avec le MÊME `.env` en production, convention
  déjà en place, non changée par ce travail).

### REDIS_URL réel sur le node — trou corrigé (blocker #2)

**La configuration au niveau du repo (ancre partagée, invariance testée
ci-dessus) ne garantit PAS, à elle seule, que le `.env` RÉELLEMENT présent
sur `/opt/ladini/app/.env` a la nouvelle valeur, ni que chaque conteneur a
effectivement redémarré avec.** Deux angles morts identifiés et corrigés :

1. **Le fichier `.env` du node lui-même** — régénérer `LADINI_APP_ENV_B64`
   (secret GitHub) ne touche RIEN sur le node tant qu'un déploiement ne
   décode pas ce secret en `.env` (voir `.github/workflows/deploy.yml`).
   Pendant un cutover MANUEL (pas via un redéploiement GitHub Actions
   complet), il faut écrire `.env` sur le node directement :

   ```bash
   sudo bash scripts/apply_env_change_on_node.sh \
     REDIS_URL='redis://:<NOUVEAU_SECRET>@10.200.0.2:6379/0' \
     WIREGUARD_REQUIRED=1
   ```

   Ce script (1) sauvegarde `.env` AVANT toute modification (horodaté),
   (2) remplace `REDIS_URL`/`WIREGUARD_REQUIRED` s'ils existent déjà
   (jamais un doublon), les ajoute sinon, (3) permissions `600`, (4)
   affiche une vérification REDACTÉE (`redis://***@10.200.0.2:6379`,
   jamais le mot de passe). **En parallèle**, mettre EXACTEMENT le même
   contenu dans `LADINI_APP_ENV_B64` (§11 du doc de migration) — sinon le
   PROCHAIN déploiement GitHub Actions écraserait ce `.env` avec l'ancienne
   valeur Upstash, annulant silencieusement le cutover à la prochaine
   release.

2. **Chaque conteneur garde SON PROPRE environnement figé au démarrage** —
   écrire `.env` ne change RIEN pour un conteneur déjà en cours
   d'exécution tant qu'il n'est pas recréé (`docker compose up -d
   --no-deps <service>`). Après recréation de worker/api (§Q), vérifier
   qu'ils ciblent bien la MÊME destination :

   ```bash
   bash scripts/check_redis_target_consistency.sh
   ```

   Ce script fait calculer `scheme://host:port` (jamais le mot de passe)
   PAR CHAQUE conteneur lui-même (`docker compose exec <svc> python -c
   "..."`, urlsplit sur SA PROPRE variable d'environnement) — seul ce
   résultat redacté traverse vers ce script hôte. Une divergence entre
   `api`/`worker`/`beat`/`flower` signale immédiatement qu'un service n'a
   pas encore été recréé avec la nouvelle `REDIS_URL` — exactement le
   scénario "producteur sur A, consommateur sur B" que §G doit empêcher.

---

## G. Migration Celery — vraie stratégie de cutover (5 phases, AUCUNE fenêtre producteur/consommateur divergente)

**Le problème identifié en revue est réel et corrigé ici** : la version
précédente basculait le worker en premier, puis l'API "juste après" — mais
entre les deux, l'API continuait de publier sur Upstash pendant que plus
aucun worker Upstash n'existait pour consommer. **Corrigé par un mode
maintenance explicite** (§1 ci-dessus,
[core/maintenance.py](../backend/src/ladini/core/maintenance.py)) : PENDANT
toute la fenêtre où worker et API pourraient cibler des brokers différents,
**aucun producteur n'est autorisé à publier du tout** — les webhooks
renvoient 503 (retry côté fournisseur, message jamais perdu) au lieu de
publier vers un broker dont on n'est pas certain qu'un worker l'écoute.

**Stratégie : DRAINER → PAUSE TOTALE DES PRODUCTEURS → BASCULER worker PUIS
API PENDANT la pause → RÉOUVRIR LE TRAFIC → Beat → Flower.** Il n'existe
JAMAIS d'instant où un producteur actif et un worker actif regardent des
brokers différents — soit les deux sont sur l'ancien, soit les deux sont
sur le nouveau, soit AUCUN producteur n'est actif (pause). Alternative
envisagée et écartée : faire tourner un worker temporaire sur Upstash EN
PARALLÈLE d'un worker temporaire sur Valkey — techniquement possible mais
ajoute de la complexité opérationnelle (2 process Celery distincts, risque
de double-configuration de Beat) pour un gain marginal avec **moins de 100
utilisateurs** (`infra/inventory.example.yml`, Phase 1) — une interruption
contrôlée de 1-3 minutes est préférable et plus simple à opérer sous
pression le jour J.

### PHASE 1 — Drain + pause des producteurs

```bash
# 1. Confirmer les longueurs de file actuelles (avant tout arrêt) :
bash scripts/check_celery_broker_state.sh
# → noter les longueurs de interactive/background/scheduled/celery.

# 2. Stopper Beat (empêche la CRÉATION de nouvelles tâches planifiées) :
docker compose -f docker-compose.prod.yml --profile scheduler stop beat

# 3. Mettre les producteurs en pause (webhooks Twilio/WhatsApp/Paydunya +
#    market.py renvoient 503 dès CET instant — AUCUN nouveau message ne
#    sera publié tant que la pause est active ; Twilio/Paydunya
#    redélivrent après échec, WhatsApp Cloud API aussi, voir
#    core/maintenance.py pour le détail du tradeoff) :
bash scripts/celery_maintenance_mode.sh on
bash scripts/celery_maintenance_mode.sh status   # confirme "MAINTENANCE ACTIVE"
```

**À partir d'ici, plus AUCUNE nouvelle tâche n'est publiée nulle part** —
seules les tâches déjà en file avant la pause restent à drainer.

### PHASE 2 — Confirmer les files à 0 (ou état connu)

```bash
# Répéter jusqu'à convergence (quelques minutes suffisent en Phase 1,
# task_time_limit=600s borne le pire cas — et plus AUCUNE nouvelle tâche
# n'arrive depuis la Phase 1, donc cette convergence est monotone) :
watch -n 10 'bash scripts/check_celery_broker_state.sh'
```

Critère d'arrêt : `interactive`/`background`/`scheduled`/`celery` à `0`
(ou stables à une valeur connue et acceptée — ex. une tâche `EXECUTION_UNKNOWN`
déjà couverte par la réconciliation périodique, voir
[docs/REDIS_VALKEY_MIGRATION_2026-09-20.md](REDIS_VALKEY_MIGRATION_2026-09-20.md)
§2) ET `celery inspect active`/`reserved` vides (aucune tâche en cours).

### PHASE 3 — Basculer le worker (pendant que les producteurs sont TOUJOURS en pause)

```bash
# Arrêter le worker Upstash :
docker compose -f docker-compose.prod.yml --profile app stop worker

# Écrire la nouvelle REDIS_URL sur LE VRAI .env du node (voir §F, corrige
# le trou "LADINI_APP_ENV_B64 mis à jour mais .env du node pas garanti") :
sudo bash scripts/apply_env_change_on_node.sh \
  REDIS_URL='redis://:<NOUVEAU_SECRET>@10.200.0.2:6379/0' \
  WIREGUARD_REQUIRED=1

# Démarrer le worker sur Valkey :
docker compose -f docker-compose.prod.yml --profile app up -d --no-deps worker

# Valider :
docker compose -f docker-compose.prod.yml exec worker \
  celery -A ladini.api.celery_app inspect ping
```

**Aucun risque à ce stade même si cette phase prend plus de temps que
prévu** : les producteurs sont TOUJOURS en pause (§Phase 1) — il ne peut
pas exister de tâche publiée vers un broker que le worker n'écoute pas,
quelle que soit la durée de cette phase.

### PHASE 4 — Basculer l'API (toujours pendant la pause), puis réouvrir le trafic

```bash
docker compose -f docker-compose.prod.yml --profile app up -d --no-deps api
curl -fsS https://<host>/health/ready | python3 -m json.tool
# attendu : {"status": "ready", "components": {"database": "ok", "redis": "ok"}}

# Confirmer que worker ET api ciblent bien la MÊME destination (corrige le
# trou "aucun check ne confirme la cohérence réelle entre conteneurs") :
bash scripts/check_redis_target_consistency.sh --service api --service worker
# attendu : "✓ Tous les services interrogés ciblent la même destination"

# SEULEMENT MAINTENANT — api ET worker confirmés sur Valkey — réouvrir le
# trafic métier :
bash scripts/celery_maintenance_mode.sh off
bash scripts/celery_maintenance_mode.sh status   # confirme "normal"

# Beat puis Flower :
docker compose -f docker-compose.prod.yml --profile scheduler up -d --no-deps beat
docker compose -f docker-compose.prod.yml ps beat   # UN SEUL process (voir §K)

docker compose -f docker-compose.prod.yml --profile admin up -d --no-deps flower
curl -fsS -u "$FLOWER_USER:$FLOWER_PASSWORD" https://<host>:5555/api/workers
```

### PHASE 5 — Validation réelle

```bash
# Test réel : envoyer un message webchat/WhatsApp de test, confirmer une
# réponse de l'agent (preuve idempotence + broker + LLM gateway sur Valkey).
bash scripts/check_celery_broker_state.sh
# → confirmer que les nouvelles tâches du test apparaissent puis se vident
#   (produites ET consommées sur Valkey, pas d'accumulation).
```

### Durée visée

1-3 minutes entre l'activation de la pause (Phase 1) et sa désactivation
(Phase 4) — le drain (Phase 2) est généralement le plus rapide (files
déjà courtes en Phase 1, < 100 utilisateurs), et Phase 3 (bascule worker)
ne prend que le temps d'un `docker compose up -d --no-deps` + `inspect
ping`. Si Phase 2 ne converge pas en quelques minutes (file bloquée),
**NE PAS** basculer worker/API avec des tâches encore en vol — diagnostiquer
d'abord (`celery inspect active` montre quelle tâche bloque), voir §17
"test de panne" pour les scénarios de diagnostic.

---

## H. Idempotence — audit complet, garantie Postgres déjà en place

**Constat central, vérifié fichier par fichier (pas une supposition)** :
la garantie Postgres durable demandée ("même si Redis redémarre / perd 1s
d'AOF / TTL expire / migration, une opération métier critique ne doit
jamais s'appliquer deux fois") **existe déjà, en profondeur, pour toutes
les opérations à effet métier réel identifiées.** Aucune nouvelle table ni
migration n'a donc été créée — en ajouter une aurait dupliqué un mécanisme
déjà plus spécifique et plus mûr, pour un bénéfice nul et un risque réel la
veille d'un déploiement.

### `core/idempotency.py::claim_once` — où est le fail-open, et pourquoi il est sans danger ici

Fail-open confirmé (`return True` si Redis indisponible ou clé vide,
[core/idempotency.py:45-63](../backend/src/ladini/core/idempotency.py)) —
**seulement 2 call sites réels** :

1. [api/tasks.py:94](../backend/src/ladini/api/tasks.py) — DDL idempotent
   au démarrage worker (création d'index). Sans conséquence : ré-exécuter
   un `CREATE INDEX IF NOT EXISTS` n'a aucun effet métier.
2. [api/tasks.py:291](../backend/src/ladini/api/tasks.py) — dédoublonnage
   **au niveau du message WhatsApp/webchat** (`message_sid`), une
   optimisation de saut de retraitement, PAS la protection finale contre un
   double effet métier — voir ci-dessous pourquoi.
3. [api/response_dispatch.py:119](../backend/src/ladini/api/response_dispatch.py)
   — dédoublonnage de l'ENVOI d'une réponse sortante. Si Redis est down, le
   pire cas est un message WhatsApp dupliqué (texte identique renvoyé deux
   fois) — une gêne UX, **jamais** un double débit/double stock/double
   commande. Les notifications transactionnelles (paiement, gain
   d'enchère...) passent par l'Outbox (voir plus bas), pas ce mécanisme.

**Si Redis est down, la réexécution potentielle du tour conversationnel
(cas 2) invoque exactement les mêmes mutations métier qu'un tour normal —
et ces mutations ont leur PROPRE protection, indépendante de Redis :**

### Paiement (PayDunya) — `SELECT ... FOR UPDATE` + état explicite, indépendant de Redis

[services/database/escrow.py::mark_escrow_paid](../backend/src/ladini/services/database/escrow.py#L225)
verrouille `Order` en ligne (`.with_for_update()`), vérifie explicitement
`payment_status in {ESCROWED, PAID_OUT}` et retourne
`{"already_processed": True}` **sans redébiter le stock** si l'IPN Paydunya
est livré plusieurs fois (cas documenté dans le commentaire du code :
"Paydunya peut livrer le même IPN plusieurs fois"). Le débit de stock par
produit est LUI AUSSI verrouillé (`SELECT Product ... FOR UPDATE`). Le
worker [paydunya_ipn_task.py](../backend/src/ladini/workers/payments/paydunya_ipn_task.py)
re-confirme en plus le statut RÉEL directement auprès de Paydunya avant
toute écriture — jamais un champ du corps de l'IPN pris pour argent
comptant.

### Acceptation d'enchère (ACCEPT_BID) — même pattern

[services/database/auction.py::select_winning_bid](../backend/src/ladini/services/database/auction.py#L1450)
verrouille `Auction`+`Bid` (`.with_for_update(of=[Bid, Auction])`) et
rejette explicitement `auction_already_closed`/`bid_already_processed` —
état persisté en base, pas en Redis.

### Publication produit / commande / stock — même famille de garde

[domain/sales_publish_draft.py](../backend/src/ladini/graphs/agents/market_coach/domain/sales_publish_draft.py)
porte une machine à états explicite (`PUBLISHED`/`FAILED`/
`EXECUTION_UNKNOWN`/`EXECUTING`/`CANCELLED`) ; les débits de stock
(`services/database/producer.py`) utilisent systématiquement
`.with_for_update(of=Stock)`/`.with_for_update(of=MarketOffer)`.

### Appels d'outils MCP (la majorité des mutations métier passent par là)

[services/database/mcp_idempotency_store.py](../backend/src/ladini/services/database/mcp_idempotency_store.py)
— table Postgres dédiée `marketplace.mcp_idempotency_records`,
`PRIMARY KEY (idempotency_key, tool_name)`, sémantique explicite
`CLAIMED`/`REPLAY`/`CONFLICT`/`IN_PROGRESS`/`UNAVAILABLE` : même clé + même
payload rejoue le résultat déjà obtenu SANS ré-exécuter l'outil ; même clé +
payload différent → conflit explicite, jamais résolu silencieusement.
Entièrement indépendant de Redis.

### Notifications transactionnelles — Outbox avec contrainte UNIQUE

`notification_outbox`
([domain/intelligence/models.py:259](../backend/src/ladini/domain/intelligence/models.py))
porte `UNIQUE(dedupe_key)` — écrit dans la MÊME transaction que la mutation
métier (paiement confirmé, enchère gagnée...), garantie transactionnelle
classique ("transactional outbox pattern"), indépendante de Redis.

### Conclusion

| Opération | Protection SANS Redis | Fichier |
|---|---|---|
| Paiement PayDunya | `FOR UPDATE` + statut explicite | [escrow.py:225](../backend/src/ladini/services/database/escrow.py) |
| ACCEPT_BID | `FOR UPDATE` + `auction_already_closed`/`bid_already_processed` | [auction.py:1450](../backend/src/ladini/services/database/auction.py) |
| Débit stock | `FOR UPDATE` sur `Product`/`Stock` | [escrow.py](../backend/src/ladini/services/database/escrow.py), [producer.py](../backend/src/ladini/services/database/producer.py) |
| Publication produit | Machine à états (`EXECUTING`/`EXECUTION_UNKNOWN`/...) | [sales_publish_draft.py](../backend/src/ladini/graphs/agents/market_coach/domain/sales_publish_draft.py) |
| Appels outils MCP | Table Postgres dédiée, clé+hash | [mcp_idempotency_store.py](../backend/src/ladini/services/database/mcp_idempotency_store.py) |
| Notifications | Outbox `UNIQUE(dedupe_key)` | [intelligence/models.py:259](../backend/src/ladini/domain/intelligence/models.py) |
| Message WhatsApp dupliqué (UX, pas métier) | Aucune — fail-open Redis assumé | [response_dispatch.py:119](../backend/src/ladini/api/response_dispatch.py) |

**Donc : si Valkey redémarre, perd une seconde d'AOF, ou est temporairement
indisponible pendant la bascule, aucune opération à effet financier ou de
stock ne peut s'appliquer deux fois.** Le seul risque résiduel (message
conversationnel dupliqué) est une gêne UX déjà assumée par construction,
sans rapport avec cette migration.

---

## I. Hardening SPOF Valkey

Voir §E pour systemd/AOF/bind/disque. Résumé des points couverts :

- `systemctl enable <unit détectée>` + `Restart=always` (redémarrage auto)
  — unit RÉELLE, jamais `valkey` supposé
  ([scripts/detect_valkey_systemd_unit.sh](../scripts/detect_valkey_systemd_unit.sh)).
- `appendonly yes` + `appendfsync everysec` (fenêtre de perte ~1s, acceptée
  — voir §H, aucune donnée critique n'en dépend seule).
- `maxmemory` calculé depuis la RAM réelle (jamais en dur — voir
  [scripts/compute_valkey_maxmemory.sh](../scripts/compute_valkey_maxmemory.sh))
  + `maxmemory-policy noeviction` (échec bruyant plutôt qu'éviction
  silencieuse).
- `protected-mode yes` + `bind` restreint, appliqué SEULEMENT après
  confirmation que l'IP WireGuard existe réellement
  ([scripts/configure_valkey_bind_wireguard.sh](../scripts/configure_valkey_bind_wireguard.sh),
  défense en profondeur indépendante du Security Group).
- EBS persistant, snapshot périodique recommandé (action opérateur AWS,
  hors scope applicatif de ce dépôt).
- **Jamais deux Beat actifs** — voir §K, garanti par le `profiles: [scheduler]`
  singleton déjà testé
  ([test_compose_deployment_invariants.py::test_exactly_one_service_owns_the_scheduler_profile](../backend/tests/architecture/test_compose_deployment_invariants.py)).

---

## J. Observabilité et alertes

`redis_exporter` ([infra/exporters/docker-compose.exporters.yml](../infra/exporters/docker-compose.exporters.yml))
déjà déployé, basé sur `INFO` (protocole standard, Valkey-compatible sans
changement — seule la valeur de `REDIS_ADDR`/`REDIS_URL` change). Scrapé par
Alloy (`job_name = "ladini/redis"`) → **Grafana Cloud** (jamais de Grafana
déployé sur AWS, contrainte respectée).

Métriques déjà exposées par `redis_exporter` couvrant la demande : up/down
(`redis_up`), clients connectés (`redis_connected_clients`), mémoire
utilisée/max (`redis_memory_used_bytes`/`redis_memory_max_bytes`),
évictions (`redis_evicted_keys_total`), commandes/sec
(`redis_commands_processed_total`, dériver le taux), hits/miss
(`redis_keyspace_hits_total`/`redis_keyspace_misses_total`), connexions
rejetées (`redis_rejected_connections_total`), clients bloqués
(`redis_blocked_clients`), statut AOF (`redis_aof_enabled`,
`redis_aof_last_write_status`), latence (`redis_commands_duration_seconds_total`
si `--check-single-keys`/histogrammes de latence activés côté exporter —
vérifier la config actuelle de `infra/exporters/docker-compose.exporters.yml`
et ajouter `--include-system-metrics`/les flags de latence si absents).
Réplication non applicable (instance unique, pas de replica).

### Alertes recommandées (à créer côté Grafana Cloud — hors scope de ce dépôt applicatif, pas de config Terraform Grafana ici)

**CRITICAL**
- `redis_up == 0` (Valkey down)
- `redis_aof_last_write_status != "ok"` (erreur AOF)
- espace disque EBS `/var/lib/valkey` > 90 % (métrique node exporter, pas
  redis_exporter — vérifier qu'un node exporter tourne aussi sur l'EC2)
- `redis_memory_used_bytes / redis_memory_max_bytes > 0.90`
- `redis_rejected_connections_total` en hausse continue
- broker Celery injoignable (`celery_worker_up == 0` si `celery-exporter`
  expose cette métrique, sinon `/health/ready` en 503 prolongé — voir
  alerte HTTP existante si elle existe déjà côté Grafana Cloud)

**WARNING**
- `redis_memory_used_bytes / redis_memory_max_bytes > 0.75-0.80`
- latence commande élevée (p99, si le flag de latence de l'exporter est
  activé)
- profondeur de file Celery (`LLEN`) en augmentation continue sur plusieurs
  minutes (pas de métrique native `redis_exporter` pour une file
  applicative précise — envisager `celery-exporter` déjà présent dans
  `infra/exporters/docker-compose.exporters.yml` pour une métrique
  `celery_queue_length` si elle expose ce niveau de détail, sinon
  `scripts/check_celery_broker_state.sh` reste l'outil de diagnostic manuel)
- `redis_commands_processed_total` anormal (pic ou chute brutale)
- boucles de reconnexion (`redis_connections_received_total` anormalement
  élevé sur une courte fenêtre)

---

## K. Ne jamais avoir deux Beat actifs

Garanti structurellement : `profiles: ["scheduler"]` porté par EXACTEMENT
un service (`beat`) dans `docker-compose.prod.yml`, et
`infra/inventory.yml`/`scripts/validate_inventory.py` imposent qu'un seul
NODE porte ce rôle. Pendant la bascule (Phase 1 de §G), Beat est stoppé
puis redémarré une seule fois (Phase 4) — jamais deux instances
simultanées. Le rollback (§L) respecte la même contrainte : redémarrer
Beat dans le bon ordre, jamais en double pendant la transition.

Le mode maintenance (§G, §1) est ORTHOGONAL à cette garantie : il empêche
la PRODUCTION de tâches (webhooks/API), pas le nombre d'instances Beat —
les deux mécanismes protègent contre des risques différents
(perte/duplication de tâche vs. double-planification), et sont combinés
dans le cutover (Beat stoppé EN PLUS de la pause producteurs, Phase 1).

---

## L. Rollback — deux procédures distinctes

### L1. Rollback RÉSEAU (WireGuard KO)

```bash
# 1. Ré-ouvrir TEMPORAIREMENT l'accès public Security Group (source = IP
#    publique Hetzner UNIQUEMENT, jamais 0.0.0.0/0, même en urgence) :
#    Security Group AWS → ajouter inbound TCP 6379, source <IP_PUBLIQUE_HETZNER>/32
# 2. Si nécessaire, revenir temporairement à REDIS_URL sur IP publique AWS
#    (ancien format, avec le NOUVEAU secret — jamais l'ancien, compromis) :
#    REDIS_URL=redis://:<NOUVEAU_SECRET>@<IP_PUBLIQUE_AWS>:6379/0
# 3. Régénérer LADINI_APP_ENV_B64, redéployer.
# 4. Diagnostiquer le tunnel à tête reposée (scripts/check_valkey_network.sh),
#    PUIS revenir à la procédure normale (retirer l'accès public temporaire
#    une fois le tunnel réparé, voir §D).
```

### L2. Rollback BROKER (Valkey KO, tunnel OK)

**Si le rollback intervient PENDANT la fenêtre de cutover (§G, mode
maintenance déjà actif)** : ne PAS désactiver la pause avant d'avoir
restauré Upstash et confirmé `check_redis_target_consistency.sh` cohérent
— sinon la même fenêtre "producteur/consommateur divergents" que ce
rollback cherche à éviter se recrée dans l'autre sens.

```bash
# 1. Si la pause producteurs n'est pas déjà active (rollback DÉCOUVERT
#    après coup, hors fenêtre de cutover), l'activer D'ABORD :
bash scripts/celery_maintenance_mode.sh on

# 2. Restaurer l'ancienne REDIS_URL Upstash sur le VRAI .env du node (pas
#    seulement LADINI_APP_ENV_B64 — voir §F) :
sudo bash scripts/apply_env_change_on_node.sh REDIS_URL='<ancienne valeur Upstash>'
# 3. Régénérer LADINI_APP_ENV_B64 EN PARALLÈLE (voir §11 du doc de migration).
# 4. Redémarrer DANS LE BON ORDRE — jamais worker+api simultanément,
#    jamais deux Beat (voir §K) :
docker compose -f docker-compose.prod.yml --profile app stop worker
docker compose -f docker-compose.prod.yml --profile app up -d --no-deps worker
docker compose -f docker-compose.prod.yml exec worker celery -A ladini.api.celery_app inspect ping
docker compose -f docker-compose.prod.yml --profile app up -d --no-deps api
curl -fsS https://<host>/health/ready
bash scripts/check_redis_target_consistency.sh --service api --service worker

# 5. SEULEMENT MAINTENANT — api ET worker confirmés cohérents sur Upstash
#    — réouvrir le trafic :
bash scripts/celery_maintenance_mode.sh off

docker compose -f docker-compose.prod.yml --profile scheduler up -d --no-deps beat
docker compose -f docker-compose.prod.yml --profile admin up -d --no-deps flower
```

Aucune donnée applicative perdue par l'un ou l'autre rollback : Postgres
n'est jamais touché par cette migration, et les données Redis/Valkey sont
par construction éphémères ou protégées côté Postgres (§H).

---

## M. Tests de panne (staging/local — jamais destructif en production)

Simuler et vérifier, en local/staging :

| Scénario | Comment simuler | Vérifier |
|---|---|---|
| Valkey down | `docker compose stop redis` (dev) ou couper le process Valkey local | `/health/ready` → 503, API ne crash pas ([test_health_ready.py](../backend/tests/unit/test_health_ready.py) déjà couvre ce cas via mock) |
| Mauvais password | `REDIS_URL` avec un mot de passe invalide | `/health/ready` → 503, `AuthenticationError`, aucun secret dans le corps de réponse (déjà testé) |
| WireGuard down | `sudo wg-quick down wg0` en local/staging | `scripts/check_valkey_network.sh` → `exit 1`, détail clair sans secret |
| Timeout Redis | Bloquer le port via `iptables`/pare-feu local temporaire | `/health/ready` → 503 (`TimeoutError`, déjà testé) |
| Redis restart | `docker compose restart redis` (dev) | Reconnexion automatique `redis-py`/Celery, `/health/ready` repasse à 200 sans redémarrage manuel des services applicatifs |
| Worker restart | `docker compose restart worker` | `celery inspect ping` répond de nouveau après quelques secondes (`worker_proc_alive_timeout=60.0`, voir `celery_app.py`) |
| Beat restart | `docker compose restart beat` | Un seul process Beat après redémarrage (jamais deux), planification reprend |

Ces scénarios sont couverts par les tests unitaires existants
([test_health_ready.py](../backend/tests/unit/test_health_ready.py),
[test_celery_tls_config.py](../backend/tests/unit/test_celery_tls_config.py))
pour la partie applicative ; les scénarios réseau (WireGuard down, Valkey
down au niveau infra) sont à exercer manuellement en staging avant le
déploiement de demain si le temps le permet — **non automatisés dans ce
dépôt** (nécessitent une vraie infra WireGuard/EC2 de test, hors de portée
d'un test unitaire/CI).

---

## N. Test de bascule Celery reproductible

Procédure manuelle (nécessite un broker de test — non automatisée en CI,
qui n'a pas de Redis/Valkey réel disponible par défaut) :

1. Publier X tâches de test sur le broker A (`celery_app.send_task` ou un
   script ad hoc, queue dédiée `scheduled` par exemple pour ne pas
   perturber le trafic réel).
2. Vérifier consommation (`scripts/check_celery_broker_state.sh` — la file
   redescend à 0, `inspect active` montre les tâches en cours puis vides).
3. Drainer complètement (attendre 0 partout).
4. Basculer `REDIS_URL` vers le broker B (Valkey de test).
5. Publier X nouvelles tâches sur le broker B.
6. Vérifier consommation sur B de la même façon.
7. Confirmer via les logs applicatifs (chaque tâche de test loggue un
   identifiant unique) qu'aucune tâche n'est absente des logs de
   consommation.
8. Confirmer qu'aucun identifiant n'apparaît deux fois dans les logs de
   consommation (pas de duplication).

**Limite explicite** : ce test n'a pas été exécuté de bout en bout contre
la vraie instance Valkey AWS dans le cadre de ce travail (nécessiterait un
accès direct à l'infra AWS/Hetzner que cette session n'a pas) — la
procédure ci-dessus est prête à l'emploi pour l'opérateur, à exécuter en
staging avant la bascule de demain si le temps le permet, ou pendant la
Phase 5 du cutover (§G) avec le trafic réel comme test de facto.

---

## O. Docker Compose — validation

```bash
RELEASE_VERSION=check REDIS_URL="redis://:dummy@10.200.0.2:6379/0" \
  MCP_HTTP_AUTH_TOKEN=x FLOWER_USER=x FLOWER_PASSWORD=x GROQ_API_KEY=x \
  DB_HOST=x DB_USER=x DB_PASSWORD=x DB_NAME=x \
  docker compose -f docker-compose.prod.yml config --quiet
```

Validé (§18 ci-dessous) : `api`/`worker`/`beat`/`flower` lisent tous
`REDIS_URL` depuis la même source (`x-full-app-env`/mêmes noms de
variable), aucun fallback `localhost` en production (vérifié par
[test_compose_deployment_invariants.py](../backend/tests/architecture/test_compose_deployment_invariants.py)).

---

## P. GitHub Actions — deploy.yml

`LADINI_APP_ENV_B64` → décodé en `.env` sur le node (inchangé). Aucun
secret n'apparaît dans les logs du workflow (`set -Eeuo pipefail`, jamais
de `echo "$LADINI_APP_ENV_B64"` ni de `cat .env`). Le pipeline échoue déjà
AVANT toute mutation si une variable requise manque (`DEPLOY_DIR`,
`LADINI_APP_ENV_B64`, `PUBLIC_DOMAIN`, `ACME_EMAIL`) — voir
[.github/workflows/deploy.yml](../.github/workflows/deploy.yml) lignes 93-116.

**Gate WireGuard avant redémarrage** : `preflight.sh` (appelé par
`cluster_deploy.sh`/`node_deploy.sh` **avant toute mutation**, voir
`scripts/cluster_deploy.sh:204`/`scripts/node_deploy.sh:201`) applique
désormais le §4sexies si `.env` contient `WIREGUARD_REQUIRED=1` — le
déploiement échoue proprement, sans toucher aux conteneurs, si le tunnel
ou Valkey ne sont pas joignables. **Action opérateur requise** : ajouter
`WIREGUARD_REQUIRED=1` dans le `.env.production` LOCAL utilisé pour
régénérer `LADINI_APP_ENV_B64` (voir §11 du doc de migration) une fois le
tunnel validé — pas de changement de workflow YAML nécessaire, le gate est
déjà lu depuis `.env` par `preflight.sh`.

---

## Q. Runbook — ordre exact de déploiement demain

**Règle absolue : chaque étape a une COMMANDE, un RÉSULTAT ATTENDU, et une
action IF FAILURE. Si le résultat obtenu ne correspond pas au résultat
attendu, exécuter IF FAILURE et NE PAS passer à l'étape suivante — jamais
"ça devrait passer quand même".**

---

**0. Backup**

- COMMAND : (commande de backup Postgres habituelle de l'opérateur — hors
  scope Redis, non redéfinie ici, mais réflexe avant toute opération de prod)
- EXPECTED RESULT : backup confirmé (taille non nulle, horodatage récent)
- IF FAILURE → STOP. Ne pas commencer le cutover sans backup DB frais.

**1. Vérifier Upstash actuel**

- COMMAND : `bash scripts/check_celery_broker_state.sh` (avec `REDIS_URL`
  pointant encore sur Upstash)
- EXPECTED RESULT : sortie affiche `active`/`reserved`/`scheduled` +
  longueurs de file — noter les valeurs, exit 0
- IF FAILURE → STOP. Le broker actuel doit être joignable avant de
  commencer — sinon on ne peut pas confirmer le drain plus tard (Phase 2).

**2. Vérifier AWS (EC2 up, Valkey joignable en LOCAL sur l'EC2)**

- COMMAND (sur l'EC2, via SSH) :
  ```bash
  UNIT="$(bash scripts/detect_valkey_systemd_unit.sh)" && echo "unit=${UNIT}"
  sudo systemctl status "$UNIT"
  REDISCLI_AUTH='<secret ACTUEL, compromis mais encore actif>' valkey-cli -h 127.0.0.1 -p 6379 --no-auth-warning ping
  ```
- EXPECTED RESULT : `unit=valkey-server.service` (ou équivalent détecté,
  jamais supposé) ; `systemctl status` → `active (running)` ; `PONG`
- IF FAILURE → STOP. Corriger Valkey côté AWS avant de toucher au réseau.

**3. Générer le nouveau secret Valkey (l'actuel est compromis — exposé sur IP publique)**

- COMMAND (sur l'EC2) :
  ```bash
  NEW_SECRET="$(openssl rand -hex 32)"
  sudo sed -i "s/^requirepass .*/requirepass ${NEW_SECRET}/" /etc/valkey/valkey.conf
  UNIT="$(bash scripts/detect_valkey_systemd_unit.sh)" && sudo systemctl restart "$UNIT"
  REDISCLI_AUTH="$NEW_SECRET" valkey-cli -h 127.0.0.1 -p 6379 --no-auth-warning ping
  ```
- EXPECTED RESULT : `PONG` avec le NOUVEAU secret ; copier `$NEW_SECRET`
  immédiatement dans un gestionnaire de secrets (jamais un fichier texte
  durable) — il sera réutilisé aux étapes 8/10/11
- IF FAILURE → STOP. Ne pas continuer avec un secret non confirmé.

**4. Configurer WireGuard AWS**

- COMMAND (sur l'EC2) : `sudo bash scripts/wireguard_setup_aws.sh`
- EXPECTED RESULT : affiche une clé publique AWS — la noter
- IF FAILURE → STOP. Corriger l'installation WireGuard avant de continuer.

**5. Configurer WireGuard Hetzner**

- COMMAND (sur le node Hetzner) : `sudo bash scripts/wireguard_setup_hetzner.sh`
- EXPECTED RESULT : affiche une clé publique Hetzner — la noter, puis
  `--activate` des deux côtés avec les clés échangées (voir
  [infra/wireguard/README.md](../infra/wireguard/README.md) §3-4)
- IF FAILURE → STOP.

**6. Tester le tunnel (handshake)**

- COMMAND (des deux côtés) : `sudo wg show wg0`
- EXPECTED RESULT : un peer listé, `latest handshake` récent (< quelques
  dizaines de secondes)
- IF FAILURE → STOP. Diagnostiquer : Security Group UDP 51820 ouvert
  côté AWS (source = IP publique Hetzner) ? Clés correctement échangées ?

**7. Vérifier que l'IP WireGuard existe RÉELLEMENT avant de toucher à Valkey**

- COMMAND (sur l'EC2) : `ip addr show wg0`
- EXPECTED RESULT : `10.200.0.2/24` apparaît dans la sortie
- IF FAILURE → STOP. **NE JAMAIS passer à l'étape 8 si cette IP n'apparaît
  pas** — configurer `bind` sur une IP absente ferait échouer Valkey au
  redémarrage.

**8. Configurer le bind Valkey sur l'IP WireGuard + PONG local + PONG via le tunnel**

- COMMAND (sur l'EC2) :
  ```bash
  VALKEY_PASSWORD="$NEW_SECRET" sudo -E bash scripts/configure_valkey_bind_wireguard.sh --wg-ip 10.200.0.2
  ```
- EXPECTED RESULT : le script progresse à travers ses 8 étapes internes
  jusqu'à `✓ PONG (10.200.0.2)` — **le script REFUSE lui-même** (exit ≠ 0,
  aucune modification) si l'étape 7 n'a pas été validée entretemps
- IF FAILURE → STOP. Si le script a refusé au garde IP : revenir à
  l'étape 6/7. Si le PONG échoue après écriture : vérifier le secret,
  vérifier les logs Valkey (`journalctl -u <unit détectée>`).

**9. PONG depuis Hetzner, à travers le tunnel**

- COMMAND (sur le node Hetzner) :
  ```bash
  VALKEY_PASSWORD="$NEW_SECRET" sudo -E bash scripts/check_valkey_network.sh
  ```
- EXPECTED RESULT : exit 0, toutes les vérifications vertes (wg0, peer,
  handshake, TCP, PONG authentifié)
- IF FAILURE → STOP. Ne PAS passer à l'étape 10 (retrait de l'exposition
  publique) tant que ceci n'est pas vert — c'est le DERNIER filet avant de
  couper l'accès public.

**10. Retirer l'exposition publique 6379 (Security Group AWS)**

- COMMAND : (action manuelle console/CLI AWS — retirer la règle inbound
  TCP 6379 source `0.0.0.0/0` ou large, celle utilisée pour les tests
  initiaux)
- EXPECTED RESULT : règle retirée ; re-exécuter l'étape 9
  (`check_valkey_network.sh`) → reste exit 0 (preuve que Valkey reste
  joignable EXCLUSIVEMENT via le tunnel)
- IF FAILURE (le re-test échoue après retrait) → **rollback réseau
  immédiat** (§L1 : ré-ouvrir temporairement la règle, diagnostiquer à
  tête reposée).

**11. Écrire la nouvelle REDIS_URL sur le VRAI `.env` du node**

- COMMAND (sur le node Hetzner) :
  ```bash
  sudo bash scripts/apply_env_change_on_node.sh \
    REDIS_URL="redis://:${NEW_SECRET}@10.200.0.2:6379/0" \
    WIREGUARD_REQUIRED=1
  ```
- EXPECTED RESULT : backup horodaté créé, vérification redactée affichée
  (`REDIS_URL=redis://***@10.200.0.2:6379`), permissions 600
- IF FAILURE → STOP. Restaurer depuis le backup affiché si une écriture
  partielle a eu lieu.

**12. Régénérer LADINI_APP_ENV_B64 EN PARALLÈLE (même contenu exact)**

- COMMAND :
  ```bash
  base64 -w0 /opt/ladini/app/.env > .env.b64
  gh secret set LADINI_APP_ENV_B64 --env production < .env.b64
  rm -f .env.b64
  ```
- EXPECTED RESULT : `gh secret set` confirme la mise à jour
- IF FAILURE → STOP et corriger avant de continuer — **sinon le PROCHAIN
  déploiement GitHub Actions écraserait le `.env` du node avec l'ancienne
  valeur Upstash**, annulant silencieusement tout ce cutover à la
  prochaine release.

**13. Stopper Beat**

- COMMAND : `docker compose -f docker-compose.prod.yml --profile scheduler stop beat`
- EXPECTED RESULT : conteneur `beat` arrêté (`docker compose ps beat` → vide/`Exited`)
- IF FAILURE → STOP.

**14. Activer le mode maintenance (pause TOTALE des producteurs)**

- COMMAND :
  ```bash
  bash scripts/celery_maintenance_mode.sh on
  bash scripts/celery_maintenance_mode.sh status
  ```
- EXPECTED RESULT : `MAINTENANCE ACTIVE` affiché
- IF FAILURE → STOP. Le conteneur `api` est-il démarré ? Sans cette
  pause active, NE PAS continuer vers l'étape 16 (arrêt du worker
  Upstash) — c'est elle qui garantit l'absence de fenêtre
  producteur/consommateur divergente.

**15. Drainer Upstash — attendre convergence**

- COMMAND : `watch -n 10 'bash scripts/check_celery_broker_state.sh'`
- EXPECTED RESULT : `interactive`/`background`/`scheduled`/`celery` → `0`
  (ou état connu et accepté, voir §G Phase 2) ET `inspect active`/`reserved`
  vides
- IF FAILURE (ne converge pas après plusieurs minutes) → STOP. NE PAS
  couper le worker Upstash avec des tâches en vol. Diagnostiquer
  (`celery inspect active` montre la tâche qui bloque) avant de continuer.
  La pause producteurs (étape 14) reste active pendant ce diagnostic —
  sans risque supplémentaire tant qu'elle l'est.

**16. Stopper le worker Upstash**

- COMMAND : `docker compose -f docker-compose.prod.yml --profile app stop worker`
- EXPECTED RESULT : conteneur `worker` arrêté
- IF FAILURE → STOP.

**17. Démarrer le worker sur Valkey**

- COMMAND : `docker compose -f docker-compose.prod.yml --profile app up -d --no-deps worker`
- EXPECTED RESULT : conteneur `worker` `Up`/`healthy`
- IF FAILURE → **rollback broker** (§L2) : la pause producteurs (étape 14)
  est TOUJOURS active, donc aucune perte de tâche à ce stade — restaurer
  l'ancienne `REDIS_URL` et relancer le worker sur Upstash avant de
  désactiver la pause.

**18. `celery inspect ping`**

- COMMAND : `docker compose -f docker-compose.prod.yml exec worker celery -A ladini.api.celery_app inspect ping`
- EXPECTED RESULT : au moins un worker répond (`pong`)
- IF FAILURE → **rollback broker** (§L2), pause toujours active.

**19. Démarrer/recréer l'API sur Valkey**

- COMMAND : `docker compose -f docker-compose.prod.yml --profile app up -d --no-deps api`
- EXPECTED RESULT : conteneur `api` `Up`/`healthy`
- IF FAILURE → **rollback broker** (§L2), pause toujours active.

**20. `/health/ready`**

- COMMAND : `curl -fsS https://<host>/health/ready | python3 -m json.tool`
- EXPECTED RESULT : `{"status": "ready", "components": {"database": "ok", "redis": "ok"}}`, HTTP 200
- IF FAILURE → **rollback broker** (§L2), pause toujours active.

**21. Confirmer que worker ET api ciblent la MÊME destination**

- COMMAND : `bash scripts/check_redis_target_consistency.sh --service api --service worker`
- EXPECTED RESULT : `✓ Tous les services interrogés ciblent la même destination : redis://10.200.0.2:6379`
- IF FAILURE (divergence détectée) → STOP, **NE PAS désactiver la pause
  producteurs**. Un service n'a pas encore été recréé avec la nouvelle
  `REDIS_URL` — identifier lequel (affiché explicitement) et le recréer,
  puis ré-exécuter cette étape avant de continuer.

**22. Désactiver le mode maintenance — réouvrir le trafic métier**

- COMMAND :
  ```bash
  bash scripts/celery_maintenance_mode.sh off
  bash scripts/celery_maintenance_mode.sh status
  ```
- EXPECTED RESULT : `normal (pas de maintenance active)`
- IF FAILURE → réessayer ; si le conteneur `api` ne répond pas, diagnostiquer
  avant de considérer le cutover terminé (le trafic reste bloqué tant que
  ce n'est pas confirmé désactivé).

**23. Démarrer Beat**

- COMMAND : `docker compose -f docker-compose.prod.yml --profile scheduler up -d --no-deps beat`
- EXPECTED RESULT : conteneur `beat` `Up` ; `docker compose ps beat` →
  EXACTEMENT une ligne (jamais deux instances, voir §K)
- IF FAILURE → STOP, diagnostiquer (les crons resteront simplement en
  pause, sans risque de double-exécution tant que rien n'est démarré en double).

**24. Démarrer Flower**

- COMMAND : `docker compose -f docker-compose.prod.yml --profile admin up -d --no-deps flower`
- EXPECTED RESULT : `curl -fsS -u "$FLOWER_USER:$FLOWER_PASSWORD" https://<host>:5555/api/workers` liste le(s) worker(s) connecté(s)
- IF FAILURE → non-bloquant pour le trafic métier (Flower est un
  dashboard, pas un composant critique) mais à corriger avant de
  considérer le cutover terminé (§R exige Flower dans le GO/NO-GO).

**25. Test webchat réel**

- COMMAND : envoyer un message de test via l'interface webchat
- EXPECTED RESULT : réponse de l'agent reçue
- IF FAILURE → STOP, diagnostiquer avant de considérer le cutover terminé
  (le trafic est déjà réouvert à l'étape 22 — un échec ici est un signal
  fort, pas une simple vérification cosmétique).

**26. Test WhatsApp réel**

- COMMAND : envoyer un message de test WhatsApp
- EXPECTED RESULT : réponse de l'agent reçue
- IF FAILURE → STOP, diagnostiquer (voir §M pour les scénarios de panne).

**27. Vérifier les métriques**

- COMMAND : consulter Grafana Cloud (dashboard `redis_exporter`/`celery-exporter`)
- EXPECTED RESULT : `redis_up == 1`, commandes/sec cohérentes avec le
  trafic réel, pas d'évictions, pas de connexions rejetées
- IF FAILURE → diagnostiquer (§J pour les seuils d'alerte) — non-bloquant
  pour le trafic déjà réouvert, mais à résoudre avant §29/§30.

**28. Garder Upstash disponible comme rollback**

- COMMAND : (aucune action — ne RIEN désactiver côté Upstash)
- EXPECTED RESULT : Upstash reste intact et accessible
- IF FAILURE : n/a (étape d'abstention)

**29. Surveiller 1h**

- COMMAND : répéter périodiquement `bash scripts/check_celery_broker_state.sh` et `curl -fsS https://<host>/health/ready`
- EXPECTED RESULT : stable, aucune accumulation de file, `/health/ready` reste 200
- IF FAILURE → évaluer un rollback broker (§L2) selon la gravité.

**30. Surveiller plusieurs heures**

- COMMAND : surveillance passive via Grafana Cloud + health checks périodiques
- EXPECTED RESULT : stable sur la durée
- IF FAILURE → rollback broker (§L2) si nécessaire.

**31. Seulement ensuite, désactiver/supprimer Upstash**

- COMMAND : (action manuelle dans la console Upstash — hors scope
  automatisable de ce dépôt, JAMAIS fait automatiquement)
- EXPECTED RESULT : Upstash désactivé, fenêtre de rollback broker (§L2)
  définitivement fermée — ne faire ceci qu'après confiance totale
- IF FAILURE : n/a (action manuelle finale)

---

## R. GO / NO-GO — checklist finale

**GO uniquement si TOUT est coché :**

- [ ] WireGuard handshake OK des deux côtés (`sudo wg show wg0`)
- [ ] IP `10.200.0.2` confirmée présente sur `wg0` AVANT toute modification
      de `valkey.conf` (étape 7 du runbook — jamais dans l'autre ordre)
- [ ] Unit systemd Valkey détectée explicitement, jamais supposée
      (`scripts/detect_valkey_systemd_unit.sh` — un seul match, sans ambiguïté)
- [ ] `maxmemory` calculé depuis la RAM réelle de l'EC2
      (`scripts/compute_valkey_maxmemory.sh`), jamais une valeur en dur ;
      `maxmemory-policy noeviction` confirmé
- [ ] Valkey `PONG` via IP tunnel (`scripts/check_valkey_network.sh` → `0`)
- [ ] 6379 public fermé côté Security Group AWS (vérifié APRÈS l'étape 9 du runbook)
- [ ] Nouveau secret Valkey actif (ancien secret jamais réutilisé)
- [ ] `.env` RÉEL sur le node Hetzner confirmé avec la nouvelle `REDIS_URL`
      (`scripts/apply_env_change_on_node.sh`, backup présent, permissions 600)
      — pas seulement `LADINI_APP_ENV_B64`
- [ ] `LADINI_APP_ENV_B64` régénéré avec EXACTEMENT le même contenu que le
      `.env` du node (sinon le prochain déploiement GitHub Actions
      rétablirait Upstash silencieusement)
- [ ] Mode maintenance activé AVANT l'arrêt du worker Upstash, désactivé
      SEULEMENT après confirmation `check_redis_target_consistency.sh`
      (aucune fenêtre producteur/consommateur divergente, voir §G)
- [ ] `check_redis_target_consistency.sh` confirme api/worker/beat/flower
      sur la MÊME destination Redis
- [ ] `/health/ready` → 200 (`database: ok`, `redis: ok`)
- [ ] `celery inspect ping` répond depuis le worker
- [ ] Un seul process Beat actif (`docker compose ps beat`)
- [ ] Flower voit le(s) worker(s) connecté(s)
- [ ] File Upstash à 0 (ou état connu et accepté) avant bascule worker
- [ ] File Valkey fonctionne (tâche de test produite ET consommée)
- [ ] Test agent réel OK (webchat ET WhatsApp)
- [ ] Mode maintenance confirmé DÉSACTIVÉ (`scripts/celery_maintenance_mode.sh status` → `normal`) — trafic métier réellement réouvert
- [ ] Grafana Cloud reçoit les métriques `redis_exporter`
- [ ] Rollback réseau (§L1) ET broker (§L2) documentés et compris par
      l'opérateur de garde
- [ ] Secrets GitHub (`LADINI_APP_ENV_B64`) mis à jour et confirmés
      (redéploiement de test si possible)
- [ ] Tests backend verts (voir §S)
- [ ] Tests architecture verts (voir §S)

**Sinon : NO-GO.** Ne pas retirer l'exposition publique 6379, ne pas
basculer le worker, garder Upstash comme broker actif jusqu'à résolution.

---

## S. Tests exécutés (cette session)

```bash
# Unitaires/architecture pertinents :
cd backend
python -m pytest tests/unit/test_health_ready.py tests/unit/test_celery_queue_routing.py \
  tests/architecture/test_compose_deployment_invariants.py tests/unit/test_celery_tls_config.py \
  tests/unit/test_log_redaction.py tests/unit/test_maintenance.py \
  tests/unit/test_maintenance_mode_producer_gate.py -q
# → tous verts (49 tests). Le test order-dependent connu de
#   test_log_redaction.py (échoue seulement quand le FICHIER complet
#   tourne, jamais isolément — reproduit identiquement sur un tree
#   propre sans aucun changement de cette migration, voir comparaison
#   git stash de la mission précédente) reste sans rapport avec ce travail.

# Shell — régression complète des 5 blockers + tout ce qui précède :
bash scripts/test/test-preflight-redis-url-checks.sh       # 9/9
bash scripts/test/test-preflight-network-checks.sh          # 9/9
bash scripts/test/test-valkey-bind-wireguard-order.sh       # 6/6 (blockers #4/#5)
bash scripts/test/test-compute-valkey-maxmemory.sh          # 5/5 (blocker #3)
bash scripts/test/test-apply-env-change-on-node.sh          # 5/5 (blocker #2)
bash scripts/test/test-deploy-lock-architecture.sh
bash scripts/test/test-node-preflight-lock.sh
bash -n scripts/preflight.sh scripts/predeploy_check.sh scripts/wireguard_setup_aws.sh \
  scripts/wireguard_setup_hetzner.sh scripts/check_valkey_network.sh scripts/check_celery_broker_state.sh \
  scripts/celery_maintenance_mode.sh scripts/apply_env_change_on_node.sh \
  scripts/compute_valkey_maxmemory.sh scripts/detect_valkey_systemd_unit.sh \
  scripts/configure_valkey_bind_wireguard.sh scripts/check_redis_target_consistency.sh
# → tous OK

# shellcheck : toujours indisponible dans cet environnement (déjà noté
# lors d'une mission précédente) — à exécuter côté opérateur si disponible.

# docker compose config :
RELEASE_VERSION=x REDIS_URL="redis://:dummy@10.200.0.2:6379/0" \
  MCP_HTTP_AUTH_TOKEN=x FLOWER_USER=x FLOWER_PASSWORD=x GROQ_API_KEY=x \
  DB_HOST=x DB_USER=x DB_PASSWORD=x DB_NAME=x \
  docker compose -f docker-compose.prod.yml config --quiet   # OK

# scripts/check_redis_target_consistency.sh : validé contre de VRAIS
# conteneurs en cours d'exécution sur cette machine (pas seulement un
# sandbox mocké) — confirme 4/4 services cohérents.

# Scripts WireGuard/systemd/bind (wireguard_setup_*, configure_valkey_
# bind_wireguard.sh) : testés en --dry-run et en environnement sandboxé
# (fake `ip`/`systemctl`, pas de vraie infra WireGuard/EC2 disponible
# dans cette session) — voir §M pour ce qui reste à valider manuellement
# en staging/production réelle par l'opérateur.
```

**Limite explicite** : cette session n'a pas d'accès direct à l'EC2 AWS ni
au node Hetzner de production — tous les scripts WireGuard/réseau ont été
testés pour leur LOGIQUE (syntaxe, extraction de motifs, comportement sur
port ouvert/fermé, non-fuite de secret) en sandbox local, jamais contre la
vraie infra. L'opérateur doit exécuter le runbook (§Q) pas à pas demain, en
vérifiant chaque commande avant de passer à la suivante.
