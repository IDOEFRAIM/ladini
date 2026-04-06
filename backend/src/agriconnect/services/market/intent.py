import json
import logging
import re
import unicodedata
from typing import Any, Dict, Optional

from agriconnect.graphs.prompts import MARKET_EXTRACT_INTENT_TEMPLATE

logger = logging.getLogger("AgriConnect.IntentService")

class IntentService:
    """Service dedicated to Natural Language Understanding (NLU).
    Uses a hybrid approach (Rules > LLM) to minimize latency and cost.
    """

    _PRODUCT_ALIASES = {
        "mais": "mais",
        "maïs": "mais",
        "sorgho": "sorgho",
        "mil": "mil",
        "riz": "riz",
        "niebe": "niebe",
        "niébé": "niebe",
        "sesame": "sesame",
        "sésame": "sesame",
        "arachide": "arachide",
        "tomate": "tomate",
        "oignon": "oignon",
        "pomme de terre": "pomme de terre",
        "pommes de terre": "pomme de terre",
        "patate": "pomme de terre",
        "igname": "igname",
        "manioc": "manioc",
        "cassava": "manioc",
    }

    def __init__(self, llm_client: Any, model_name: str | None = None):
        self.llm = llm_client
        self.model_name = model_name

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Lowercase + remove accents/punctuation variance for robust matching."""
        if not text:
            return ""
        txt = text.lower().replace("'", " ").replace("’", " ")
        txt = unicodedata.normalize("NFKD", txt)
        txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
        txt = re.sub(r"[^a-z0-9\s\-]", " ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt

    def _extract_product(self, q_norm: str) -> Optional[str]:
        """Extract canonical product with alias support and word boundaries."""
        # Longer aliases first (e.g. "pommes de terre" before "terre")
        items = sorted(self._PRODUCT_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)
        for alias, canonical in items:
            alias_norm = self._normalize_text(alias)
            if re.search(rf"\b{re.escape(alias_norm)}\b", q_norm):
                return canonical
        return None

    def analyze(self, query: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Complete NLU pipeline: Heuristics -> LLM Fallback -> Context Enrichment."""
        context = context or {}
        
        # 1. Fast Path: Rule-based extraction
        heuristic_result = self._apply_heuristics(query)
        
        # 2. Check if heuristics are sufficient (high confidence)
        # Heuristics are sufficient if we found a clear intent and at least one entity
        is_confident = (
            heuristic_result.get("intent") != "CHECK_PRICE" 
            or (heuristic_result.get("product") and heuristic_result.get("quantity"))
        )
        
        # 3. Slow Path: LLM Extraction (only if heuristics failed or are ambiguous)
        if not is_confident and self.llm:
             llm_result = self._extract_market_intent_llm(query)
             # Merge: Prefer LLM for intent if heuristic was weak, but keep heuristic entities if valid
             final_intent = heuristic_result.get("intent") if heuristic_result.get("intent") != "CHECK_PRICE" else llm_result.get("intent", "CHECK_PRICE")
             
             # Enrich entities from LLM if missing in heuristics
             product = heuristic_result.get("product") or llm_result.get("product")
             quantity = heuristic_result.get("quantity") if heuristic_result.get("quantity") is not None else llm_result.get("quantity")
             location = heuristic_result.get("location") or llm_result.get("location")
             price = heuristic_result.get("price") if heuristic_result.get("price") is not None else llm_result.get("price")
             unit = heuristic_result.get("unit") or llm_result.get("unit")
             
             heuristic_result = {
                 "intent": final_intent,
                 "product": product,
                 "quantity": quantity,
                 "location": location,
                 "price": price,
                 "unit": unit
             }
        
        # 4. Sticky Intent Logic (Context Awareness)
        # If user provides only entities ("40k", "à Gaoua") but no verb, infer intent from previous turn.
        final_result = self._apply_sticky_intent(heuristic_result, query, context)
        
        return final_result

    def _apply_heuristics(self, query: str) -> Dict[str, Any]:
        """Regex-based entity and intent extraction."""
        q_lower = query.lower()
        q_norm = self._normalize_text(query)

        result = {
            "intent": "CHECK_PRICE",
            "product": None,
            "quantity": None,
            "unit": None,
            "price": None,
            "location": None,
        }

        # 1. Product Creation / Catalog
        if any(
            w in q_norm
            for w in [
                "catalogue",
                "nouveau produit",
                "creer produit",
                "ajouter produit",
                "base de donne",
                "dans la base",
            ]
        ):
            result["intent"] = "CREATE_PRODUCT"

        # 2. Selling / Surplus (if not already Create)
        elif any(
            w in q_norm
            for w in [
                "vends",
                "vendre",
                "offre",
                "disponible",
                "dispo",
                "j ai",
                "jai",
                "stock",
                "enregistrer",
                "ajout",
                "cultive",
                "recolte",
            ]
        ):
            result["intent"] = "REGISTER_SURPLUS"
            if "catalogue" in q_norm:
                result["intent"] = "CREATE_PRODUCT"

        # 3. Buying
        elif any(w in q_norm for w in ["achete", "acheter", "cherche", "besoin", "veux"]):
            result["intent"] = "BUY_OFFER"

        # Price with keyword "prix" or currency
        price_match = re.search(r"(?:prix\s*[:]?\s*)?(\d+(?:[.,]\d+)?)\s*(?:f|cfa|fcfa)\b", q_norm)
        if not price_match:
            price_match = re.search(r"\bprix\s*[:]?\s*(\d+(?:[.,]\d+)?)\b", q_norm)

        if price_match:
            try:
                val_str = price_match.group(1).replace(",", ".")
                result["price"] = float(val_str)
            except Exception:
                pass

        # Quantity: number + unit
        qty_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kilo|kg|t|tonnes|sac|sacs|unite|unites)\b", q_norm)
        if qty_match:
            try:
                val = float(qty_match.group(1).replace(",", "."))
                result["quantity"] = val
                unit = qty_match.group(2)
                if unit.startswith("t"):
                    result["unit"] = "tonnes"
                elif unit.startswith("s"):
                    result["unit"] = "sacs"
                else:
                    result["unit"] = "kg"
            except Exception:
                pass
        elif "quantite" in q_norm or "qte" in q_norm:
            q_search = re.search(r"(?:quantite|qte)\s*[:]?\s*(\d+(?:[.,]\d+)?)", q_norm)
            if q_search:
                result["quantity"] = float(q_search.group(1).replace(",", "."))
                result["unit"] = "kg"

        # Product extraction with aliases and canonical value.
        result["product"] = self._extract_product(q_norm)

        # Location extraction robust to lowercase and plain "a" in informal messages.
        loc_match = re.search(
            r"\b(?:a|à|sur|vers|dans)\s+([a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ\-\s]{1,40})",
            q_lower,
        )
        if loc_match:
            loc = loc_match.group(1).strip(" ;,.:!?")
            if loc:
                result["location"] = loc.title()

        # If we have a product AND a quantity, and intent is still default,
        # this is very likely an inventory declaration.
        if result["intent"] == "CHECK_PRICE" and result.get("product") and result.get("quantity") is not None:
            result["intent"] = "REGISTER_SURPLUS"

        # Guardrail for noisy declarative inventory phrasing.
        has_inventory_shape = (
            result.get("quantity") is not None
            and bool(result.get("product"))
            and any(w in q_norm for w in ["j ai", "jai", "cultive", "recolte", "stock", "disponible"])
        )
        if result["intent"] == "CHECK_PRICE" and has_inventory_shape:
            result["intent"] = "REGISTER_SURPLUS"

        return result

    def _extract_market_intent_llm(self, query: str) -> Dict[str, Any]:
        if not self.llm: return {"intent": "CHECK_PRICE"}
        try:
            formatted = MARKET_EXTRACT_INTENT_TEMPLATE.format(query=query)
            resp = self.llm.chat.completions.create(
                model=self.model_name or "llama3-70b-8192", # Default or configured
                messages=[{"role": "user", "content": formatted}],
                temperature=0,
                response_format={"type": "json_object"}
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("LLM Intent Extraction failed: %s", e)
            return {"intent": "CHECK_PRICE"}

    def _apply_sticky_intent(self, check_result: Dict[str, Any], query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Apply sticky intent logic logic: if inputs are provided without verb, reuse pending intent."""
        
        current_intent = check_result.get("intent", "CHECK_PRICE")
        pending_intent = context.get("pending_intent")
        
        # Heuristic: Short inputs or entity-only inputs usually imply continuation
        tokens = query.split()
        short_input = len(tokens) < 8
        has_entities = bool(check_result.get("location") or check_result.get("quantity") or check_result.get("price"))
        
        # If we detected "CHECK_PRICE" (default) but have entities and a pending transactional intent, override it.
        if current_intent == "CHECK_PRICE" and pending_intent in ["REGISTER_SURPLUS", "CREATE_PRODUCT", "BUY_OFFER"]:
            if short_input or has_entities:
                check_result["intent"] = pending_intent
                logger.info(f"Sticky Intent Triggered: Overriding CHECK_PRICE with {pending_intent}")

        # Fallback: declarative completion message (price + location) is likely
        # answering a prior registration prompt, not asking for market lookup.
        if check_result.get("intent") == "CHECK_PRICE" and not pending_intent:
            has_price = check_result.get("price") is not None
            has_location = bool(check_result.get("location"))
            asks_market_price = any(
                token in query.lower()
                for token in ["prix de", "combien", "quel prix", "?", "marche", "marché"]
            )
            if has_price and has_location and not asks_market_price:
                check_result["intent"] = "REGISTER_SURPLUS"
                logger.info("Follow-up completion heuristic triggered: CHECK_PRICE -> REGISTER_SURPLUS")
        
        return check_result
