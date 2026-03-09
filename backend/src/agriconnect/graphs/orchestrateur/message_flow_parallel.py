"""Parallel executor helper for MessageResponseFlow.

Encapsulates threadpool execution and task composition for experts.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List
from dataclasses import dataclass

from agriconnect.graphs.orchestrateur.message_flow_helpers import ExpertInvoker
from agriconnect.graphs.state import ExpertResponse
import logging

logger = logging.getLogger(__name__)

@dataclass
class RequestContext:
    query: str
    zone: str
    crop: str
    user_level: str


class ParallelExecutor:
    def __init__(self, invoker: ExpertInvoker):
        self.invoker = invoker

    def run(
        self,
        needs: Dict[str, bool],
        ctx: RequestContext,
    ) -> List[ExpertResponse]:
        """Run requested experts in parallel and return ExpertResponse list."""
        lead = self._determine_lead(needs)
        tasks = self._build_tasks(needs, ctx)

        if not tasks:
            tasks["sentinelle"] = lambda: self.invoker.call_sentinelle(ctx.query, ctx.zone, ctx.user_level)

        expert_responses: List[ExpertResponse] = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            future_to_name = {executor.submit(fn): name for name, fn in tasks.items()}
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    result = future.result(timeout=30)
                    resp_text = result.get("final_response", "")
                    has_alerts = bool(result.get("hazards"))
                    expert_responses.append(
                        ExpertResponse(
                            expert=name,
                            response=resp_text,
                            is_lead=(name == lead),
                            has_alerts=has_alerts,
                        )
                    )
                except Exception as e:
                    logger.warning("Expert %s failed: %s", name, e)
                    expert_responses.append(
                        ExpertResponse(
                            expert=name,
                            response=f"[{name}] Données indisponibles.",
                            is_lead=(name == lead),
                            has_alerts=False,
                        )
                    )

        return expert_responses

    def _determine_lead(self, needs: Dict[str, bool]) -> str:
        if needs.get("needs_formation"):
            return "formation"
        if needs.get("needs_market"):
            return "market"
        return "sentinelle"

    def _build_tasks(self, needs: Dict[str, bool], ctx: RequestContext) -> Dict[str, Any]:
        tasks: Dict[str, Any] = {}
        if needs.get("needs_sentinelle", True):
            tasks["sentinelle"] = lambda: self.invoker.call_sentinelle(ctx.query, ctx.zone, ctx.user_level)
        if needs.get("needs_formation"):
            tasks["formation"] = lambda: self.invoker.call_formation(ctx.query, ctx.crop, ctx.user_level)
        if needs.get("needs_market"):
            tasks["market"] = lambda: self.invoker.call_market(ctx.query, ctx.zone, ctx.user_level)
        return tasks
