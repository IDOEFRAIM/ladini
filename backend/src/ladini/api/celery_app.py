import logging
import os
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from celery import Celery
from celery.signals import worker_ready, worker_shutdown

from ladini.core.log_redaction import install_log_redaction
from ladini.core.settings import settings
from ladini.workers.beat_schedule import BEAT_SCHEDULE

# Le plus tôt possible — avant que Celery (worker ET beat, qui importent
# tous deux ce module en premier) ne produise ses propres logs. Voir
# core/log_redaction.py pour le mécanisme (numéros de téléphone + secrets
# structurés) et le pourquoi (Alloy expédie stdout des conteneurs vers
# Grafana Cloud Loki tel quel).
install_log_redaction()

# Configuration du logging pour Celery
logger = logging.getLogger(__name__)

# (2026-09-16, chantier Hetzner scale-out) : passait par `os.getenv("REDIS_URL",
# ...)` directement, contournant `settings.celery_broker`/`celery_backend`
# (core/settings.py) — qui existent justement pour permettre à
# CELERY_BROKER_URL/CELERY_RESULT_BACKEND de diverger de REDIS_URL si besoin
# un jour (§5 du chantier), avec repli sur REDIS_URL sinon. Un seul point de
# vérité désormais, jamais deux lectures indépendantes de la même variable.
BROKER_URL = settings.celery_broker
RESULT_BACKEND = settings.celery_backend

# ═══════════════════════════════════════════════════════════════════════
# §BUG CORRIGÉ ICI (2026-09-18, incident réel production, release sha-891cb2f)
# — le worker crashait au démarrage : `ValueError: A rediss:// URL must have
# parameter ssl_cert_reqs and this must be set to CERT_REQUIRED, CERT_OPTIONAL,
# or CERT_NONE`, levée depuis celery/backends/redis.py::RedisBackend.__init__.
#
# Cause racine exacte, confirmée en lisant le code source installé (pas une
# supposition) :
#   - `celery_app.py` ne passait JAMAIS `broker_use_ssl`/`redis_backend_use_ssl`
#     — aucune des deux options n'a de valeur par défaut côté Celery.
#   - Côté BROKER (kombu/transport/redis.py, via kombu/connection.py::
#     Connection._init_params) : un schéma `rediss://` SANS `ssl=` explicite
#     ne lève PAS d'exception — kombu retombe SILENCIEUSEMENT sur
#     `ssl_cert_reqs=CERT_NONE` (aucune vérification de certificat), avec
#     seulement un warning ("Secure redis scheme specified (rediss) with no
#     ssl options, defaulting to insecure SSL behaviour.") — visible dans les
#     logs du worker, mais un warning, pas un crash.
#   - Côté RESULT BACKEND (celery/backends/redis.py::RedisBackend.__init__) :
#     AUCUN filet de sécurité équivalent — un schéma `rediss://` SANS
#     `redis_backend_use_ssl=` explicite (donc sans `ssl_cert_reqs` dans
#     connparams) lève un `ValueError` bloquant, IMMÉDIATEMENT à
#     l'initialisation du backend. C'est CE crash, pas le broker, qui
#     apparaissait dans les logs du worker (traceback dans
#     celery/backends/redis.py) — le worker Docker restait "healthy" (le
#     healthcheck ne sonde qu'un `pgrep`, voir docker-compose.prod.yml) alors
#     que le PROCESS CELERY LUI-MÊME avait déjà crashé au boot.
#
# Fix : calculer explicitement les options SSL pour CHAQUE URL (broker ET
# backend peuvent diverger — CELERY_BROKER_URL/CELERY_RESULT_BACKEND restent
# des surcharges optionnelles de REDIS_URL, voir settings.py::celery_broker/
# celery_backend), UNE SEULE FOIS ici — worker/beat/flower important tous les
# trois CE MÊME module, aucune duplication de logique TLS entre eux.
# `redis://` (schéma non sécurisé, ex: dev local) → `None`, aucune config SSL
# forcée (Celery lève lui-même une erreur si des params SSL sont présents sur
# un schéma `redis://` non-TLS, voir celery/backends/redis.py::_params_from_url
# — cohérent, on ne doit donc RIEN passer dans ce cas).
# `rediss://` → `ssl_cert_reqs` explicite, résolu depuis
# `settings.REDIS_TLS_CERT_REQS` (défaut "required" = `ssl.CERT_REQUIRED`,
# vérification complète du certificat — jamais `CERT_NONE` par défaut,
# contrairement au repli silencieux de kombu décrit ci-dessus).
# ═══════════════════════════════════════════════════════════════════════
_SSL_CERT_REQS_BY_NAME = {
    "required": ssl.CERT_REQUIRED,
    "optional": ssl.CERT_OPTIONAL,
    "none": ssl.CERT_NONE,
}


