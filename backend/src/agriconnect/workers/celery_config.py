"""
Celery Configuration — Production-grade settings per environment.

Centralise TOUTE la configuration Celery (broker, queues, routing,
rate limits, retry policies, serialization, sécurité).

Principe : aucun magic number dans celery_app.py ni dans les tâches.
Tout est ici, documenté et ajustable par variable d'environnement.
"""

import os
from kombu import Exchange, Queue

# ── Environment Detection ───────────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
IS_PRODUCTION = ENVIRONMENT == "production"
IS_STAGING = ENVIRONMENT == "staging"

# ── Exchanges ───────────────────────────────────────────────
default_exchange = Exchange("default", type="direct")
priority_exchange = Exchange("priority", type="direct")
dlx_exchange = Exchange("dlx", type="direct")  # Dead Letter Exchange

# ── Queues ──────────────────────────────────────────────────
# Chaque domaine $métier a sa propre queue avec dead-letter routing.
TASK_QUEUES = (
    # Queue par défaut (fallback)
    Queue(
        "default",
        exchange=default_exchange,
        routing_key="default",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": "dlx.default",
        },
    ),
    # IA / Orchestrateur — tâches lourdes, concurrence limitée
    Queue(
        "ai",
        exchange=default_exchange,
        routing_key="ai",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": "dlx.ai",
            "x-max-length": 500,  # évite l'accumulation en cas de panne LLM
        },
    ),
    # Matching marketplace en arrière-plan
    Queue(
        "marketplace",
        exchange=default_exchange,
        routing_key="marketplace",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": "dlx.marketplace",
            "x-max-length": 1000,
        },
    ),
    # Voice — TTS/STT Azure
    Queue(
        "voice",
        exchange=default_exchange,
        routing_key="voice",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": "dlx.voice",
            "x-max-length": 1000,
        },
    ),
    # WhatsApp — messages Twilio (rate-limited côté provider)
    Queue(
        "whatsapp",
        exchange=default_exchange,
        routing_key="whatsapp",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": "dlx.whatsapp",
            "x-max-length": 2000,
        },
    ),
    # Monitoring — tâches périodiques (Beat)
    Queue(
        "monitoring",
        exchange=default_exchange,
        routing_key="monitoring",
        queue_arguments={
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": "dlx.monitoring",
        },
    ),
    # Maintenance — nettoyage, tâches système
    Queue(
        "maintenance",
        exchange=default_exchange,
        routing_key="maintenance",
    ),
    # Dead Letter Queue — tâches échouées après tous les retries
    Queue(
        "dead_letter",
        exchange=dlx_exchange,
        routing_key="dlx.#",
    ),
)

# ── Task Routing ────────────────────────────────────────────
TASK_ROUTES = {
    "backend.workers.tasks.ai.*": {"queue": "ai", "routing_key": "ai"},
    "backend.workers.tasks.marketplace.*": {"queue": "marketplace", "routing_key": "marketplace"},
    "backend.workers.tasks.voice.*": {"queue": "voice", "routing_key": "voice"},
    "backend.workers.tasks.whatsapp.*": {"queue": "whatsapp", "routing_key": "whatsapp"},
    "backend.workers.tasks.monitoring.*": {"queue": "monitoring", "routing_key": "monitoring"},
    "backend.workers.tasks.maintenance.*": {"queue": "maintenance", "routing_key": "maintenance"},
    "agriconnect.workers.tasks.marketplace.*": {"queue": "marketplace", "routing_key": "marketplace"},
}

# ── Rate Limits (par tâche) ─────────────────────────────────
# Format : "X/s", "X/m", "X/h"
TASK_ANNOTATIONS = {
    # IA : max 20 requêtes/min (protège le quota Groq/Azure)
    "backend.workers.tasks.ai.generate_response": {
        "rate_limit": "20/m" if IS_PRODUCTION else "60/m",
    },
    "agriconnect.workers.tasks.marketplace.process_pending_actions": {
        "rate_limit": "30/m" if IS_PRODUCTION else "120/m",
    },
    # WhatsApp : respecte les limites Twilio (1 msg/sec)
    "backend.workers.tasks.whatsapp.send_message": {
        "rate_limit": "1/s",
    },
    # TTS : max 10/min (protège quota Azure Speech)
    "backend.workers.tasks.voice.generate_tts": {
        "rate_limit": "10/m" if IS_PRODUCTION else "30/m",
    },
}

# ── Time Limits ─────────────────────────────────────────────
# Différenciés par type de tâche (configurés dans la tâche elle-même)
TIME_LIMITS = {
    "ai": {"soft": 180, "hard": 240},          # 3 min soft, 4 min hard
    "marketplace": {"soft": 120, "hard": 180}, # traitement actions matching
    "voice": {"soft": 60, "hard": 90},          # 1 min soft, 1.5 min hard
    "whatsapp": {"soft": 30, "hard": 45},       # 30s soft, 45s hard
    "monitoring": {"soft": 300, "hard": 360},   # 5 min soft, 6 min hard
    "maintenance": {"soft": 600, "hard": 720},  # 10 min soft, 12 min hard
}

