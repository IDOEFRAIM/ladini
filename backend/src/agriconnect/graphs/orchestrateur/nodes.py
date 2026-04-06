"""Core node logic for the AgriConnect Orchestrator.

This module contains the `OrchestratorNodes` class which encapsulates
all the logic for the LangGraph nodes. It is designed to be
stateless where possible, receiving dependencies via its constructor.

This replaces the old `message_flow.py` monolithic logic.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

from langgraph.types import Send, interrupt

from agriconnect.graphs.agents.common.domains import AgentDomain
from agriconnect.graphs.state import GlobalAgriState, ExpertResponse
from agriconnect.graphs.agents.sentinelle.graph import ClimateSentinel
from agriconnect.graphs.agents.formation.graph import FormationCoach
from agriconnect.graphs.agents.market_coach.graph import MarketCoach
from agriconnect.graphs.agents.marketplace_v3.graph import MarketplaceAgentV3
from agriconnect.graphs.orchestrateur.prompts import build_routing_prompt
from agriconnect.services.persistence import AgriPersister

# Configure logger
logger = logging.getLogger(__name__)

# Expert mapping
EXPERT_MAP = {
    "sentinelle": ClimateSentinel,
    "formation": FormationCoach,
    "market": MarketCoach,
    "market_coach": MarketCoach,
    "marketplace": MarketplaceAgentV3,
    "marketplace_v3": MarketplaceAgentV3,
}

# Centralized domain routing table: agents signal a required domain,
# the orchestrator decides which concrete expert implementation handles it.
DOMAIN_EXPERT_MAP = {
    AgentDomain.MARKET.value: "market_coach",
    AgentDomain.FORMATION.value: "formation",
    AgentDomain.SENTINELLE.value: "sentinelle",
    AgentDomain.MARKETPLACE.value: "marketplace_v3",
}

MIN_CONFIDENCE = 0.7


class OrchestratorNodes:
    """Encapsulates the logic for each node in the orchestrator graph."""

    def __init__(self, llm, ctx, mcp_shield=None, mcp_session=None, router=None):
        self.llm = llm
        self.ctx = ctx
        self.mcp_shield = mcp_shield
        self.mcp_session = mcp_session
        self.router = router

    def analyze_needs(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Analyze user intent and select experts."""
        # Clean/Validate query
        query = state.get("requete_utilisateur", "") 
        
        # Expert catalog description for LLM
        expert_catalog = (
              "sentinelle: maladies/météo; market_coach: prix/tendances; "
            "formation: tutoriels; marketplace: achats"
        )

        try:
            # 1. Try structured Router if available
            if self.router:
                analysis = self.router.analyze_needs(state)
            else:
                # 2. Fallback to LLM call
                analysis = self._call_llm_routing(query, expert_catalog)

            if not isinstance(analysis, dict):
                raise ValueError("Invalid analysis response")

            intent = (analysis.get("intent") or "").upper()
            selected = analysis.get("selected_experts") or []
            confidence = float(analysis.get("confidence_score") or 0.0)

            # Confidence check
            if confidence < MIN_CONFIDENCE:
                logger.info("Routing low confidence (%.2f) — failing closed", confidence)
                return {
                    "needs": {
                        "selected_experts": [],
                        "confidence_score": confidence,
                        "intent": intent,
                        "reason": analysis.get("reason")
                    },
                    "execution_path": ["analyze"]
                }

            # Normalize intent
            if intent in ("CHAT", "REJECT"):
                return {
                    "needs": {
                        "intent": intent,
                        "selected_experts": [],
                        "confidence_score": confidence,
                        "reason": analysis.get("reason")
                    },
                    "execution_path": ["analyze"]
                }

            # Limit number of experts, even if LLM suggests more (safety check)
            if isinstance(selected, list) and len(selected) > 2:
                selected = selected[:2]

            return {
                "needs": {
                    "selected_experts": selected,
                    "confidence_score": confidence,
                    "intent": intent,
                    "reason": analysis.get("reason")
                },
                "execution_path": ["analyze"]
            }

        except Exception as e:
            logger.exception("analyze_needs failed: %s", e)
            return {
                "needs": {
                    "selected_experts": [],
                    "confidence_score": 0.0,
                    "intent": "REJECT",
                    "reason": "analyze_error"
                },
                "execution_path": ["analyze_error"]
            }

    def _call_llm_routing(self, query: str, expert_catalog: str) -> Dict[str, Any]:
        prompt = build_routing_prompt(expert_catalog)
        try:
            response = self.llm.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": query}
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            return json.loads(raw)
        except Exception:
            raise

    def route_flow(self, state: GlobalAgriState) -> Union[str, List[Send]]:
        """Determine next step: CHAT, REJECT, or Expert Fan-out (Send)."""
        needs = state.get("needs", {}) or {}
        intent = (needs.get("intent") or "").upper()

        if intent == "CHAT":
            return "EXECUTE_CHAT"
        if intent == "REJECT":
            return "REJECT"

        selected = needs.get("selected_experts", []) or []
        if not selected:
            return "REJECT"

        # Fan-out: Create a Send object for each expert
        # Use simple heuristic to designate lead (first one)
        lead = selected[0]
        sends = []
        for name in selected:
            sends.append(Send(
                "RUN_EXPERT",
                {
                    **state,
                    "_expert_target": name,
                    "_expert_is_lead": (name == lead)
                }
            ))
        return sends

    def run_expert_node(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Execute a single expert workflow."""
        name = state.get("_expert_target", "sentinelle")
        is_lead = state.get("_expert_is_lead", False)
        query = state.get("requete_utilisateur", "")
        
        # Common context
        context = {
            "zone_id": state.get("zone_id", "Bobo-Dioulasso"),
            "crop": state.get("crop", "Céréales"),
            "user_level": state.get("user_level", "debutant"),
            "user_phone": state.get("user_phone", ""),
        }
        
        # Add Trust Level
        context["trust_level"] = self._get_expert_trust_level(name)

        try:
            ExpertClass = EXPERT_MAP.get(name)
            if ExpertClass is None:
                logger.warning("Unknown expert invoked: %s", name)
                return self._make_expert_response(name, "Expert indisponible.", is_lead)

            # Instantiate through DI-aware factory when available.
            try:
                if hasattr(ExpertClass, "from_config"):
                    expert = ExpertClass.from_config(
                        mcp_shield=self.mcp_shield,
                        mcp_session=self.mcp_session,
                        llm=self.llm,
                        llm_client=self.llm,
                        ctx=self.ctx,
                    )
                else:
                    expert = ExpertClass(
                        mcp_shield=self.mcp_shield,
                        mcp_session=self.mcp_session,
                        llm=self.llm,
                        ctx=self.ctx,
                    )
            except TypeError:
                # Fallback
                expert = ExpertClass(mcp_session=self.mcp_session)

            # Invoke
            res = None
            for method in ("handle", "run", "execute", "respond", "call", "process"):
                if hasattr(expert, method):
                    res = getattr(expert, method)(query, context)
                    break
            
            if res is None:
                 res = getattr(expert, "response", None) or {"response": str(expert)}

            if isinstance(res, dict):
                requested_domain = (
                    res.get("required_domain")
                    or res.get("handoff")
                    or res.get("handoff_to")
                )
                target_expert = DOMAIN_EXPERT_MAP.get(str(requested_domain), None) if requested_domain else None
                if target_expert and target_expert != name:
                    delegated = self._run_delegate_expert(target_expert, query, context)
                    transition = "Je transfere votre demande vers l'expert le plus adapte."
                    base_txt = res.get("full_text") or res.get("response") or ""
                    delegated_txt = delegated.get("response", "")
                    combined_txt = "\n\n".join([part for part in [base_txt, transition, delegated_txt] if part])
                    return {
                        "expert_responses": [
                            ExpertResponse(
                                expert=target_expert,
                                response=combined_txt,
                                is_lead=is_lead,
                                has_alerts=delegated.get("has_alerts", False),
                            )
                        ],
                        "execution_path": [f"expert_{name}", f"handoff_{requested_domain}", f"expert_{target_expert}"],
                    }

            if isinstance(res, dict) and "full_text" in res:
                resp_text = res.get("full_text", "")
                structured_data = res.get("structured_data") or {}
                has_alerts = bool(structured_data.get("validation_warnings") or structured_data.get("alerts"))
            else:
                resp_text = res.get("response", "") if isinstance(res, dict) else str(res)
                has_alerts = res.get("has_alerts", False) if isinstance(res, dict) else False

            return {
                "expert_responses": [
                    ExpertResponse(expert=name, response=resp_text, is_lead=is_lead, has_alerts=has_alerts)
                ],
                "execution_path": [f"expert_{name}"]
            }

        except Exception as e:
            logger.error("Expert %s failed: %s", name, e)
            return self._make_expert_response(name, "", is_lead, is_error=True)

    def _run_delegate_expert(self, name: str, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Run a delegated expert chosen by domain routing table."""
        ExpertClass = EXPERT_MAP.get(name)
        if ExpertClass is None:
            return {"response": "", "has_alerts": False}
        try:
            if hasattr(ExpertClass, "from_config"):
                expert = ExpertClass.from_config(
                    mcp_shield=self.mcp_shield,
                    mcp_session=self.mcp_session,
                    llm=self.llm,
                    llm_client=self.llm,
                    ctx=self.ctx,
                )
            else:
                expert = ExpertClass(mcp_session=self.mcp_session)
        except TypeError:
            expert = ExpertClass(mcp_session=self.mcp_session)

        for method in ("handle", "run", "execute", "respond", "call", "process"):
            if hasattr(expert, method):
                res = getattr(expert, method)(query, context)
                if isinstance(res, dict) and "full_text" in res:
                    structured_data = res.get("structured_data") or {}
                    return {
                        "response": res.get("full_text", ""),
                        "has_alerts": bool(structured_data.get("validation_warnings") or structured_data.get("alerts")),
                    }
                if isinstance(res, dict):
                    return {"response": res.get("response", ""), "has_alerts": res.get("has_alerts", False)}
                return {"response": str(res), "has_alerts": False}
        return {"response": "", "has_alerts": False}

    def _make_expert_response(self, name, text, is_lead, is_error=False):
        return {
            "expert_responses": [
                ExpertResponse(expert=name, response=text, is_lead=is_lead, has_alerts=False)
            ],
            "execution_path": [f"expert_{name}_error" if is_error else f"expert_{name}"]
        }

    def _get_expert_trust_level(self, agent_name: str) -> str:
        if agent_name.lower().startswith("market") or "marketplace" in agent_name.lower():
            return "LOW"
        if agent_name.lower().startswith("formation"):
            return "LOW"
        if agent_name.lower().startswith("sentinelle"):
            return "READ_ONLY"
        return "ASK"

    def hitl_gate(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Human-in-the-Loop Gate."""
        if not state.get("requires_validation"):
            return {}

        payload = state.get("pending_action_payload") or {}
        action_id = state.get("pending_action_id", "unknown")
        
        logger.info("HITL_GATE: Suspending for action=%s", action_id)
        
        decision = interrupt({
            "action_id": action_id,
            "payload": payload,
            "message": "Cette action nécessite une validation humaine."
        })
        
        # Verify approval
        approved = decision in ("approved", "APPROVED", True)
        status = "APPROVED" if approved else "REJECTED"
        logger.info("HITL_GATE: Resumed with %s", status)

        if not approved:
            return {
                "hitl_status": "REJECTED",
                "final_response": "Action annulée par l'opérateur.",
                "execution_path": ["hitl_rejected"]
            }
        
        return {"hitl_status": "APPROVED", "execution_path": ["hitl_approved"]}

    def execute_chat(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Simple chat handler."""
        query = (state.get("requete_utilisateur") or "").strip().lower()
        greetings = ("bonjour", "salut", "bonsoir", "hello", "hi")
        
        # Check if short greeting
        if any(query.startswith(g) for g in greetings) or len(query.split()) <= 3:
            return {
                "final_response": "Bonjour ! Je suis AgriBot — comment puis-je vous aider aujourd'hui ?",
                "execution_path": ["chat_shortcircuit"]
            }
        return {
            "final_response": "Je transfère votre demande à nos experts...",
            "execution_path": ["chat_forward"]
        }

    def execute_rejection(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Handle rejection logic."""
        reason = state.get("needs", {}).get("reason") or ""
        msg = (
            "Désolé, je suis AgriBot et je ne peux répondre qu'aux questions sur l'agriculture. "
            "Comment puis-je vous aider ?"
        )
        if "arnaque" in reason.lower() or "argent" in reason.lower():
            msg = "Attention : Ne partagez jamais de codes secrets."
            
        return {
            "final_response": msg,
            "execution_path": ["rejection_node"]
        }

    def synthesize_results(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Synthesize multiple expert responses."""
        responses = state.get("expert_responses", []) or []
        if not responses:
            return {
                "final_response": "Aucun expert disponible pour le moment.",
                "execution_path": ["synthesize_fallback"]
            }

        full_text = "\n".join([f"{r['expert']}: {r['response']}" for r in responses])
        prompt = (
            "Tu es l'agronome en chef. Fusionne ces rapports d'experts en une seule réponse fluide "
            "et sans répétitions pour l'agriculteur :\n" + full_text
        )

        try:
            final_msg = self.llm.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}]
            ).choices[0].message.content
        except Exception:
            # Fallback join
            final_msg = "\n\n".join([f"{r['expert']}: {r['response']}" for r in responses])

        return {
            "final_response": final_msg,
            "execution_path": ["smart_synthesize"],
            # Usually we don't need to return expert_responses strictly as they are additive,
            # but sometimes logic needs them updated. Here we just keep them.
        }

    def generate_audio(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Placeholder for Audio Generation."""
        # Typically calling self.ctx.services.voice.synthesize(...)
        # For now disabled as per previous implementation
        logger.info("Audio disabled (dev mode).")
        return {"audio_url": None}

    def persist(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Persist state to DB."""
        if not self.ctx:
            logger.warning("No context for persistence.")
            return {}

        try:
            persister = AgriPersister(db=self.ctx.db, memory=self.ctx.memory)
            persister.save_all(state)
        except Exception as e:
            logger.error("Persistence failed: %s", e)
        return {}