def _redis_ssl_options(url: str) -> dict | None:
    """`rediss://` -> dict d'options SSL explicites pour broker_use_ssl /
    redis_backend_use_ssl (format attendu par Celery : un dict avec au moins
    `ssl_cert_reqs`). Tout autre schéma (`redis://`, ...) -> None : aucune
    config SSL n'est forcée."""
    if urlsplit(url).scheme != "rediss":
        return None
    cert_reqs_name = str(getattr(settings, "REDIS_TLS_CERT_REQS", "required") or "required").strip().lower()
    cert_reqs = _SSL_CERT_REQS_BY_NAME.get(cert_reqs_name)
    if cert_reqs is None:
        logger.warning(
            "REDIS_TLS_CERT_REQS=%r inconnu (valides: %s) — repli sur 'required' (CERT_REQUIRED).",
            cert_reqs_name, sorted(_SSL_CERT_REQS_BY_NAME),
        )
        cert_reqs = ssl.CERT_REQUIRED
    elif cert_reqs != ssl.CERT_REQUIRED:
        logger.warning(
            "REDIS_TLS_CERT_REQS=%r — vérification de certificat AFFAIBLIE pour la connexion Redis "
            "(devrait être 'required' sauf raison documentée).",
            cert_reqs_name,
        )
    return {"ssl_cert_reqs": cert_reqs}


BROKER_SSL = _redis_ssl_options(BROKER_URL)
BACKEND_SSL = _redis_ssl_options(RESULT_BACKEND)

# Initialisation de Celery
celery_app = Celery(
    "ladini_worker",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    # Indique où Celery doit chercher les tâches à exécuter (agent + crons d'orchestration)
    include=[
        "ladini.api.tasks",
        "ladini.workers.crons.auction_solicitation",
        "ladini.workers.crons.proximity_matching",
        "ladini.workers.crons.outbox_dispatch",
        "ladini.workers.crons.order_expiry",
        "ladini.workers.crons.procurement_reconciliation",
        "ladini.workers.crons.recurring_need_reconciliation",
        "ladini.workers.crons.recurring_need_occurrence_replenishment",
        "ladini.workers.crons.preorder_reconciliation",
        "ladini.workers.crons.sales_publish_reconciliation",
        "ladini.workers.crons.recurring_need_matching",
        "ladini.workers.crons.recurring_supply_digest",
        "ladini.workers.crons.agent_telemetry_retention",
        "ladini.workers.payments.paydunya_ipn_task",
        "ladini.workers.media.product_photo_task",
    ],
)

# (2026-09-16, chantier Hetzner scale-out, §8) : un tour WhatsApp interactif
# (`process_agent_task`, sensible à la latence perçue par l'utilisateur) et
# une tâche de fond lente (upload photo, reconciliation périodique) partageaient
# la MÊME file par défaut ("celery") — un burst de tâches de fond pouvait donc
# retarder une réponse WhatsApp derrière `worker_prefetch_multiplier=1`
# (une tâche à la fois par process). Séparation en 3 files, justifiée par ce
# risque réel de tête-de-ligne, pas par principe : le nombre de workers par
# file reste piloté par CELERY_WORKER_QUEUES (docker-compose.prod.yml) — un
# node worker dédié "interactive" peut être ajouté sans toucher au code.
_INTERACTIVE_QUEUE = "interactive"
_BACKGROUND_QUEUE = "background"
_SCHEDULED_QUEUE = "scheduled"

TASK_ROUTES = {
    "ladini.api.tasks.process_agent_task": {"queue": _INTERACTIVE_QUEUE},
    "ladini.workers.media.product_photo_task.*": {"queue": _BACKGROUND_QUEUE},
    "ladini.workers.payments.paydunya_ipn_task.*": {"queue": _BACKGROUND_QUEUE},
    "ladini.workers.crons.*": {"queue": _SCHEDULED_QUEUE},
    "workers.*": {"queue": _SCHEDULED_QUEUE},  # noms courts posés par BEAT_SCHEDULE
}

