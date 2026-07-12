"""Orchestration proactive — crons, automation métier, outbox de notification.

Architecture (3 étages découplés) :

* ``crons/``        — tâches Celery FINES planifiées par Celery Beat.
* ``automation/``   — LOGIQUE MÉTIER PURE (aucun appel externe, DB uniquement) :
                      qui solliciter → écrit ``solicitations`` + ``notification_outbox``.
* ``outbox/``       — LIVRAISON découplée : un worker lit l'outbox et envoie via
                      des canaux (WhatsApp réel, Email/Push en interface).
* ``repositories/`` — accès DB typé aux tables ``solicitations`` / ``notification_outbox``.

Idempotence : upsert ``ON CONFLICT DO NOTHING`` sur les index uniques +
``dedupe_key`` unique sur l'outbox + claim ``FOR UPDATE SKIP LOCKED``.
"""
