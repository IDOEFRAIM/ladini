"""Market Coach — TunnelManager.

Centralises the logic for slot-locking and intent-switching decisions.
Previously this logic was spread across ``goal_planner`` (routing.py),
``DomainRouter.route_after_validator`` (router.py) and implicitly in
``memory_update`` (nodes/memory.py).

The TunnelManager is the **single authority** that answers two questions:

1. Should we stay in the current slot-filling tunnel or allow an interruption?
2. Is it safe to route toward a transactional node (cart, negotiation…)?

Design principles
-----------------
* Pure functions — no I/O, no external state.
* A module-level singleton ``tunnel_manager`` is provided for convenience;
  all nodes import it directly.
* All thresholds and intent whitelists are explicit constants, easy to tune.
"""

from __future__ import annotations

import logging
from typing import FrozenSet, List, Optional

from ladini.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    is_blocking_slot,
)

logger = logging.getLogger("Ladini.Market.TunnelManager")


# ---------------------------------------------------------------------------
# Configurable thresholds
# ---------------------------------------------------------------------------

#: Minimum LLM confidence to allow a NEW_TASK / INTERRUPTION to break a soft tunnel.
INTERRUPTION_CONFIDENCE_THRESHOLD: float = 0.60

# (2026-09-09, Bloc 2 passe finale — Invariant A) : l'import de
# `NAVIGATION_BREAKOUT_GOALS` a été RETIRÉ de ce module. La liste reste
# définie une seule fois (`core/goals.py`, dérivée du flag `breakout`
# d'INTENT_CONFIG) et n'a plus qu'UN consommateur décisionnel :
# `nodes/cognitive.py`. La conserver ici en aurait fait une seconde copie
# consultée par une seconde autorité — exactement ce que cette passe
# élimine.

#: expected_input values where NEW_TASK can break the tunnel (soft slots).
#: Les slots-champs (PRODUCT/PRICE/QUANTITY/UNIT/LOCATION/DATE/FARM_NAME)
#: viennent du registre central (core/slots.py::SLOT_FILLING_INPUTS) —
#: source unique, plus de liste recopiée à la main qui dérive. SELECTION est
#: soft pour l'interruption mais n'est pas un « champ » à re-demander, donc
#: ajouté explicitement ici seulement.
SOFT_EXPECTED_INPUTS: FrozenSet[str] = SLOT_FILLING_INPUTS | frozenset({"SELECTION"})

#: expected_input values that are never interruptible (hard slots).
HARD_EXPECTED_INPUTS: FrozenSet[str] = frozenset(
    {
        "CONFIRMATION",
        "OTP",
        "OTP_CODE",
    }
)

#: Subset of HARD_EXPECTED_INPUTS that must NEVER be interrupted, under any
#: circumstance (payment/security codes). CONFIRMATION is deliberately
#: excluded: a user saying "non, je veux X à la place" mid-recap is normal
#: conversation, not noise — see ALWAYS_UNBREAKABLE_INPUTS usage below.
ALWAYS_UNBREAKABLE_INPUTS: FrozenSet[str] = frozenset(
    {
        "OTP",
        "OTP_CODE",
    }
)


# ---------------------------------------------------------------------------
# TunnelDecision — result object
# ---------------------------------------------------------------------------


class TunnelDecision:
    """Encapsulates the outcome of a tunnel evaluation.

    Attributes:
        stay_in_tunnel: The incoming event should be absorbed by the current tunnel.
        allow_interrupt: The current tunnel should be suspended and the new goal activated.
        reason: Short debug label for logging.
    """

    __slots__ = ("stay_in_tunnel", "allow_interrupt", "reason")

    def __init__(
        self,
        stay_in_tunnel: bool,
        allow_interrupt: bool = False,
        reason: str = "",
    ) -> None:
        self.stay_in_tunnel = stay_in_tunnel
        self.allow_interrupt = allow_interrupt
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"TunnelDecision(stay={self.stay_in_tunnel}, "
            f"interrupt={self.allow_interrupt}, reason={self.reason!r})"
        )


