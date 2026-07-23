#!/usr/bin/env python
"""Command-line audit for MarketCoach action registry integrity."""
from __future__ import annotations

import argparse
from typing import List

from agriconnect.graphs.agents.market_coach.registry import (
    iter_actions,
    load_all_actions,
    validate_integrity,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
#
#si apres avoir lister les produit

def main() -> None:
    parser = argparse.ArgumentParser(description="Audit MarketCoach action registry")
    parser.parse_args()

    load_all_actions()

    intent_keys = {key.upper(): cfg for key, cfg in INTENT_CONFIG.items()}
    registry = {intent: registration for intent, registration in iter_actions()}
    flow_only = {intent for intent, cfg in intent_keys.items() if cfg.get("handled_by_flow")}

    missing = sorted((set(intent_keys) - flow_only) - set(registry))
    orphan = sorted(set(registry) - set(intent_keys))

    rows: List[str] = []
    header = f"{'Intent':<35} | {'Mode':<5} | Status"
    rows.append(header)
    rows.append("-" * len(header))

    for intent, cfg in sorted(intent_keys.items()):
        registration = registry.get(intent)
        mode = (cfg.get("action_type") or "").upper()
        if intent in flow_only:
            status = "FLOW"
        else:
            status = "OK" if registration else "MISSING"
        rows.append(f"{intent:<35} | {mode:<5} | {status}")

    for intent in orphan:
        rows.append(f"{intent:<35} | {'?':<5} | ORPHAN")

    print("\n".join(rows))

    if missing or orphan:
        print("\nIntegrity issues detected. See statuses above.")
    else:
        print("\nAll actions registered correctly.")

    validate_integrity()


if __name__ == "__main__":
    main()
