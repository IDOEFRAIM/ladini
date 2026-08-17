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

from agriconnect.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    is_blocking_slot,
)

logger = logging.getLogger("AgriConnect.Market.TunnelManager")


# ---------------------------------------------------------------------------
# Configurable thresholds
# ---------------------------------------------------------------------------

#: Minimum LLM confidence to allow a NEW_TASK / INTERRUPTION to break a soft tunnel.
INTERRUPTION_CONFIDENCE_THRESHOLD: float = 0.60

#: Intents that always break through any tunnel regardless of confidence.
#: Dérivé du flag `breakout` d'INTENT_CONFIG — source : core/goals.py.
from agriconnect.graphs.agents.market_coach.core.goals import (  # noqa: E402
    NAVIGATION_BREAKOUT_GOALS as CRITICAL_BREAKOUT_INTENTS,
)

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
    critical_breakout_intents:
        Set of intent keys that always trigger a clean switch, ignoring the threshold.
    """

    def __init__(
        self,
        interruption_threshold: float = INTERRUPTION_CONFIDENCE_THRESHOLD,
        critical_breakout_intents: Optional[FrozenSet[str]] = None,
    ) -> None:
        self._threshold = interruption_threshold
        self._critical = critical_breakout_intents or CRITICAL_BREAKOUT_INTENTS

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
    ) -> TunnelDecision:
        """Evaluate whether to stay in the active tunnel or switch.

        Args:
            current_goal:    The goal currently being pursued.
            expected_input:  What the system currently expects from the user.
            incoming_event:  The ``interpreted_event`` produced by the interpreter.
            incoming_intent: The ``detected_intent`` produced by the interpreter.
            confidence:      LLM confidence score [0.0 – 1.0].

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

        # Critical navigation intents always break through — including hard
        # slots like CONFIRMATION. Checked BEFORE the hard-slot gate below:
        # otherwise a broken/empty confirmation recap traps the user forever,
        # since CONFIRMATION used to be blocked unconditionally regardless of
        # intent or confidence (bug fixed 2026-07-17, see
        # [[market-coach-turn-boundary-state]]).
        if intent_upper in self._critical:
            logger.info(
                "[TunnelManager] Critical intent '%s' breaks tunnel '%s'",
                intent_upper,
                current_goal,
            )
            return TunnelDecision(
                stay_in_tunnel=False, allow_interrupt=True, reason="critical_intent"
            )

        # Remaining hard slots (CONFIRMATION, or a custom blocking slot):
        # only a high-confidence INTERRUPTION/NEW_TASK can break through —
        # never silently, but never unconditionally either.
        if is_blocking_slot(exp_upper) or exp_upper in HARD_EXPECTED_INPUTS:
            if (
                event_upper in {"INTERRUPTION", "NEW_TASK"}
                and confidence >= self._threshold
            ):
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

        # INTERRUPTION event: honour if confidence meets threshold.
        if event_upper == "INTERRUPTION":
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

        # NEW_TASK during a *soft* slot: allow if confidence is sufficient.
        if event_upper == "NEW_TASK" and exp_upper in SOFT_EXPECTED_INPUTS:
            if confidence >= self._threshold:
                logger.info(
                    "[TunnelManager] NEW_TASK during soft slot '%s': intent=%s conf=%.2f",
                    exp_upper,
                    intent_upper,
                    confidence,
                )
                return TunnelDecision(
                    stay_in_tunnel=False,
                    allow_interrupt=True,
                    reason="new_task_soft_slot",
                )

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
    ) -> bool:
        """Returns True when it is safe to route to ``cart_management``.

        The DomainRouter must NOT route to the cart node when the validator
        still has blocking missing fields — that would bypass the
        response_strategy prompt and create an infinite loop.
        The only exception is when ``missing_fields`` is empty (all required
        slots satisfied) but ``status`` happens to be WAITING_INPUT because
        cart_management itself handles vendor-selection menus.

        Args:
            status: Current agent status from the state (str).
            missing_fields: List of field names reported as missing by the validator.
        """
        status_upper = str(status or "").upper()
        if status_upper == "ERROR":
            return False
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
    "CRITICAL_BREAKOUT_INTENTS",
    "SOFT_EXPECTED_INPUTS",
    "HARD_EXPECTED_INPUTS",
]
