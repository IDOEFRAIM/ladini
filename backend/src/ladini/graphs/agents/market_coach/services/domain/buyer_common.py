"""Buyer domain shared helpers (support footer)."""
from __future__ import annotations

from typing import Optional

SUPPORT_FOOTER = (
    "🙏 Toute l'équipe Ladini s'excuse pour la gêne occasionnée.\n"
    "📞 Contactez-nous au +226 68 81 52 99 pour une assistance immédiate."
)


def with_support_footer(message: Optional[str]) -> Optional[str]:
    if not isinstance(message, str) or not message.strip():
        return SUPPORT_FOOTER
    if SUPPORT_FOOTER in message:
        return message
    return f"{message.rstrip()}\n\n{SUPPORT_FOOTER}"


__all__ = ["SUPPORT_FOOTER", "with_support_footer"]