# Configuration détaillée pour la résilience et la performance
celery_app.conf.update(
    task_routes=TASK_ROUTES,
    # 0. TLS Redis (voir le bloc §BUG CORRIGÉ ICI ci-dessus) — `None` sur un
    #    schéma `redis://` (équivalent à ne rien passer, Celery lève lui-même
    #    une erreur si des params SSL traînent sur un schéma non-TLS) ; un
    #    dict `{"ssl_cert_reqs": ...}` sur `rediss://`. Broker et backend
    #    calculés SÉPARÉMENT : ils peuvent légitimement diverger (§5 du
    #    chantier scale-out) et chacun doit refléter SON PROPRE schéma.
    broker_use_ssl=BROKER_SSL,
    redis_backend_use_ssl=BACKEND_SSL,
    # 1. Gestion des tâches : la tâche n'est supprimée du broker qu'APRÈS exécution réussie
    task_acks_late=True,
    # 2. Performance : Un worker ne traite qu'une tâche à la fois
    # Empêche la saturation mémoire si une tâche prend du temps (comme ton graphe)
    worker_prefetch_multiplier=1,
    # 3. Sérialisation sécurisée et performante (JSON est obligatoire pour ton API)
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # 4. Limites de sécurité : timeout de 10 minutes par tâche
    task_time_limit=600,
    # 5. Gestion des résultats : expire après 1 heure pour ne pas saturer Redis
    result_expires=3600,
    # 6. S'assure que les files d'attente sont bien traitées
    task_create_missing_queues=True,
    # 7. Orchestration proactive : planification Celery Beat + fuseau UTC
    #    (cohérent avec les datetime naïfs UTC stockés en base).
    beat_schedule=BEAT_SCHEDULE,
    timezone="UTC",
    # 8. Redis broker : le visibility_timeout par défaut est 3600s (1h).
    #    Si un worker meurt (OOM, redeploy forcé) AVANT d'acquitter une tâche
    #    (task_acks_late=True ci-dessus), Redis ne la redélivre à un autre
    #    worker qu'après ce délai — jusqu'à 1h de "tâche fantôme" invisible
    #    pour un job qui dure au plus task_time_limit=600s. On aligne le
    #    visibility_timeout juste au-dessus du time_limit pour une reprise
    #    rapide sans redélivrer une tâche encore légitimement en cours.
    broker_transport_options={
        "visibility_timeout": 660,
        # (2026-09-17, follow-up pre-Hetzner) — sans ça, un `.delay()` (côté
        # API, webhook) ou une opération broker côté worker peut rester
        # bloqué très longtemps contre un Redis injoignable, borné
        # uniquement par le timeout TCP par défaut du système — mesuré en
        # E2E local : un webhook attendait encore ~16s après une coupure
        # Redis complète malgré le passage en thread séparé
        # (asyncio.to_thread, voir whatsapp_webhook.py/twilio_webhook.py) —
        # ce correctif-ci réduit la latence de CETTE tentative elle-même,
        # complémentaire (pas redondant) du to_thread qui protège les
        # AUTRES requêtes concurrentes sur le même worker gunicorn pendant
        # ce temps.
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
    },
    # (2026-09-17, follow-up pre-Hetzner — suite du correctif ci-dessus) :
    # `socket_connect_timeout` borne UNE tentative de connexion, mais Celery
    # retente ensuite lui-même jusqu'à `broker_connection_max_retries` fois
    # (défaut 100, avec backoff croissant) avant de laisser l'exception
    # remonter à l'appelant — mesuré en direct : un `.delay()` restait
    # bloqué 25s+ malgré le socket_connect_timeout=3 ci-dessus, à cause de
    # CE retry englobant. Borné à 2 tentatives : un webhook (chemin
    # synchrone, déjà protégé par asyncio.to_thread pour ne pas geler
    # d'AUTRES requêtes pendant ce temps) doit échouer vite plutôt que de
    # rejouer une connexion déjà classée injoignable une dizaine de fois.
    broker_connection_retry=True,
    broker_connection_max_retries=2,
    # 9. `worker_process_init` (api/tasks.py::init_worker_process) fait des
    #    allers-retours RÉSEAU synchrones (Langfuse cloud, warm-up + DDL sur
    #    la DB distante) avant de pouvoir répondre "UP" au processus maître.
    #    Le défaut de billiard (`PROC_ALIVE_TIMEOUT=4.0s`) suppose un
    #    démarrage quasi instantané ; avec 4 enfants qui font ça en même
    #    temps sur un lien distant, ça dépasse régulièrement 4s → le maître
    #    tue le processus (SIGKILL) AVANT la fin de son init, en reforke un
    #    qui répète le même travail lent et se fait tuer à son tour : boucle
    #    infinie de kills observée en prod/dev (2026-09-12), 0 worker
    #    n'atteint jamais l'état "up". On donne une marge large — un
    #    démarrage normal prend 1-3s, un démarrage sous contention réseau/CPU
    #    (4 enfants simultanés) peut monter bien plus haut sans que ce soit
    #    le signe d'un vrai blocage.
    worker_proc_alive_timeout=60.0,
)

