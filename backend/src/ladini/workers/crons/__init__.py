"""Tâches Celery FINES planifiées par Celery Beat.

Chaque cron ne fait qu'ouvrir une session, déléguer à un service métier, et
retourner un rapport sérialisable (monitoring). Aucune logique ici.
"""
