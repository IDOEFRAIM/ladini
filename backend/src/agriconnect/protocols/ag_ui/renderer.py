"""
AG-UI Renderer 2.0 — Capability-Negotiated Multi-Channel Rendering.
=====================================================================

Upgrades from v1:
  - Renderers accept ``ClientCapabilities`` manifest
  - Components are pruned BEFORE rendering based on channel constraints
  - Trace recording for rendering decisions (what was pruned + why)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from agriconnect.protocols.core import (
    ClientCapabilities,
    TraceCategory,
    TraceEnvelope,
)

from .components import (
    ActionButton,
    AgriResponse,
    AGUIComponent,
    AlertBanner,
    Card,
    ChartData,
    ComponentType,
    FormField,
    ListPicker,
    Severity,
    TextBlock,
)

logger = logging.getLogger("AG-UI.Renderer")


# ═══════════════════════════════════════════════════════════════
# COMPONENT PRUNER
# ═══════════════════════════════════════════════════════════════


def prune_components(
    components: List[AGUIComponent],
    caps: ClientCapabilities,
) -> tuple[List[AGUIComponent], List[Dict[str, str]]]:
    """
    Remove or trim components that exceed channel capabilities.
    Returns (kept_components, pruning_log).
    """
    kept: List[AGUIComponent] = []
    pruned_log: List[Dict[str, str]] = []

    for comp in components:
        # Charts not supported
        if comp.type == ComponentType.CHART and not caps.supports_charts:
            pruned_log.append(
                {"type": "chart", "reason": f"{caps.channel} does not support charts"}
            )
            continue

        # Cards with images on non-image channels
        if isinstance(comp, Card) and comp.image_url and not caps.supports_images:
            comp = comp.clone(image_url="")
            pruned_log.append(
                {
                    "type": "card_image",
                    "reason": f"{caps.channel} does not support images",
                }
            )

        # Too many buttons
        if isinstance(comp, Card) and comp.actions and caps.max_buttons > 0:
            if len(comp.actions) > caps.max_buttons:
                pruned_log.append(
                    {
                        "type": "card_buttons",
                        "reason": f"Trimmed {len(comp.actions)} → {caps.max_buttons} buttons",
                    }
                )
                comp = comp.clone(actions=comp.actions[: caps.max_buttons])

        # List picker items
        if isinstance(comp, ListPicker) and caps.max_list_items > 0:
            if len(comp.items) > caps.max_list_items:
                pruned_log.append(
                    {
                        "type": "list_items",
                        "reason": f"Trimmed {len(comp.items)} → {caps.max_list_items} items",
                    }
                )
                comp = comp.clone(items=comp.items[: caps.max_list_items])

        # Interactive components on non-interactive channels
        if comp.type in (
            ComponentType.ACTION,
            ComponentType.LIST_PICKER,
            ComponentType.USER_APPROVAL,
        ):
            if not caps.supports_interactive:
                pruned_log.append(
                    {
                        "type": str(comp.type.value),
                        "reason": f"{caps.channel} non-interactive",
                    }
                )
                continue

        kept.append(comp)

    return kept, pruned_log


# ═══════════════════════════════════════════════════════════════
# BASE RENDERER
# ═══════════════════════════════════════════════════════════════


class AGUIRenderer(ABC):
    """Interface de base pour la transformation multi-canal."""

    @abstractmethod
    def render(
        self,
        response: AgriResponse,
        caps: Optional[ClientCapabilities] = None,
        trace_envelope: Optional[TraceEnvelope] = None,
    ) -> Any: ...

    @abstractmethod
    def render_component(self, component: AGUIComponent) -> Any: ...

    def _apply_capabilities(
        self,
        response: AgriResponse,
        caps: Optional[ClientCapabilities],
        trace_envelope: Optional[TraceEnvelope] = None,
    ) -> List[AGUIComponent]:
        """Prune components based on capabilities, record trace."""
        if not caps:
            return response.components

        t0 = time.monotonic()
        kept, pruned_log = prune_components(response.components, caps)

        if trace_envelope:
            trace_envelope.record(
                TraceCategory.RENDERING,
                type(self).__name__,
                "capability_negotiation",
                input_summary={
                    "channel": caps.channel,
                    "total_components": len(response.components),
                },
                output_summary={
                    "kept": len(kept),
                    "pruned": len(pruned_log),
                    "pruning_details": pruned_log,
                },
                reasoning=(
                    f"Pruned {len(pruned_log)} components for {caps.channel} "
                    f"(kept {len(kept)}/{len(response.components)})"
                ),
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        return kept


# ═══════════════════════════════════════════════════════════════
# WHATSAPP RENDERER (Optimisé pour Twilio/Meta API)
# ═══════════════════════════════════════════════════════════════


class WhatsAppRenderer(AGUIRenderer):
    """
    Mapping vers WhatsApp Business :
    - Gère les limites de caractères (20 pour boutons, 24 pour listes).
    - Supporte les médias et les emojis de sévérité.
    """

    SEVERITY_EMOJI = {
        Severity.INFO: "ℹ️",
        Severity.WARNING: "⚠️",
        Severity.HIGH: "🔴",
        Severity.CRITICAL: "🚨",
    }

    def render(
        self,
        response: AgriResponse,
        caps: Optional[ClientCapabilities] = None,
        trace_envelope: Optional[TraceEnvelope] = None,
    ) -> Dict[str, Any]:
        caps = caps or ClientCapabilities.whatsapp()
        components = self._apply_capabilities(response, caps, trace_envelope)
        messages = []
        for component in components:
            try:
                rendered = self.render_component(component)
                if rendered:
                    messages.append(rendered)
            except Exception as e:
                logger.error(f"Erreur rendu WhatsApp pour {component.type}: {e}")

        return {
            "channel": "whatsapp",
            "messages": messages,
            "voice_summary": response.voice_summary,
            "metadata": response.metadata,
            "fallback_text": response.raw_text or self._build_fallback(response),
        }

    def render_component(self, component: AGUIComponent) -> Optional[Dict[str, Any]]:
        handlers = {
            ComponentType.TEXT: self._render_text,
            ComponentType.CARD: self._render_card,
            ComponentType.ACTION: self._render_action,
            ComponentType.LIST_PICKER: self._render_list_picker,
            ComponentType.FORM_FIELD: self._render_form_field,
            ComponentType.ALERT: self._render_alert,
            ComponentType.CHART: self._render_chart,
        }
        handler = handlers.get(component.type)
        return handler(component) if handler else None

    def _render_text(self, block: TextBlock) -> Dict:
        return {"type": "text", "body": block.content}

    def _render_card(self, card: Card) -> Dict:
        # Construction du corps du message
        emoji = self.SEVERITY_EMOJI.get(card.severity, "📋") if card.severity else "📋"
        header = f"{emoji} *{card.title.upper()}*"

        body_parts = [header]
        if card.subtitle:
            body_parts.append(f"_{card.subtitle}_")
        if card.body:
            body_parts.append(f"\n{card.body}")

        for f in card.fields:
            body_parts.append(f"• *{f.get('label')}*: {f.get('value')}")

        result = {"type": "text", "body": "\n".join(body_parts)}
        if card.image_url:
            result["media_url"] = card.image_url

        # Boutons interactifs (Contrainte WhatsApp : max 3)
        if card.actions:
            result["type"] = "interactive_buttons"
            result["buttons"] = [
                {"id": a.id or f"btn_{i}", "title": a.label[:20]}
                for i, a in enumerate(card.actions[:3])
            ]
        return result

    def _render_list_picker(self, picker: ListPicker) -> Dict:
        # Liste interactive (Contrainte WhatsApp : max 10 items)
        rows = [
            {
                "id": item.get("id", str(i)),
                "title": item.get("label", "")[:24],
                "description": item.get("description", "")[:72],
            }
            for i, item in enumerate(picker.items[:10])
        ]
        return {
            "type": "interactive_list",
            "header": picker.title[:60] if picker.title else None,
            "body": "Sélectionnez une option dans la liste ci-dessous :",
            "button_text": "Choisir",
            "sections": [{"title": "Options disponibles", "rows": rows}],
        }

    def _render_alert(self, alert: AlertBanner) -> Dict:
        emoji = self.SEVERITY_EMOJI.get(alert.severity, "⚠️")
        body = f"{emoji} *ALERTE {alert.severity.value.upper()}*\n\n*{alert.title}*\n{alert.message}"
        if alert.zone:
            body += f"\n\n📍 *Zone:* {alert.zone.upper()}"
        return {"type": "text", "body": body}

    def _render_action(self, action: ActionButton) -> Dict:
        return {
            "type": "interactive_buttons",
            "body": f"Action requise : *{action.label}*",
            "buttons": [{"id": action.id or "action_1", "title": action.label[:20]}],
        }

    def _render_form_field(self, field: FormField) -> Dict:
        return {
            "type": "text",
            "body": f"❓ *{field.label}*\n_{field.placeholder}_"
            if field.placeholder
            else f"❓ *{field.label}*",
        }

    def _render_chart(self, chart: ChartData) -> Dict:
        return {
            "type": "media_pending",
            "body": f"📊 *Génération du graphique : {chart.title}*",
            "meta": chart.to_dict(),  # Utilise le to_dict sécurisé récursif
        }

    def _build_fallback(self, response: AgriResponse) -> str:
        return response.voice_summary or "Nouveau message d'AgriConnect."


# ═══════════════════════════════════════════════════════════════
# WEB RENDERER (Full JSON pour SPA)
# ═══════════════════════════════════════════════════════════════


class WebRenderer(AGUIRenderer):
    """Passe le dictionnaire structuré au Frontend."""

    def render(
        self,
        response: AgriResponse,
        caps: Optional[ClientCapabilities] = None,
        trace_envelope: Optional[TraceEnvelope] = None,
    ) -> Dict[str, Any]:
        caps = caps or ClientCapabilities.web()
        self._apply_capabilities(response, caps, trace_envelope)
        return response.to_dict()

    def render_component(self, component: AGUIComponent) -> Dict[str, Any]:
        return component.to_dict()


# ═══════════════════════════════════════════════════════════════
# SMS RENDERER (Texte ultra-condensé)
# ═══════════════════════════════════════════════════════════════


class SMSRenderer(AGUIRenderer):
    """Optimisé pour les réseaux à faible bande passante (SMS/USSD)."""

    MAX_LEN = 160

    def render(
        self,
        response: AgriResponse,
        caps: Optional[ClientCapabilities] = None,
        trace_envelope: Optional[TraceEnvelope] = None,
    ) -> Dict[str, Any]:
        caps = caps or ClientCapabilities.sms()
        components = self._apply_capabilities(response, caps, trace_envelope)
        lines = []
        for c in components:
            text = self.render_component(c)
            if text:
                lines.append(text)

        full_body = "\n".join(lines)
        return {
            "channel": "sms",
            "segments": self._split_text(full_body),
            "total_chars": len(full_body),
        }

    def render_component(self, component: AGUIComponent) -> str:
        if isinstance(component, TextBlock):
            return component.voice_text or component.content
        if isinstance(component, AlertBanner):
            return f"ALERTE {component.severity.value}: {component.title}"
        if isinstance(component, Card):
            return f"{component.title}: {component.body[:50]}..."
        if isinstance(component, ListPicker):
            opts = ", ".join(
                [
                    f"{i + 1}-{item['label']}"
                    for i, item in enumerate(component.items[:3])
                ]
            )
            return f"{component.title}: {opts}"
        return ""

    def _split_text(self, text: str) -> List[str]:
        return [text[i : i + self.MAX_LEN] for i in range(0, len(text), self.MAX_LEN)]
