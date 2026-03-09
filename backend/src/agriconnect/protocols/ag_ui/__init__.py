"""
AG-UI (Agent-to-UI) — Couche de rendu universelle AgriConnect 2.0
==================================================================

PHILOSOPHIE : Les agents NE RENVOIENT PLUS du texte brut.
Ils renvoient des composants AG-UI structurés (JSON) que chaque canal
traduit dans son format natif :

  - WhatsApp  → Menus interactifs, images, messages vocaux
  - Web       → Cards, graphiques, boutons
  - SMS/USSD  → Texte condensé, menus numérotés
  - Mobile    → Composants natifs React/Flutter

GAIN : Un seul backend sert TOUS les canaux sans coder de variante.
"""

from .components import (
    ComponentType,
    Severity,
    ActionType,
    AGUIComponent,
    TextBlock,
    Card,
    ActionButton,
    ListPicker,
    FormField,
    ChartData,
    AlertBanner,
    UserApproval,
    ContextRequest,
    AgriResponse,
)
from .renderer import AGUIRenderer, WhatsAppRenderer, WebRenderer, SMSRenderer, prune_components
from .formatter import UIFormatter

# Alias rétro-compatible (ancien nom utilisé dans les agents)
AgriComponent = AGUIComponent

__all__ = [
    "ComponentType",
    "Severity",
    "ActionType",
    "AGUIComponent",
    "AgriComponent",
    "TextBlock",
    "Card",
    "ActionButton",
    "ListPicker",
    "FormField",
    "ChartData",
    "AlertBanner",
    "UserApproval",
    "ContextRequest",
    "AgriResponse",
    "AGUIRenderer",
    "WhatsAppRenderer",
    "WebRenderer",
    "SMSRenderer",
    "prune_components",
    "UIFormatter",
]
