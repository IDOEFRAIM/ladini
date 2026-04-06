"""
Scheduling Module

Orchestration temporelle des tâches périodiques.

Modules:
- scheduler: APScheduler configuration for periodic tasks

Architecture:
- Background scheduler pour exécution asynchrone
- Cron triggers pour horaires précis
- Integration avec tous les collecteurs de données

Usage:
    from backend.services.scheduling.scheduler import start_scheduler
    
    scheduler = start_scheduler()
    # Runs in background
"""

__version__ = "1.0.0"

__all__ = [
    "scheduler"
]
