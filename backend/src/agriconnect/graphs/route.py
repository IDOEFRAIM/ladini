"""
Routing   to make routing logic
testable and independent from the orchestrator class.

Provides:
- analyze_needs(state, llm, ctx): returns {'needs': ..., 'execution_path': [...]}
- select_experts(user_input, manifests, limit=2)
- route_flow(state)

This module is intentionally functional (no side-effects) and depends only on
passed parameters and lightweight imports so it can be unit-tested easily.
"""
import json
import re
import logging
from typing import Any, Dict, List

from agriconnect.core.settings import settings

logger = logging.getLogger(__name__)
# NOTE: scoring/selection helper implementations were moved into `Router`
# to avoid duplication. Module-level wrapper functions are defined
# after the `Router` class and delegate to a default Router instance.


class Router:
    """Class-based router encapsulating routing heuristics.

    Methods mirror the module-level API but allow stateful instances
    (e.g., holding a default `llm` or `ctx`). Module-level functions
    remain for backward compatibility and delegate to a default
    `Router` instance created at module import time.
    """

    def __init__(self, llm=None, ctx=None):
        self.llm = llm
        self.ctx = ctx

    # --- Scoring helpers as instance methods ---
    def _score_intents(self, intents: List[str], text: str) -> int:
        return sum(2 for intent in intents if intent.replace("_", " ") in text or intent in text)

    def _score_capabilities(self, capabilities: List[str], text: str) -> int:
        return sum(1 for cap in capabilities if cap in text)

    def _score_performance(self, avg_response_ms: int) -> int:
        avg = avg_response_ms or 1000
        return max(0, 5 - (avg // 200))

    def _score_manifest(self, manifest: Dict[str, Any], text: str) -> int:
        intents = [i.lower() for i in manifest.get("intents", [])]
        caps = [c.lower() for c in manifest.get("capabilities", [])]
        avg_response_ms = manifest.get("avg_response_ms", 1000)

        score = self._score_intents(intents, text)
        score += self._score_capabilities(caps, text)
        score += self._score_performance(avg_response_ms)

        return score

    def _map_agent_id_to_key(self, agent_id: str) -> str:
        mapping = {
            "climate_sentinel": "sentinelle",
            "formation_coach": "formation",
            "market_coach": "market",
            "marketplace_agent": "marketplace",
            "plant_doctor": "sentinelle",
        }
        if agent_id in mapping:
            return mapping[agent_id]

        aid = agent_id or ""
        patterns = [
            (["sentinel", "climate"], "sentinelle"),
            (["marketplace", "place"], "marketplace"),
            (["market"], "market"),
        ]
        
        for keywords, result in patterns:
            if any(kw in aid for kw in keywords):
                if result == "market" and "place" in aid:
                    continue
                return result

        return None

    def _score_and_filter_manifests(self, manifests: List[Dict[str, Any]], text: str) -> List[tuple]:
        scores = []
        for m in manifests:
            score = self._score_manifest(m, text)
            if score > 0:
                scores.append((score, m))

        if not scores:
            return []

        scores.sort(key=lambda x: (-x[0], x[1].get("avg_response_ms", 1000)))
        return scores

    def _map_selected_agents(self, agent_ids: List[str]) -> List[str]:
        mapped = []
        for aid in agent_ids:
            mapped_key = self._map_agent_id_to_key(aid)
            if mapped_key and mapped_key not in mapped:
                mapped.append(mapped_key)
        return mapped

    # --- Public selection API ---
    def select_experts(self, user_input: str, manifests: List[Dict[str, Any]], limit: int = 2) -> List[str]:
        if not manifests:
            return []

        text = (user_input or "").lower()
        scores = self._score_and_filter_manifests(manifests, text)

        if not scores:
            return ["sentinelle"]

        selected = [m.get("agent_id") for _, m in scores[:limit]]
        mapped = self._map_selected_agents(selected)

        return mapped or ["sentinelle"]

    # --- Card/catalog helpers ---
    def _get_card_description(self, card: Any) -> str:
        desc = getattr(card, "description", None)
        if desc:
            return desc
        if isinstance(card, dict):
            return card.get("description", str(card))
        return str(card)

    def _build_catalog_from_dict(self, agent_cards: Dict[str, Any]) -> List[str]:
        return [f"- {name}: {self._get_card_description(card)}" for name, card in agent_cards.items()]

    def _build_catalog_from_list(self, agent_cards: List[Dict[str, Any]]) -> List[str]:
        entries = []
        for m in agent_cards:
            aid = m.get("agent_id") or m.get("name") or "unknown"
            desc = m.get("description") or m.get("capabilities", "")
            entries.append(f"- {aid}: {desc}")
        return entries

    def _build_expert_catalog(self, agent_cards: Any) -> str:
        try:
            if isinstance(agent_cards, dict):
                entries = self._build_catalog_from_dict(agent_cards)
            elif isinstance(agent_cards, list):
                entries = self._build_catalog_from_list(agent_cards)
            else:
                entries = [str(agent_cards)]
            return "\n".join(entries)
        except Exception:
            return "(no agent catalog)"

    def _fetch_agent_cards(self, ctx) -> Dict[str, Any]:
        # A2A is disabled in the simplified orchestrator; return empty
        # manifest list so routing falls back to default heuristics.
        return {}

    # --- Analyze / route ---
    def analyze_needs(self, state: Dict[str, Any], llm=None, ctx=None) -> Dict[str, Any]:
        _llm = llm or self.llm
        _ctx = ctx or self.ctx

        query = state.get("requete_utilisateur", "")

        # fetch manifests from A2A if available
        agent_cards = self._fetch_agent_cards(_ctx) if _ctx else {}
        expert_catalog = self._build_expert_catalog(agent_cards)

        system_prompt = (
            "Tu es le 'Cerveau Central' d'AgriConnect.\n"
            "Analyse la requête de l'agriculteur en fonction des experts DISPONIBLES ci-dessous :\n\n"
            f"{expert_catalog}\n\n"
            "DIRECTIVES :\n"
            "1. Détecte les ARNAQUES (demandes de fonds/codes) -> intent: REJECT\n"
            "2. CLASSIFICATION :\n"
            "   - 'CHAT' : Simple politesse.\n"
            "   - 'SOLO' : Un seul expert peut répondre.\n"
            "   - 'COUNCIL' : Plusieurs experts requis (ex: market + marketplace).\n"
            "   - 'REJECT' : Hors-sujet ou arnaque.\n\n"
            "IMPORTANT: Inclus un champ 'confidence_score' numérique entre 0.0 et 1.0 indiquant la confiance de la décision.\n"
            "Réponds uniquement par un JSON strict, sans explication, sans phrase d'intro ni excuse.\n"
            '{"intent": "REJECT"|"CHAT"|"SOLO"|"COUNCIL", '
            '"selected_experts": ["nom_expert_1", "nom_expert_2"], '
            '"confidence_score": 0.0, "reason": "si reject"}'
        )

        try:
            logger.info("Calling LLM routing model for routing")
            response = _llm.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": query}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content if response.choices else str(response)
            logger.info("LLM raw routing response: %s", raw)
            analysis = json.loads(raw)
            # Ensure confidence_score exists and is numeric; provide reasonable
            # fallbacks when the model omitted it.
            try:
                analysis_conf = float(analysis.get("confidence_score", None))
            except Exception:
                analysis_conf = None
            if analysis_conf is None:
                # Infer confidence: if the model selected experts or clearly
                # set an intent, assume high confidence; otherwise low.
                if (analysis.get("selected_experts") or []) or (analysis.get("intent") or "").upper() == "CHAT":
                    analysis_conf = 0.85
                else:
                    analysis_conf = 0.0
            # clamp
            analysis["confidence_score"] = max(0.0, min(1.0, float(analysis_conf)))
            sel = analysis.get("selected_experts") or []
            # If the LLM provided a textual reason mentioning an expert
            # (e.g. "formation_coach") but did not populate
            # `selected_experts`, try to map known tokens from the reason
            # into the selected_experts field so routing can act on it.
            if not sel:
                reason_text = (analysis.get("reason") or "").lower()
                if reason_text:
                    mapped_candidates = []
                    tokens = [
                        "formation_coach",
                        "formation",
                        "marketplace",
                        "market_coach",
                        "market",
                        "sentinelle",
                        "sentinel",
                        "plant_doctor",
                        "marketplace_agent",
                    ]
                    for tok in tokens:
                        if tok in reason_text:
                            mapped = self._map_agent_id_to_key(tok)
                            if mapped:
                                mapped_candidates.append(mapped)
                    # dedupe, keep order
                    if mapped_candidates:
                        uniq = []
                        for c in mapped_candidates:
                            if c not in uniq:
                                uniq.append(c)
                        analysis["selected_experts"] = uniq
                        logger.info("Mapped LLM reason -> selected_experts: %s", uniq)

                    # Recompute sel in case we populated `analysis['selected_experts']`
                    sel = analysis.get("selected_experts") or []

            # Avoid forcing a default expert for simple CHAT intents; short
            # chat messages should be handled locally without A2A calls.
            intent_val = (analysis.get("intent") or "").upper()
            # If the LLM didn't return selected_experts, apply lightweight
            # heuristics (based on reason text and the user query) to
            # infer an expert for short CHAT intents where appropriate.
            if not sel:
                if intent_val == "CHAT":
                    # Normalize query & reason: remove punctuation and unify hyphens
                    raw_query = (state.get("requete_utilisateur", "") or "").lower()
                    # Replace hyphens with spaces (e.g. 'explique-moi' -> 'explique moi')
                    query_text = re.sub(r"[-–—]", " ", raw_query)
                    # Remove surrounding punctuation
                    query_text = re.sub(r"[^\w\sàâäéèêëîïôöùûüç]", "", query_text)

                    reason_text = (analysis.get("reason") or "").lower()
                    reason_text = re.sub(r"[-–—]", " ", reason_text)
                    reason_text = re.sub(r"[^\w\sàâäéèêëîïôöùûüç]", "", reason_text)

                    # Prefer explicit hints from the reason (already attempted
                    # above). For query-based heuristics avoid matching very
                    # short greetings or the single word 'comment' which is
                    # often used in casual salutations ("comment ça va").
                    tokens_count = len(query_text.split())
                    greetings = ("bonjour", "salut", "bonsoir", "hello", "hi")
                    if any(k in reason_text for k in ("formation", "formation_coach")):
                        analysis["selected_experts"] = ["formation"]
                    elif tokens_count > 3 and not any(query_text.strip().startswith(g) for g in greetings) and any(k in query_text for k in ("explique", "comment faire", "tutoriel", "procédé", "comment")):
                        analysis["selected_experts"] = ["formation"]
                    elif any(k in query_text for k in ("prix", "marché", "marche")):
                        analysis["selected_experts"] = ["market"]
                    elif any(k in query_text for k in ("maladie", "feuille", "tache", "sympt", "insecte")):
                        analysis["selected_experts"] = ["sentinelle"]
                    # Otherwise keep empty for true short polite CHATs
                else:
                    analysis["selected_experts"] = ["dev_stub"] if settings.DEBUG else ["sentinelle"]

            # Return the analysis dict directly (caller expects a plain
            # analysis object with keys: intent, selected_experts, confidence_score)
            return analysis
        except Exception as e:
            logger.exception("Routing Error while analyzing needs: %s", e)
            return {"intent": "REJECT", "selected_experts": [], "confidence_score": 0.0, "reason": "error"}

    def _check_multi_need_flags(self, needs: Dict[str, Any]) -> str:
        need_flags = [bool(needs.get(k)) for k in ("needs_formation", "needs_sentinelle", "needs_market", "needs_marketplace")]
        if sum(1 for v in need_flags if v) > 1:
            return "PARALLEL_EXPERTS"
        if needs.get("needs_formation"):
            return "SOLO_FORMATION"
        if needs.get("needs_sentinelle"):
            return "SOLO_SENTINELLE"
        if needs.get("needs_market"):
            return "SOLO_MARKET"
        return None

    def route_flow(self, state: Dict[str, Any]) -> str:
        needs = state.get("needs", {})
        intent = needs.get("intent")
        selected = needs.get("selected_experts", [])

        routing_map = {
            "REJECT": "REJECT",
            "CHAT": "EXECUTE_CHAT",
        }

        if intent in routing_map:
            # CHAT is usually a short greeting — however the LLM may
            # include a "reason" hint with a recommended expert (e.g.
            # "formation_coach"). If the analysis suggests an expert
            # despite intent==CHAT, route to that expert to avoid
            # ignoring useful hints.
            if intent == "CHAT":
                sel = needs.get("selected_experts", []) or []
                if sel:
                    return f"SOLO_{sel[0].upper()}"

                # Prefer explicit LLM hints in `reason`
                reason = (needs.get("reason") or "").lower()
                expert_keywords = {
                    "formation": "FORMATION",
                    "formation_coach": "FORMATION",
                    "marketplace": "MARKETPLACE",
                    "market": "MARKET",
                    "sentinel": "SENTINELLE",
                    "sentinelle": "SENTINELLE",
                    "plant_doctor": "SENTINELLE",
                }
                for kw, node in expert_keywords.items():
                    if kw in reason:
                        return f"SOLO_{node}"

                # Lightweight query heuristics: map common user intents
                # to experts when the LLM response is ambiguous.
                # Normalize query text similar to analyze_needs earlier
                query_text = (state.get("requete_utilisateur", "") or "").lower()
                query_text = re.sub(r"[-–—]", " ", query_text)
                query_text = re.sub(r"[^\w\sàâäéèêëîïôöùûüç]", "", query_text)
                if any(k in query_text for k in ("explique", "comment faire", "comment", "tutoriel", "procédé")):
                    return "SOLO_FORMATION"
                if any(k in query_text for k in ("prix", "marché", "marche")):
                    return "SOLO_MARKET"
                if any(k in query_text for k in ("maladie", "feuille", "tache", "sympt", "insecte")):
                    return "SOLO_SENTINELLE"

            return routing_map[intent]

        multi_need_route = self._check_multi_need_flags(needs)
        if multi_need_route:
            return multi_need_route

        if intent == "COUNCIL" or len(selected) > 1:
            return "PARALLEL_EXPERTS"

        if len(selected) == 1:
            return f"SOLO_{selected[0].upper()}"

        return "REJECT"


# Default module-level router instance for backwards compatibility
_DEFAULT_ROUTER = Router()

def _score_intents(intents: List[str], text: str) -> int:
    return _DEFAULT_ROUTER._score_intents(intents, text)


def _score_capabilities(capabilities: List[str], text: str) -> int:
    return _DEFAULT_ROUTER._score_capabilities(capabilities, text)


def _score_performance(avg_response_ms: int) -> int:
    return _DEFAULT_ROUTER._score_performance(avg_response_ms)


def _score_manifest(manifest: Dict[str, Any], text: str) -> int:
    return _DEFAULT_ROUTER._score_manifest(manifest, text)


def _map_agent_id_to_key(agent_id: str) -> str:
    return _DEFAULT_ROUTER._map_agent_id_to_key(agent_id)


def _score_and_filter_manifests(manifests: List[Dict[str, Any]], text: str) -> List[tuple]:
    return _DEFAULT_ROUTER._score_and_filter_manifests(manifests, text)


def _map_selected_agents(agent_ids: List[str]) -> List[str]:
    return _DEFAULT_ROUTER._map_selected_agents(agent_ids)


def select_experts(user_input: str, manifests: List[Dict[str, Any]], limit: int = 2) -> List[str]:
    return _DEFAULT_ROUTER.select_experts(user_input, manifests, limit=limit)



def _get_card_description(card: Any) -> str:
    """Delegate to Router._get_card_description for compatibility."""
    return _DEFAULT_ROUTER._get_card_description(card)


def _build_catalog_from_dict(agent_cards: Dict[str, Any]) -> List[str]:
    return _DEFAULT_ROUTER._build_catalog_from_dict(agent_cards)


def _build_catalog_from_list(agent_cards: List[Dict[str, Any]]) -> List[str]:
    return _DEFAULT_ROUTER._build_catalog_from_list(agent_cards)


def _build_expert_catalog(agent_cards: Any) -> str:
    return _DEFAULT_ROUTER._build_expert_catalog(agent_cards)


def _fetch_agent_cards(ctx) -> Dict[str, Any]:
    return _DEFAULT_ROUTER._fetch_agent_cards(ctx)


def analyze_needs(state: Dict[str, Any], llm, ctx) -> Dict[str, Any]:
    return _DEFAULT_ROUTER.analyze_needs(state, llm, ctx)


def _check_multi_need_flags(needs: Dict[str, Any]) -> str:
    return _DEFAULT_ROUTER._check_multi_need_flags(needs)


def route_flow(state: Dict[str, Any]) -> str:
    return _DEFAULT_ROUTER.route_flow(state)
