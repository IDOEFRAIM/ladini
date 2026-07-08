"""Core MarketCoach — State, graph builder et primitives de routage maître."""

from agriconnect.graphs.agents.market_coach.core.slots import (  # noqa: F401
    SlotDefinition,
    SLOT_REGISTRY,
    resolve_canonical,
    get_aliases,
    get_slot,
    is_blocking_slot,
    get_slot_hint,
    build_remap_dict,
    build_alias_mirrors,
    build_canonical_field_aliases,
)
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import (  # noqa: F401
    TunnelDecision,
    TunnelManager,
    tunnel_manager,
)
