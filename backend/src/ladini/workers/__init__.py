"""Orchestration proactive — crons, automation métier, outbox de notification.

* ``crons/``        — tâches Celery FINES planifiées par Celery Beat.
* ``automation/``   — LOGIQUE MÉTIER PURE (aucun appel externe, DB uniquement).
* ``outbox/``       — LIVRAISON découplée (WhatsApp réel, Email/Push en interface).
* ``repositories/`` — accès DB typé (solicitations / notification_outbox).

Idempotence : upsert ``ON CONFLICT DO NOTHING`` + ``dedupe_key`` unique +
claim ``FOR UPDATE SKIP LOCKED``.
"""
