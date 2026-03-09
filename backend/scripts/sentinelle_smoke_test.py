#!/usr/bin/env python3
import logging
import json

from agriconnect.graphs.nodes.sentinelle import ClimateSentinel


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    agent = ClimateSentinel()
    workflow = agent.build()
    # Exemple d'état initial
    state = {
        "user_query": "Y a-t-il un risque alimentaire presentement au burkina ?",
        "location_profile": {
            "village": "Bobo-Dioulasso",
            "zone": "Hauts-Bassins",
            "country": "Burkina Faso",
        },
    }
    result = workflow.invoke(state)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
