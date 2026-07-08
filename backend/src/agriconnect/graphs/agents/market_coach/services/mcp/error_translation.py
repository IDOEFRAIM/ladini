"""MCP Error Translation — user-friendly French error messages.

Extracted from ``nodes/executor.py``.
"""
from __future__ import annotations

from typing import Dict

GENERIC_TECHNICAL_ERROR = (
    "Une erreur technique est survenue. Veuillez réessayer dans quelques instants. "
    "Si le problème persiste, contactez le support."
)

_MCP_ERROR_TRANSLATIONS: Dict[str, str] = {
    "not_found": "L'élément demandé n'a pas été trouvé. Vérifiez les données ou reformulez.",
    "duplicate": "Cet enregistrement existe déjà. Voulez-vous le modifier plutôt ?",
    "permission": "Vous n'avez pas les droits pour cette opération.",
    "invalid": "Les données envoyées ne sont pas valides. Vérifiez et réessayez.",
    "stock": "Problème lié au stock. Vérifiez vos quantités.",
    "closed": "Cette enchère ou offre est déjà clôturée.",
}


def translate_mcp_error(raw_error: str) -> str:
    lower = (raw_error or "").lower()
    for key, msg in _MCP_ERROR_TRANSLATIONS.items():
        if key in lower:
            return msg
    return GENERIC_TECHNICAL_ERROR


__all__ = ["GENERIC_TECHNICAL_ERROR", "translate_mcp_error"]