# ═══════════════════════════════════════════════════════════════════════
# Marqueur de démarrage RÉUSSI — healthcheck (2026-09-18, incident sha-891cb2f)
# ═══════════════════════════════════════════════════════════════════════
# Pourquoi : `docker-compose.prod.yml`/`Dockerfile.worker` sondent la santé du
# worker via `pgrep -f 'celery.*worker'` (process vivant), délibérément PAS
# `celery inspect ping` (un aller-retour broker en direct sur CHAQUE probe —
# reverté le 2026-09-10 : un simple hoquet Redis faisait redémarrer TOUS les
# workers en boucle, aggravant l'incident au lieu de le signaler). Mais
# `pgrep` seul a un angle mort CONCRET, confirmé lors de CET incident : un
# worker qui crashe au boot (ex: le `ValueError` TLS corrigé ci-dessus) puis
# `restart: unless-stopped` en boucle laisse, À CHAQUE cycle, une brève
# fenêtre où un process "celery...worker" existe réellement (le temps
# d'importer, de se connecter, avant de crasher) — largement suffisant pour
# qu'une probe `pgrep` (intervalle 30s) l'attrape "vivant" par pur
# échantillonnage, alors que ce worker n'a JAMAIS terminé son démarrage.
# `docker healthy` mentait donc pendant que Celery crash-loopait réellement.
#
# Fix : `celery.signals.worker_ready` ne se déclenche QU'APRÈS une connexion
# broker+backend réussie et le mingle terminé — impossible d'y arriver si le
# process crashe pendant l'initialisation (avant même l'event loop). On pose
# un fichier marqueur À CE moment précis ; le healthcheck exige les DEUX :
# `pgrep` (un process vit CE round) ET ce marqueur (CE process a RÉELLEMENT
# fini de démarrer au moins une fois). Pas de round-trip broker à chaque
# probe (contrairement à `inspect ping`) — juste `test -f`, aussi bon marché
# et insensible aux pannes Redis transitoires que `pgrep` seul, mais qui ne
# peut plus mentir sur un crash-loop au boot. `worker_shutdown` nettoie le
# marqueur par hygiène (le redémarrage du conteneur y suffit déjà : `/tmp`
# n'est pas un volume persistant ici).
WORKER_READY_MARKER = os.environ.get("CELERY_WORKER_READY_FILE", "/tmp/celery_worker_ready")


@worker_ready.connect
def _mark_worker_ready(**_kwargs) -> None:
    try:
        Path(WORKER_READY_MARKER).write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:  # jamais bloquant : un healthcheck plus faible
        logger.warning("Impossible d'écrire le marqueur de santé worker (%s) : %s", WORKER_READY_MARKER, exc)


@worker_shutdown.connect
def _clear_worker_ready(**_kwargs) -> None:
    try:
        Path(WORKER_READY_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


# Horodatage de publication des tâches (temps d'attente en file d'un tour) — API ET worker importent ce module.
from ladini.core import turn_telemetry as _turn_telemetry  # noqa: E402

_turn_telemetry.connect_celery_signals()

# Optionnel : configuration du mode "Task Always Eager" pour les tests unitaires
# Si tu veux que Celery exécute les tâches immédiatement sans worker (pour debug) :
# celery_app.conf.task_always_eager = False

if __name__ == "__main__":
    # Point d'entrée pour démarrer le worker via la ligne de commande
    celery_app.start()