# ---------------------------------------------------------------------------
# TunnelManager
# ---------------------------------------------------------------------------


class TunnelManager:
    """Decides whether an ongoing slot-filling tunnel should be maintained or
    interrupted based on the incoming event, intent, and confidence score.

    Parameters
    ----------
    interruption_threshold:
        Confidence floor (0–1) to allow an interruption during a soft slot.
    """

    def __init__(
        self,
        interruption_threshold: float = INTERRUPTION_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._threshold = interruption_threshold

    # ------------------------------------------------------------------
    # Primary decision method
    # ------------------------------------------------------------------

    def evaluate(
        self,
        current_goal: Optional[str],
        expected_input: Optional[str],
        incoming_event: str,
        incoming_intent: str,
        confidence: float,
        *,
        policy_approved: bool = False,
    ) -> TunnelDecision:
        """Evaluate whether to stay in the active tunnel or switch.

        Args:
            current_goal:    The goal currently being pursued.
            expected_input:  What the system currently expects from the user.
            incoming_event:  The ``interpreted_event`` produced by the interpreter.
            incoming_intent: The ``detected_intent`` produced by the interpreter.
            confidence:      LLM confidence score [0.0 – 1.0].
            policy_approved: True quand `cognitive_guard` — PROPRIÉTAIRE
                             UNIQUE de la décision d'interruption depuis
                             2026-09-09 — a explicitement approuvé cette
                             transition (`cognitive_decision.action ==
                             INTERRUPT_ACTIVE_GOAL`). Ce nœud n'a alors plus
                             à juger la confiance : il ne lui reste qu'à
                             faire respecter les invariants STRUCTURELS
                             (OTP), c.-à-d. INTERDIRE, jamais choisir.

        Returns:
            :class:`TunnelDecision` with the recommended action.
        """
        exp_upper = str(expected_input or "").upper().strip()
        event_upper = str(incoming_event or "UNKNOWN").upper().strip()
        intent_upper = str(incoming_intent or "UNKNOWN").upper().strip()

        has_tunnel = bool(current_goal and exp_upper and exp_upper not in ("", "NONE"))

        if not has_tunnel:
            return TunnelDecision(
                stay_in_tunnel=False, allow_interrupt=False, reason="no_active_tunnel"
            )

        # Slot-filling events are always absorbed by the current tunnel.
        if event_upper in {"CONFIRM", "SELECTION", "ANSWER", "UPDATE", "REJECT"}:
            return TunnelDecision(
                stay_in_tunnel=True, allow_interrupt=False, reason="slot_event"
            )

        # Truly unbreakable slots (payment/security codes) — never
        # interruptible, not even by a critical intent or high confidence.
        if exp_upper in ALWAYS_UNBREAKABLE_INPUTS:
            return TunnelDecision(
                stay_in_tunnel=True, allow_interrupt=False, reason="always_unbreakable"
            )

        # (2026-09-09, Bloc 2 passe finale — Invariant A) : le branchement
        # « intention de navigation critique » a été RETIRÉ d'ici. Il
        # décidait, en aval et sans consulter personne, de casser un tunnel
        # que `cognitive_guard` venait éventuellement de refuser
        # d'interrompre — deux autorités sur la même question. La décision
        # vit désormais dans `nodes/cognitive.py` (seuil de confiance OU
        # breakout de navigation, `NAVIGATION_BREAKOUT_GOALS`), qui émet
        # `interpreted_event="INTERRUPTION"` + `cognitive_decision.action=
        # INTERRUPT_ACTIVE_GOAL` ; `goal_planner` relaie cette approbation
        # ici via `policy_approved=True`. Ce nœud ne CHOISIT plus aucune
        # transition : il ne fait qu'interdire (OTP) ou laisser passer.
        # Comportement utilisateur inchangé, y compris le cas du récap de
        # confirmation cassé/vide (bug 2026-07-17,
        # [[market-coach-turn-boundary-state]]) : `policy_approved` traverse
        # les slots durs exactement comme le faisait `critical_intent`.

        # Remaining hard slots (CONFIRMATION, or a custom blocking slot):
        # only a high-confidence INTERRUPTION can break through — never
        # silently, but never unconditionally either.
        #
        # (2026-09-09, audit Bloc 2 — élimination de décision dupliquée) :
        # NEW_TASK n'est PLUS un événement éligible ici (il l'était avant,
        # au même seuil que INTERRUPTION). `cognitive_guard` (Bloc 1, gelé)
        # est le SEUL propriétaire de la décision « cette intention
        # concurrente est-elle assez confiante pour interrompre ? » — voir
        # nodes/cognitive.py, seuil `_DISAMBIGUATION_CONFIDENCE_THRESHOLD`
        # (0.85). Il ne réécrit `interpreted_event` en `"INTERRUPTION"` QUE
        # s'il approuve l'interruption ; si l'événement atteint ce nœud
        # encore étiqueté `"NEW_TASK"`, c'est que `cognitive_guard` a
        # explicitement REFUSÉ d'interrompre (confiance insuffisante, ou
        # intention non distincte du goal courant). Un second seuil ICI
        # (0.60, plus bas) pouvait annuler ce refus pour toute confiance
        # dans l'intervalle [0.60, 0.85) — deux autorités contradictoires
        # décidant de la même question avec des seuils différents. Seule
        # une intention de « breakout » critique (ci-dessus, indépendante
        # de la confiance — `cognitive_guard` ne la connaît pas du tout)
        # garde le droit de casser un slot dur ou souple sans passer par
        # `cognitive_guard`.
        if is_blocking_slot(exp_upper) or exp_upper in HARD_EXPECTED_INPUTS:
            if policy_approved:
                logger.info(
                    "[TunnelManager] Hard slot '%s' interrupted: approuvé par "
                    "cognitive_guard (intent=%s)",
                    exp_upper,
                    intent_upper,
                )
                return TunnelDecision(
                    stay_in_tunnel=False,
                    allow_interrupt=True,
                    reason="policy_approved_interruption",
                )
            if event_upper == "INTERRUPTION" and confidence >= self._threshold:
                logger.info(
                    "[TunnelManager] Hard slot '%s' interrupted: intent=%s conf=%.2f >= %.2f",
                    exp_upper,
                    intent_upper,
                    confidence,
                    self._threshold,
                )
                return TunnelDecision(
                    stay_in_tunnel=False,
                    allow_interrupt=True,
                    reason="hard_slot_high_confidence",
                )
            return TunnelDecision(
                stay_in_tunnel=True, allow_interrupt=False, reason="hard_slot"
            )

        # INTERRUPTION event: honour it — `cognitive_guard` already applied
        # the confidence gate before emitting this event (see note above).
        # The threshold check here is a harmless redundant confirmation for
        # any INTERRUPTION not produced by cognitive_guard's own gate (e.g.
        # a future caller), never a second real decision in practice.
        if event_upper == "INTERRUPTION":
            if policy_approved:
                return TunnelDecision(
                    stay_in_tunnel=False,
                    allow_interrupt=True,
                    reason="policy_approved_interruption",
                )
            if confidence >= self._threshold:
                logger.info(
                    "[TunnelManager] INTERRUPTION allowed: intent=%s conf=%.2f >= %.2f",
                    intent_upper,
                    confidence,
                    self._threshold,
                )
                return TunnelDecision(
                    stay_in_tunnel=False,
                    allow_interrupt=True,
                    reason="interruption_threshold_met",
                )
            logger.debug(
                "[TunnelManager] INTERRUPTION blocked: conf=%.2f < %.2f",
                confidence,
                self._threshold,
            )
            return TunnelDecision(
                stay_in_tunnel=True,
                allow_interrupt=False,
                reason="interruption_low_confidence",
            )

        # NEW_TASK (soft or hard slot): `cognitive_guard` already declined to
        # escalate it to INTERRUPTION — stay locked. See note above the
        # hard-slot branch for the full rationale.
        # Default: stay locked in the active tunnel.
        return TunnelDecision(
            stay_in_tunnel=True, allow_interrupt=False, reason="default_lock"
        )

    # ------------------------------------------------------------------
    # Routing helpers (used by DomainRouter)
    # ------------------------------------------------------------------

    def is_cart_routeable(
        self,
        status: str,
        missing_fields: Optional[List[str]] = None,
        *,
        selection_tunnel_active: bool = False,
    ) -> bool:
        """Returns True when it is safe to route to ``cart_management``.

        The DomainRouter must NOT route to the cart node when the validator
        still has blocking missing fields — that would bypass the
        response_strategy prompt and create an infinite loop.
        The only exception is when ``missing_fields`` is empty (all required
        slots satisfied) but ``status`` happens to be WAITING_INPUT because
        cart_management itself handles vendor-selection menus.

        `selection_tunnel_active` (2026-09-02, refonte state canonique, G-1) :
        deuxième exception, structurelle celle-là. Root cause confirmée par
        audit : un menu producteur/palier ENCORE actif (dérivé à neuf de
        `vendor_selection_context`/`tier_selection_context` — voir
        `domain/selection_actions.py::build_selection_context`) fait presque
        toujours remonter des `missing_fields` (la quantité/le palier ne
        sont pas encore répondus), donc cette garde bloquait
        SYSTÉMATIQUEMENT `cart_management` — le SEUL node qui sait
        interpréter une réponse de sélection (`_execute_selection_action`) —
        pendant exactement le tour où l'utilisateur répond au menu. Le
        routeur retombait alors sur `to_strategy`, qui lit `expected_input`/
        `waiting_for_confirmation` SANS savoir qu'un menu est actif : un
        signal `CONFIRMATION` périmé d'un tour précédent gagnait, produisant
        "Que souhaitez-vous confirmer exactement ?" en réponse à "celui de
        10 l". Un tunnel de sélection actif doit TOUJOURS atteindre
        `cart_management`, quels que soient les champs manquants — c'est
        exactement le node conçu pour ce cas, pas response_strategy.

        Args:
            status: Current agent status from the state (str).
            missing_fields: List of field names reported as missing by the validator.
            selection_tunnel_active: True si `build_selection_context(state)
                ).expected_action` est non-None ce tour — calculé par
                l'appelant (`core/router.py::_cart_guard`), ce module reste
                sans dépendance vers `domain/selection_actions.py`.
        """
        status_upper = str(status or "").upper()
        if status_upper == "ERROR":
            return False
        if selection_tunnel_active:
            return True
        if status_upper == "WAITING_INPUT":
            # Only allow if there are genuinely no missing required fields
            # (cart handles vendor-selection internally).
            if missing_fields:
                return False
        return True

    def is_negotiation_routeable(self, status: str) -> bool:
        """Returns True when it is safe to route to ``negotiation_gate``."""
        return str(status or "").upper() not in {"ERROR", "WAITING_INPUT"}

    # ------------------------------------------------------------------
    # Prompt helper (used by response_strategy / final_response)
    # ------------------------------------------------------------------

    def build_drift_hint(
        self,
        current_goal: Optional[str],
        detected_intent: Optional[str],
        confidence: float,
        lang: str = "fr",
    ) -> Optional[str]:
        """Returns a gentle suggestion when an UNKNOWN turns up during a slot
        but looks like a new intent.  Returns None if no hint is warranted.

        This is the "intent drift coach": rather than silently re-asking the
        same slot question, the agent acknowledges the drift and offers an out.
        """
        if not detected_intent or str(detected_intent).upper() in ("", "UNKNOWN"):
            return None
        if confidence < 0.50:
            return None
        if lang == "fr":
            return (
                f"_(Je vois que vous souhaitez peut-être **{detected_intent.lower().replace('_', ' ')}** "
                f"— répondez *oui* pour switcher ou continuez votre réponse actuelle.)_"
            )
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Shared TunnelManager instance — import and use directly in all nodes.
tunnel_manager: TunnelManager = TunnelManager()


__all__ = [
    "TunnelDecision",
    "TunnelManager",
    "tunnel_manager",
    "INTERRUPTION_CONFIDENCE_THRESHOLD",
    "SOFT_EXPECTED_INPUTS",
    "HARD_EXPECTED_INPUTS",
]
