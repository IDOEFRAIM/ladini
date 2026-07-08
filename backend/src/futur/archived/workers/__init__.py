"""
Workers — Celery async task infrastructure (production-grade).

Architecture:
  - celery_app      : L'app Celery unique (backend.workers.celery_app)
  - celery_config   : Configuration externalisée per-environment
  - task_base       : Classe de base AgriTask (logging, backoff, classification)
  - tasks/ai        : Traitement IA via l'orchestrateur
  - tasks/voice     : TTS / STT Azure
  - tasks/whatsapp  : Envoi WhatsApp via Twilio
  - tasks/monitoring: Météo périodique (Celery Beat)
  - tasks/maintenance: Nettoyage, health checks
"""
