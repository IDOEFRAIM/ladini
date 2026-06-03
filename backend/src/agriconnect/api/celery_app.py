from celery import Celery
import os
import logging

# Configuration du logging pour Celery
logger = logging.getLogger(__name__)

# Utilise une variable d'environnement pour l'URL Redis, avec une valeur par défaut locale
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Initialisation de Celery
celery_app = Celery(
    "agriconnect_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    # Indique où Celery doit chercher les tâches à exécuter
    include=["agriconnect.api.tasks"]
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
)

# Optionnel : configuration du mode "Task Always Eager" pour les tests unitaires
# Si tu veux que Celery exécute les tâches immédiatement sans worker (pour debug) :
# celery_app.conf.task_always_eager = False 

if __name__ == "__main__":
    # Point d'entrée pour démarrer le worker via la ligne de commande
    celery_app.start()