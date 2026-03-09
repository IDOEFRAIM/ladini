import json
import logging
from typing import Any, Dict, List

from agriconnect.core.tracing import get_tracing_config

logger = logging.getLogger(__name__)


class ExpertInvoker:
    """Small helper to host expert workflow invocations outside the main file.

    This reduces the LOC of `message_flow.py` by moving the repetitive
    tracing + wf.invoke boilerplate here.
    """

    def __init__(self, workflows: Dict[str, Any]):
        self.workflows = workflows

    def invoke_workflow(
        self,
        wf,
        run_name: str,
        tags: List[str],
        metadata: Dict[str, Any],
        inputs: Dict[str, Any],
        fallback_message: str = "Service indisponible.",
    ) -> Dict[str, Any]:
        try:
            config = get_tracing_config(run_name=run_name, tags=tags, metadata=metadata)
            return wf.invoke(inputs, config)
        except Exception as e:
            logger.warning("%s Error: %s", run_name, e)
            return {"final_response": fallback_message}

    def call_sentinelle(self, query: str, zone: str, user_level: str = "debutant") -> Dict[str, Any]:
        inputs = {
            "user_query": query,
            "user_level": user_level,
            "location_profile": {"village": zone, "zone": "Hauts-Bassins", "country": "Burkina Faso"},
        }
        return self.invoke_workflow(
            self.workflows.get("sentinelle"),
            "agent.sentinelle",
            ["sentinelle", "weather", zone],
            {"user_level": user_level, "zone": zone},
            inputs,
            "Données Sentinel indisponibles.",
        )

    def call_formation(self, query: str, crop: str, user_level: str = "debutant") -> Dict[str, Any]:
        inputs = {"user_query": query, "learner_profile": {"culture_actuelle": crop, "niveau": user_level}}
        return self.invoke_workflow(
            self.workflows.get("formation"),
            "agent.formation",
            ["formation", "pedagogy", crop],
            {"user_level": user_level, "crop": crop},
            inputs,
            "Conseils techniques indisponibles.",
        )

    def call_market(self, query: str, zone: str, user_level: str = "debutant") -> Dict[str, Any]:
        inputs = {"user_query": query, "user_level": user_level, "user_profile": {"zone": zone}}
        return self.invoke_workflow(
            self.workflows.get("market"),
            "agent.market",
            ["market", "prices", zone],
            {"user_level": user_level, "zone": zone},
            inputs,
            "Infos marché indisponibles.",
        )

    def call_marketplace(self, query: str, zone: str, phone: str = "") -> Dict[str, Any]:
        inputs = {"user_query": query, "user_phone": phone, "zone_id": zone if zone else None, "warnings": []}
        return self.invoke_workflow(
            self.workflows.get("marketplace"),
            "agent.marketplace",
            ["marketplace", "transactions", zone],
            {"zone": zone, "phone": phone},
            inputs,
            "Service marketplace indisponible.",
        )
