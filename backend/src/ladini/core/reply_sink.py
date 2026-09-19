"""Collecteur de réponses texte — pour les canaux SYNCHRONES (webchat).

Incident 2026-09-19 : le pipeline photo produit (`workers/media/
product_photo_task.py`) répond EXCLUSIVEMENT par WhatsApp
(`api/tasks.py::send_confirmation_text`). Sur le webchat (réponse HTTP,
agent exécuté dans le process API), la photo était donc soit impossible à
envoyer, soit traitée comme du texte par le LLM (« je ne peux pas voir les
photos »), et la confirmation partait sur WhatsApp au lieu du chat.

Un `ContextVar` (jamais un monkeypatch global : plusieurs requêtes webchat
tournent en parallèle dans la même boucle) redirige les envois de la tâche
courante vers une liste, que le endpoint renvoie dans sa réponse HTTP."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, List, Optional

_sink: ContextVar[Optional[List[str]]] = ContextVar("ladini_reply_sink", default=None)


def active_sink() -> Optional[List[str]]:
    """Liste de collecte de la tâche courante, ou None (envoi WhatsApp normal)."""
    return _sink.get()


@contextmanager
def collect_replies() -> Iterator[List[str]]:
    sink: List[str] = []
    token = _sink.set(sink)
    try:
        yield sink
    finally:
        _sink.reset(token)
