"""Buyer Domain Context Objects.

Encapsulates the mutable, domain-specific state blobs that previously lived
as raw dicts in ``vendor_selection_context``, ``preorder_workflow``, and
``negotiation_context``.

Design principles
-----------------
* Each context object is a thin wrapper around a plain ``Dict[str, Any]``.
  It never owns the dict — it reads from / writes to an external state ref,
  so LangGraph reducers keep working unchanged.
* Mutation is done through explicit methods (e.g. ``apply_selection``,
  ``advance_phase``) rather than ad-hoc dict writes scattered across flow
  modules, eliminating duplicated logic.
* ``from_state`` class-methods make extraction from the LangGraph state
  a one-liner; ``to_patch`` produces a clean state patch dict.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("AgriConnect.Market.BuyerContexts")


# ---------------------------------------------------------------------------
# Sentinel for a non-existent / reset context
# ---------------------------------------------------------------------------
_RESET = {"__reset__": True}


def _is_reset(ctx: Optional[Dict[str, Any]]) -> bool:
    return not ctx or bool(ctx.get("__reset__"))


# ---------------------------------------------------------------------------
# VendorSelectionState
# ---------------------------------------------------------------------------

class VendorSelectionState:
    """Manages the multi-vendor disambiguation step in the cart flow.

    When ``cart_management`` resolves multiple vendors for a product, it stores
    the list in ``vendor_selection_context``.  The user replies with an index;
    ``apply_selection`` maps that integer to the correct vendor dict and marks
    the context as resolved.

    Attributes:
        product_name: Product being sought.
        vendors: Ordered list of vendor dicts from the catalog search.
        selected_vendor: The chosen vendor dict after ``apply_selection``.
        mapping_kind: Always "product_vendor" — used by memory_update.
    """

    MAPPING_KIND = "product_vendor"

    def __init__(self, raw: Dict[str, Any]) -> None:
        self._raw = raw

    # -- Factory --

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "VendorSelectionState":
        """Extract from a LangGraph state dict."""
        ctx = state.get("vendor_selection_context") or {}
        return cls(ctx if not _is_reset(ctx) else {})

    @classmethod
    def empty(cls) -> "VendorSelectionState":
        return cls({})

    # -- Queries --

    @property
    def is_active(self) -> bool:
        """True when a vendor menu is pending selection."""
        return bool(
            self._raw
            and not _is_reset(self._raw)
            and self._raw.get("vendors")
            and not self._raw.get("selected_vendor")
        )

    @property
    def product_name(self) -> Optional[str]:
        return self._raw.get("product")

    @property
    def vendors(self) -> List[Dict[str, Any]]:
        return list(self._raw.get("vendors") or [])

    @property
    def selected_vendor(self) -> Optional[Dict[str, Any]]:
        return self._raw.get("selected_vendor")

    # -- Mutations --

    def apply_selection(self, index: int) -> Optional[Dict[str, Any]]:
        """Resolve an integer selection (1-based) to a vendor dict.

        Returns the vendor dict if found, else None (leaving state unchanged).
        """
        vendors = self.vendors
        idx = int(index)
        if not (1 <= idx <= len(vendors)):
            logger.warning(
                "[VendorSelectionState] index=%d out of range (vendors=%d)",
                idx, len(vendors),
            )
            return None
        vendor = vendors[idx - 1]
        self._raw = dict(self._raw)
        self._raw["selected_vendor"] = vendor
        logger.info(
            "[VendorSelectionState] Vendor selected: %s (index=%d)",
            vendor.get("vendor_name"), idx,
        )
        return vendor

    def reset(self) -> None:
        self._raw = {}

    def to_patch(self) -> Dict[str, Any]:
        """Produce a state patch dict suitable for LangGraph merging."""
        if not self._raw:
            return {"vendor_selection_context": _RESET}
        return {"vendor_selection_context": dict(self._raw)}

    def to_vendor_context_dict(
        self,
        product_name: str,
        vendors: List[Dict[str, Any]],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the initial context dict from a vendor list."""
        ctx: Dict[str, Any] = {
            "product_name": product_name,
            "vendors": vendors,
            "available_mapping_kind": self.MAPPING_KIND,
        }
        if extra:
            ctx.update(extra)
        self._raw = ctx
        return ctx


