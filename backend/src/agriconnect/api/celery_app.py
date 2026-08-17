import logging
import os

from celery import Celery

from agriconnect.workers.beat_schedule import BEAT_SCHEDULE

# Configuration du logging pour Celery
logger = logging.getLogger(__name__)

# Utilise une variable d'environnement pour l'URL Redis, avec une valeur par défaut locale
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Initialisation de Celery
celery_app = Celery(
    "agriconnect_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    # Indique où Celery doit chercher les tâches à exécuter (agent + crons d'orchestration)
    include=[
        "agriconnect.api.tasks",
        "agriconnect.workers.crons.auction_solicitation",
        "agriconnect.workers.crons.proximity_matching",
        "agriconnect.workers.crons.outbox_dispatch",
        "agriconnect.workers.crons.order_expiry",
        "agriconnect.workers.payments.paydunya_ipn_task",
        "agriconnect.workers.media.product_photo_task",
    ],
)

# Configuration détaillée pour la résilience et la performance
celery_app.conf.update(
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
    broker_transport_options={"visibility_timeout": 660},
)

# Optionnel : configuration du mode "Task Always Eager" pour les tests unitaires
# Si tu veux que Celery exécute les tâches immédiatement sans worker (pour debug) :
# celery_app.conf.task_always_eager = False

if __name__ == "__main__":
    # Point d'entrée pour démarrer le worker via la ligne de commande
    celery_app.start()
