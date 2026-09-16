import logging

from celery import Celery

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
        "ladini.workers.crons.preorder_reconciliation",
        "ladini.workers.crons.sales_publish_reconciliation",
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

# Optionnel : configuration du mode "Task Always Eager" pour les tests unitaires
# Si tu veux que Celery exécute les tâches immédiatement sans worker (pour debug) :
# celery_app.conf.task_always_eager = False

if __name__ == "__main__":
    # Point d'entrée pour démarrer le worker via la ligne de commande
    celery_app.start()