# ---------------------------------------------------------------------------
# PreorderPhase
# ---------------------------------------------------------------------------

class PreorderPhase:
    """Typed wrapper for the ``preorder_workflow`` state blob.

    Phases (ordered):
        CART             → User is adding items to the cart.
        PREORDER_DRAFTED → Brouillon créé, en attente de confirmation.
        CONFIRMED        → Précommande validée.
    """

    PHASES = ("CART", "PREORDER_DRAFTED", "CONFIRMED")

    def __init__(self, raw: Dict[str, Any]) -> None:
        self._raw = raw

    # -- Factory --

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "PreorderPhase":
        wf = state.get("preorder_workflow") or {}
        return cls(wf if not _is_reset(wf) else {"phase": "CART"})

    @classmethod
    def initial(cls) -> "PreorderPhase":
        return cls({"phase": "CART"})

    # -- Queries --

    @property
    def phase(self) -> str:
        return str(self._raw.get("phase") or "CART").upper()

    @property
    def is_cart(self) -> bool:
        return self.phase == "CART"

    @property
    def is_drafted(self) -> bool:
        return self.phase == "PREORDER_DRAFTED"

    @property
    def is_confirmed(self) -> bool:
        return self.phase == "CONFIRMED"

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    # -- Mutations --

    def advance_to(self, phase: str) -> "PreorderPhase":
        """Move to a new phase; returns self for chaining."""
        phase_upper = str(phase).upper()
        if phase_upper not in self.PHASES:
            logger.warning("[PreorderPhase] Unknown phase: %s", phase_upper)
        self._raw = dict(self._raw)
        self._raw["phase"] = phase_upper
        return self

    def set(self, key: str, value: Any) -> "PreorderPhase":
        self._raw = dict(self._raw)
        self._raw[key] = value
        return self

    def to_patch(self) -> Dict[str, Any]:
        return {"preorder_workflow": dict(self._raw)}


# ---------------------------------------------------------------------------
# NegotiationContext
# ---------------------------------------------------------------------------

class NegotiationContext:
    """Typed wrapper for the ``negotiation_context`` state blob.

    Exposes read helpers and mutation guards so negotiation_gate never writes
    raw dicts directly to shared state, preventing the in-place mutation bugs
    found previously.
    """

    def __init__(self, raw: Dict[str, Any]) -> None:
        self._raw = dict(raw) if raw else {}

    # -- Factory --

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "NegotiationContext":
        ctx = state.get("negotiation_context") or {}
        return cls(ctx if not _is_reset(ctx) else {})

    @classmethod
    def empty(cls) -> "NegotiationContext":
        return cls({})

    # -- Queries --

    @property
    def is_active(self) -> bool:
        return bool(self._raw and not _is_reset(self._raw))

    @property
    def session_id(self) -> Optional[str]:
        return self._raw.get("session_id") or self._raw.get("negotiation_id")

    @property
    def auction_id(self) -> Optional[str]:
        return self._raw.get("auction_id")

    @property
    def product_id(self) -> Optional[str]:
        return self._raw.get("product_id")

    @property
    def buyer_offer(self) -> Optional[float]:
        v = self._raw.get("buyer_offer")
        return float(v) if v is not None else None

    @property
    def seller_minimum(self) -> Optional[float]:
        v = self._raw.get("seller_minimum")
        return float(v) if v is not None else None

    @property
    def status(self) -> str:
        return str(self._raw.get("status") or "PENDING").upper()

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    # -- Mutations --

    def update(self, data: Dict[str, Any]) -> "NegotiationContext":
        """Non-mutating merge — returns self for chaining."""
        merged = dict(self._raw)
        merged.update(data)
        self._raw = merged
        return self

    def reset(self) -> "NegotiationContext":
        self._raw = {}
        return self

    def to_patch(self) -> Dict[str, Any]:
        if not self._raw:
            return {"negotiation_context": _RESET}
        return {"negotiation_context": dict(self._raw)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "VendorSelectionState",
    "PreorderPhase",
    "NegotiationContext",
]
