"""
Celery Tasks — Modules de tâches asynchrones.

Découverts automatiquement par celery_app.autodiscover_tasks().

Modules:
  - ai          : Traitement IA (orchestrateur LangGraph)
  - marketplace : Traitement des actions de matching en background
  - voice       : TTS/STT Azure Speech
  - whatsapp    : Messages Twilio
  - monitoring  : Surveillance météo périodique
  - maintenance : Nettoyage, health checks, housekeeping
"""

from . import ai, maintenance, marketplace, monitoring, voice, whatsapp

__all__ = [
    "ai",
    "maintenance",
    "marketplace",
    "monitoring",
    "voice",
    "whatsapp",
]