# ── Worker Configuration (per environment) ──────────────────

if IS_PRODUCTION:
    WORKER_CONFIG = {
        # Prefetch 1 pour les tâches IA (lourdes), plus pour les légères
        "worker_prefetch_multiplier": 1,
        # Recycle workers après N tâches → évite les memory leaks
        "worker_max_tasks_per_child": 200,
        # Nombre de workers (sera overridé par -c en CLI)
        "worker_concurrency": 4,
        # Désactive le pool solo — prefork obligatoire en prod
        "worker_pool": "prefork",
        # Shutdown propre : attendre les tâches en cours
        "worker_cancel_long_running_tasks_on_connection_loss": True,
    }
elif IS_STAGING:
    WORKER_CONFIG = {
        "worker_prefetch_multiplier": 2,
        "worker_max_tasks_per_child": 500,
        "worker_concurrency": 2,
        "worker_pool": "prefork",
        "worker_cancel_long_running_tasks_on_connection_loss": True,
    }
else:
    WORKER_CONFIG = {
        "worker_prefetch_multiplier": 4,
        "worker_max_tasks_per_child": 1000,
        "worker_concurrency": 2,
        "worker_pool": "prefork",
        "worker_cancel_long_running_tasks_on_connection_loss": False,
    }


# ── Broker Transport Options (Redis robustness) ────────────
BROKER_TRANSPORT_OPTIONS = {
    # Timeout de visibilité : si un worker crash, la tâche redevient
    # disponible après ce délai (en secondes).
    "visibility_timeout": 600 if IS_PRODUCTION else 300,
    # Retry policy pour la connexion au broker
    "max_retries": 5,
    "interval_start": 0.2,
    "interval_step": 0.5,
    "interval_max": 5.0,
    # Socket timeout (détecte les déconnexions réseau)
    "socket_timeout": 15,
    "socket_connect_timeout": 10,
    # Séparation des DB Redis (broker ≠ backend ≠ cache)
    "sep": ":",
}

# ── Result Backend Options ──────────────────────────────────
RESULT_BACKEND_TRANSPORT_OPTIONS = {
    "retry_policy": {
        "max_retries": 3,
        "interval_start": 0.2,
        "interval_step": 0.5,
        "interval_max": 3.0,
    },
}


def get_celery_config() -> dict:
    """
    Retourne la configuration Celery complète, prête à être
    passée à celery_app.conf.update().
    """
    return {
        # ── Serialization (sécurité : JSON uniquement) ──
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "event_serializer": "json",

        # ── Timezone ──
        "timezone": "Africa/Ouagadougou",
        "enable_utc": True,

        # ── Queues & Routing ──
        "task_queues": TASK_QUEUES,
        "task_routes": TASK_ROUTES,
        "task_default_queue": "default",
        "task_default_exchange": "default",
        "task_default_routing_key": "default",

        # ── Rate Limits & Annotations ──
        "task_annotations": TASK_ANNOTATIONS,

        # ── Reliability ──
        # ACK en retard → si le worker crash, la tâche est re-dispatchée
        "task_acks_late": True,
        # Rejeter (et donc re-queuer) si le worker est tué (OOM, SIGKILL)
        "task_reject_on_worker_lost": True,
        # Ne pas stocker les résultats sauf si on en a besoin
        "task_ignore_result": False,
        # Expiration des résultats (2h en prod, 1h en dev)
        "result_expires": 7200 if IS_PRODUCTION else 3600,
        # Compression des messages (réduit le trafic Redis)
        "result_compression": "gzip",
        "task_compression": "gzip",

        # ── Time Limits (défauts globaux — overridés par tâche) ──
        "task_soft_time_limit": 180,
        "task_time_limit": 240,

        # ── Broker robustness ──
        "broker_transport_options": BROKER_TRANSPORT_OPTIONS,
        "broker_connection_retry_on_startup": True,
        "broker_connection_retry": True,
        "broker_connection_max_retries": 10,
        "broker_pool_limit": 20 if IS_PRODUCTION else 10,

        # ── Result backend robustness ──
        "result_backend_transport_options": RESULT_BACKEND_TRANSPORT_OPTIONS,

        # ── Worker ──
        **WORKER_CONFIG,

        # ── Task tracking (pour Flower + introspection) ──
        "task_track_started": True,
        "task_send_sent_event": True,
        "worker_send_task_events": True,

        # ── Sécurité : limiter la taille des résultats (10 MB) ──
        "result_chord_join_timeout": 60,
    }
