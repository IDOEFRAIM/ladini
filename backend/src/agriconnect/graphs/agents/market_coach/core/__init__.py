"""Core MarketCoach — State, graph builder et primitives de routage maître."""

from agriconnect.graphs.agents.market_coach.core.slots import (  # noqa: F401
    SLOT_REGISTRY,
    SlotDefinition,
    build_alias_mirrors,
    build_canonical_field_aliases,
    build_remap_dict,
    get_aliases,
    get_slot,
    get_slot_hint,
    is_blocking_slot,
    resolve_canonical,
)
# NOTE : pas d'import eager de tunnel_manager ici — il importe core.goals,
# qui dérive ses ensembles d'INTENT_CONFIG (interpreter/intent.py), lequel
# importe core.slots et déclenche donc ce __init__ : l'eager créerait un
# cycle intent → core/__init__ → tunnel_manager → goals → intent.
# Importer directement `agriconnect...core.tunnel_manager` (aucun appelant
# ne passe par le package).
