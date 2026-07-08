"""
Worker — Point d'entrée Celery (production-grade).

USAGE:
    # Worker standard (toutes les queues) :
    celery -A agriconnect.worker:celery_app worker --loglevel=info -Q default,ai,voice,whatsapp,monitoring,maintenance

    # Worker IA dédié (recommandé en production) :
    celery -A agriconnect.worker:celery_app worker --loglevel=info -Q ai -c 2 --max-tasks-per-child=100

    # Worker voice/whatsapp (I/O-bound) :
    celery -A agriconnect.worker:celery_app worker --loglevel=info -Q voice,whatsapp -c 4

    # Celery Beat (scheduler) :
    celery -A agriconnect.worker:celery_app beat --loglevel=info

    # Flower (monitoring) :
    celery -A agriconnect.worker:celery_app flower --port=5555

L'app Celery et les tâches sont définies dans backend/workers/.
Ce fichier ne fait que réexporter pour compatibilité CLI.
"""

from agriconnect.workers.celery_app import celery_app  # noqa: F401

__all__ = ["celery_app"]
