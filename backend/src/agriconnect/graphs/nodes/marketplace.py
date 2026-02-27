"""
MarketplaceAgent — Agent conversationnel Agribusiness (LangGraph).

Gère les interactions agriculteurs via WhatsApp :
  • Identification par téléphone (auto-onboarding)
  • Gestion stock & dépenses
  • Mise en vente (produits)
  • Matching acheteur ↔ vendeur par zone
  • Commandes

Architecture : graphe LangGraph IDENTIFY → PARSE → EXECUTE → CONFIRM → MATCH.
"""

import json
import logging
import re
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph

from agriconnect.graphs.prompts import MARKETPLACE_SYSTEM_PROMPT
from agriconnect.tools.marketplace import MarketplaceTool

# MCP & A2A & AG-UI Protocols
from agriconnect.protocols.mcp import MCPDatabaseServer
from agriconnect.protocols.a2a import A2ADiscovery, A2AMessage, MessageType, HandshakeStatus
from agriconnect.protocols.ag_ui import (
    AgriResponse, AGUIComponent, ComponentType,
    TextBlock, Card, ActionButton, ActionType, ListPicker,
)

logger = logging.getLogger("Agent.Marketplace")


# ── État du graphe ──────────────────────────────────────────────
class MarketplaceState(TypedDict, total=False):
    # ── Input
    user_query: str
    user_phone: str
    zone_id: Optional[str]
    # ── Identity
    user_profile: Dict[str, Any]
    producer_id: Optional[str]
    farm_id: Optional[str]
    # ── Intent parsing
    intent: str
    parsed: Dict[str, Any]
    # ── Execution
    action_result: Dict[str, Any]
    transaction_payload: Optional[Dict[str, Any]]  # pending TX, not yet written
    # ── Matching
    matches: List[Dict[str, Any]]
    # ── Output
    final_response: str
    agri_response: Optional[AgriResponse]
    status: str
    warnings: List[str]          # plain list — overwrite each cycle
    errors: List[str]            # plain list — overwrite each cycle
    # ── Loop guards
    retry_counter: int           # incremented on execute retries
    degraded_mode: bool          # True when retry_counter > MAX_RETRIES
    # ── Financial safety
    requires_human_review: bool  # True when total_fcfa > FINANCIAL_THRESHOLD_FCFA


# Maximum execute-action retries before activating degraded mode
_MAX_RETRIES: int = 2
# Financial transactions above this amount require explicit user confirmation
_FINANCIAL_THRESHOLD_FCFA: int = 100_000


# ── Intents reconnus ────────────────────────────────────────────
INTENTS = [
    "REGISTER_STOCK",
    "SELL_PRODUCT",
    "CHECK_STOCK",
    "CHECK_ORDERS",
    "UPDATE_STOCK",
    "FIND_BUYERS",
    "FIND_PRODUCTS",
    "CREATE_ORDER",
    "HELP",
]


class MarketplaceAgent:
    """Agent agribusiness — sous-graphe LangGraph appelé par l'orchestrateur."""

    def __init__(self, llm_client=None, mcp_db: MCPDatabaseServer = None, a2a: A2ADiscovery = None):
        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"
        self.tool = MarketplaceTool() # Legacy tool, gradually migrate to MCP
        self.mcp_db = mcp_db
        self.a2a = a2a

        try:
            from agriconnect.rag.components import get_groq_sdk
            self.llm = llm_client if llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("Impossible d'initialiser le LLM Marketplace : %s", exc)
            self.llm = None

    # ════════════════════════════════════════════════════════════════
    # NŒUDS DU GRAPHE
    # ════════════════════════════════════════════════════════════════

    def identify_user_node(self, state: MarketplaceState) -> MarketplaceState:
        """Identifie l'utilisateur par son numéro de téléphone."""
        state = dict(state)
        phone = state.get("user_phone", "")
        zone_id = state.get("zone_id")

        if not phone:
            state["status"] = "ERROR"
            state["final_response"] = (
                "Je n'ai pas pu identifier votre numéro. "
                "Envoyez votre message depuis WhatsApp pour que je vous reconnaisse."
            )
            return state

        try:
            user = self.tool.identify_or_create_user(phone, zone_id=zone_id)
            state["user_profile"] = user
            state["producer_id"] = user.get("producer_id")

            if user.get("is_new"):
                state["warnings"] = state.get("warnings", []) + ["NOUVEAU_UTILISATEUR"]
                logger.info("Nouvel agriculteur onboardé : %s", phone)

            # Récupérer la ferme
            if state["producer_id"]:
                farm = self.tool.get_or_create_farm(state["producer_id"], zone_id=zone_id)
                state["farm_id"] = farm["id"]

            state["status"] = "IDENTIFIED"
        except Exception as e:
            logger.error("Identification erreur : %s", e)
            state["status"] = "ERROR"
            state["final_response"] = "Erreur d'identification. Réessayez."

        return state

    def parse_intent_node(self, state: MarketplaceState) -> MarketplaceState:
        """Analyse l'intention et extrait les entités (produit, quantité, prix…)."""
        state = dict(state)
        if state.get("status") == "ERROR":
            return state

        query = state.get("user_query", "")
        if not query:
            state["intent"] = "HELP"
            state["parsed"] = {}
            return state

        prompt = (
            "Tu es un assistant agricole. Analyse le message de l'agriculteur.\n\n"
            f"Message : {query}\n\n"
            "Extrais les informations au format JSON strict :\n"
            "{{\n"
            '  "intent": "REGISTER_STOCK"|"SELL_PRODUCT"|"CHECK_STOCK"|"CHECK_ORDERS"|'
            '"UPDATE_STOCK"|"FIND_BUYERS"|"FIND_PRODUCTS"|"CREATE_ORDER"|"HELP",\n'
            '  "product": "nom du produit ou null",\n'
            '  "quantity": number ou null,\n'
            '  "unit": "sac"|"tine"|"plat"|"kg"|"tonne" ou null,\n'
            '  "price": number ou null,\n'
            '  "category": "Céréales"|"Légumineuses"|"Légumes"|"Fruits"|"Tubercules"|"Autres" ou null,\n'
            '  "description": "détails supplémentaires ou null"\n'
            "}}\n\n"
            "Exemples :\n"
            '- "J\'ai 10 sacs de maïs" → intent=REGISTER_STOCK, product=maïs, quantity=10, unit=sac\n'
            '- "Je veux vendre mon sorgho à 250 le kg" → intent=SELL_PRODUCT, product=sorgho, price=250\n'
            '- "Combien j\'ai en stock ?" → intent=CHECK_STOCK\n'
            '- "Qui cherche du mil ?" → intent=FIND_BUYERS, product=mil\n'
            '- "Je cherche du riz" → intent=FIND_PRODUCTS, product=riz\n'
        )

        try:
            resp = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("Parse intent error : %s", e)
            parsed = {"intent": "HELP"}

        state["intent"] = parsed.get("intent", "HELP")
        state["parsed"] = parsed
        state["status"] = "PARSED"

        # ── Financial safety guardrail ─────────────────────────────────────
        # Flag any transaction whose estimated total exceeds the threshold so
        # confirm_node can request explicit user consent before execution.
        price = parsed.get("price") or 0
        qty   = parsed.get("quantity") or 0
        estimated_fcfa = float(price) * float(qty)
        if estimated_fcfa > _FINANCIAL_THRESHOLD_FCFA:
            state["requires_human_review"] = True
            state["warnings"] = list(state.get("warnings", []))
            state["warnings"].append(
                f"FINANCIAL_REVIEW_REQUIRED: montant estimé {estimated_fcfa:,.0f} FCFA"
            )
            logger.warning(
                "Financial safety: estimated %.0f FCFA for intent=%s — requiring human review",
                estimated_fcfa, state["intent"],
            )
        else:
            state["requires_human_review"] = False

        return state

    def execute_action_node(self, state: MarketplaceState) -> MarketplaceState:
        """Exécute l'action CRUD correspondant à l'intention.

        For intents that involve financial writes (SELL_PRODUCT, CREATE_ORDER)
        the action result is stored as *pending* ``transaction_payload`` and
        no DB write is performed here.  ``confirm_node`` then shows the payload
        to the user and gates the actual commit behind explicit consent.
        """
        state = dict(state)
        if state.get("status") == "ERROR":
            return state

        # ── Degraded-mode loop guard ───────────────────────────────────────
        retry_counter = int(state.get("retry_counter", 0))
        if retry_counter > _MAX_RETRIES:
            logger.error(
                "MarketplaceAgent DEGRADED_MODE: retry_counter=%d > MAX=%d",
                retry_counter, _MAX_RETRIES,
            )
            state["degraded_mode"] = True
            state["status"] = "DEGRADED_MODE"
            state["final_response"] = (
                "⚠️ Le service rencontre des difficultés temporaires. "
                "Votre demande a été enregistrée et sera traitée dès que possible."
            )
            return state
        state["retry_counter"] = retry_counter + 1

        intent = state.get("intent", "HELP")
        handler = self._get_intent_handler(intent)
        try:
            result = handler(state)
        except Exception as e:
            logger.error("Execute action error (%s) : %s", intent, e)
            result = {"error": str(e)}

        # For write intents: store as pending transaction_payload (no DB commit yet)
        write_intents = {"SELL_PRODUCT", "CREATE_ORDER", "REGISTER_STOCK", "UPDATE_STOCK"}
        if intent in write_intents and not result.get("error"):
            state["transaction_payload"] = {
                "intent": intent,
                "data": result,
                "parsed": state.get("parsed", {}),
                "status": "PENDING",
                "requires_human_review": state.get("requires_human_review", False),
            }
            state["action_result"] = result
        else:
            state["action_result"] = result
            state["transaction_payload"] = None

        state["status"] = "EXECUTED"
        return state

    def _get_intent_handler(self, intent: str):
        return {
            "REGISTER_STOCK": self._handle_register_stock,
            "SELL_PRODUCT": self._handle_sell_product,
            "CHECK_STOCK": self._handle_check_stock,
            "CHECK_ORDERS": self._handle_check_orders,
            "UPDATE_STOCK": self._handle_update_stock,
            "FIND_BUYERS": self._handle_find_buyers,
            "FIND_PRODUCTS": self._handle_find_products,
            "CREATE_ORDER": self._handle_create_order,
            "HELP": self._handle_help,
        }.get(intent, self._handle_help)

    def _handle_register_stock(self, state: MarketplaceState) -> dict:
        return self._add_stock_action(state, reason=f"Déclaré via WhatsApp : {state.get('user_query', '')}")

    def _handle_sell_product(self, state: MarketplaceState) -> dict:
        producer_id = state.get("producer_id")
        parsed = state.get("parsed", {})
        if not producer_id:
            return {"error": "Aucun producteur associé."}
        product = parsed.get("product", "produit")
        price = parsed.get("price", 0)
        quantity = parsed.get("quantity", 0)
        unit = parsed.get("unit", "kg")
        category = parsed.get("category", "Céréales")
        desc = parsed.get("description")
        return self.tool.create_product(
            producer_id=producer_id,
            name=product,
            price=price,
            quantity_for_sale=quantity,
            unit=unit,
            category_label=category,
            description=desc,
        )

    def _handle_check_stock(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        stocks = self.tool.get_stocks(farm_id)
        return {"stocks": stocks, "count": len(stocks)}

    def _handle_check_orders(self, state: MarketplaceState) -> dict:
        producer_id = state.get("producer_id")
        products = self.tool.list_products(producer_id) if producer_id else []
        return {"products": products}

    def _handle_update_stock(self, state: MarketplaceState) -> dict:
        return self._add_stock_action(state, reason="Mise à jour stock via WhatsApp")

    def _add_stock_action(self, state: MarketplaceState, reason: str) -> dict:
        """Shared helper to add or update stock from parsed state.

        Returns an error dict when farm is missing.
        """
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        parsed = state.get("parsed", {})
        product = parsed.get("product", "produit")
        quantity = parsed.get("quantity", 0)
        unit = parsed.get("unit", "kg")
        try:
            return self.tool.add_stock(
                farm_id=farm_id,
                item_name=product,
                quantity=quantity,
                unit=unit,
                reason=reason,
            )
        except Exception as e:
            logger.error("add_stock failed: %s", e)
            return {"error": str(e)}

    def _handle_find_buyers(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        product = parsed.get("product", "")
        return {
            "buyers": self.tool.find_buyers_for_product(product, zone_id),
            "avg_price": self.tool.get_average_price(product, zone_id),
        }

    def _handle_find_products(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        product = parsed.get("product", "")
        return {
            "products_available": self.tool.find_products_for_buyer(product, zone_id),
        }

    def _handle_create_order(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        phone = state.get("user_phone", "")
        product_name = parsed.get("product", "")
        quantity = parsed.get("quantity", 1)
        available = self.tool.find_products_for_buyer(product_name, zone_id)
        if available:
            best = available[0]
            return self.tool.create_order(
                product_id=best["id"],
                quantity=quantity,
                buyer_phone=phone,
                buyer_name=state.get("user_profile", {}).get("name"),
                zone_id=zone_id,
            )
        else:
            alert = self.tool.create_market_alert(product_name, phone, zone_id)
            return {"error": f"Aucun {product_name} disponible dans votre zone.", "alert": alert}

    def _handle_help(self, state: MarketplaceState) -> dict:
        return {"help": True}

    def confirm_node(self, state: MarketplaceState) -> MarketplaceState:
        """Génère la réponse conversationnelle (AG-UI) + Texte."""
        state = dict(state)
        agri_response = AgriResponse(agent="marketplace")
        
        if state.get("status") == "ERROR":
            return state

        intent = state.get("intent", "HELP")
        result = state.get("action_result", {})
        parsed = state.get("parsed", {})
        user = state.get("user_profile", {})
        is_new = "NOUVEAU_UTILISATEUR" in state.get("warnings", [])

        # Génération texte via LLM (inchangé)
        # ...existing code...
        needs_review = state.get("requires_human_review", False)
        tx_payload   = state.get("transaction_payload")

        context = {
            "intent": intent,
            "result": result,
            "parsed": parsed,
            "user_name": user.get("name", "Agriculteur"),
            "is_new_user": is_new,
            "requires_human_review": needs_review,
            "transaction_payload": tx_payload,
        }

        review_instruction = (
            "\n⚠️ IMPORTANT : Cette transaction dépasse 100 000 FCFA. "
            "Demande explicitement à l'agriculteur de confirmer avec OUI avant d'exécuter.\n"
            if needs_review else ""
        )

        prompt = (
            f"{MARKETPLACE_SYSTEM_PROMPT}\n\n"
            "CONTEXTE DE L'ACTION :\n"
            f"{json.dumps(context, ensure_ascii=False, default=str)}\n\n"
            f"Message original de l'agriculteur : {state.get('user_query', '')}\n\n"
            f"{review_instruction}"
            "Génère une réponse claire et chaleureuse en français simple.\n"
            "Si c'est un nouvel utilisateur, souhaite-lui la bienvenue.\n"
            "Confirme l'action réalisée avec les détails importants.\n"
            "Utilise des emojis pertinents (🌾🛒✅💰📦).\n"
            "Propose une action suivante si pertinent."
        )

        try:
            resp = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            final_text = resp.choices[0].message.content
        except Exception as e:
            logger.warning("Confirm node error : %s", e)
            final_text = self._fallback_response(intent, result, is_new)
        
        state["final_response"] = final_text
        
        # ── Construction AG-UI ──
        response_obj = AgriResponse(agent="marketplace")
        response_obj.add_text(final_text)
        
        # Ajout de composants interactifs selon l'intent
        if needs_review:
            # Financial safety: require explicit user confirmation before commit
            from agriconnect.protocols.ag_ui import ActionButton, ActionType
            response_obj.add(ActionButton(
                label="✅ OUI, confirmer la transaction",
                action_type=ActionType.CONFIRM,
                payload={"action": "commit_transaction", "tx": tx_payload},
            ))
            response_obj.add(ActionButton(
                label="❌ Non, annuler",
                action_type=ActionType.CANCEL,
                payload={"action": "cancel_transaction"},
            ))
        elif intent == "SELL_PRODUCT":
             response_obj.add(ListPicker(
                title="Options de vente :",
                items=[
                    {"id": "view_offers", "label": "👀 Voir les offres"},
                    {"id": "modify_price", "label": "✏️ Modifier prix"}
                ]
            ))
        elif intent == "CHECK_STOCK":
             if result.get("stocks"):
                 # Créer un résumé structuré du stock en Card
                 stock_lines = "\n".join(
                     f"• {s.get('item_name')}: {s.get('quantity')} kg"
                     for s in result.get("stocks", [])
                 )
                 response_obj.add_card(
                     title="Votre Stock",
                     body=stock_lines,
                 )

        state["agri_response"] = response_obj
        state["status"] = "CONFIRMED"
        return state

    def match_check_node(self, state: MarketplaceState) -> MarketplaceState:
        """
        Vérifie les acheteurs (Local DB) ET Broadcast A2A pour la scalabilité.
        """
        state = dict(state)
        intent = state.get("intent")

        if intent != "SELL_PRODUCT":
            return state

        result = state.get("action_result", {})
        product_name = state.get("parsed", {}).get("product", "")
        zone_id = state.get("zone_id")
        phone = state.get("user_phone", "")
        product_id = result.get("product_id")

        if product_name and product_id:
            self._handle_local_matching(state)
            self._handle_a2a_broadcast(state)

        state["status"] = "COMPLETED"
        return state

    def _handle_local_matching(self, state):
        product_name = state.get("parsed", {}).get("product", "")
        zone_id = state.get("zone_id")
        phone = state.get("user_phone", "")
        product_id = state.get("action_result", {}).get("product_id")
        matches = self.tool.auto_match(product_name, zone_id, phone, product_id)
        if matches:
            state["matches"] = matches
            match_msg = (
                f"\n\n🎯 **Bonne nouvelle !** {len(matches)} acheteur(s) "
                f"cherchent du {product_name} dans votre zone :\n"
            )
            for m in matches:
                match_msg += f"  • {m['buyer_phone']} ({m.get('zone_name', '')})\n"
            match_msg += "\nJe peux les mettre en contact avec vous. Voulez-vous ?"

            state["final_response"] = state.get("final_response", "") + match_msg

            # Mise à jour AG-UI
            if "agri_response" in state:
                state["agri_response"].add_text(match_msg)

    def _handle_a2a_broadcast(self, state):
        product_name = state.get("parsed", {}).get("product", "")
        zone_id = state.get("zone_id")
        phone = state.get("user_phone", "")
        product_id = state.get("action_result", {}).get("product_id")
        if self.a2a:
            try:
                offer_msg = A2AMessage(
                    message_type=MessageType.BROADCAST,
                    sender_id=f"agent_market_{phone}",
                    intent="SELL_OFFER",
                    zone=zone_id or "global",
                    crop=product_name,
                    payload={
                        "product": product_name,
                        "quantity": state.get("parsed", {}).get("quantity"),
                        "price": state.get("parsed", {}).get("price"),
                        "product_id": product_id,
                        "seller_phone": phone
                    }
                )
                topic = f"SELL_{product_name.upper()}_{zone_id}"
                count = self.a2a.broadcast_message(offer_msg, topic=topic)
                logger.info(f"📢 A2A Broadcast: Offre {product_name} diffusée à {count} agents.")
            except Exception as e:
                logger.warning(f"A2A Broadcast failed: {e}")

    # ════════════════════════════════════════════════════════════════
    # FALLBACK (sans LLM)
    # ════════════════════════════════════════════════════════════════

    def _fallback_response(
        self, intent: str, result: Dict, is_new: bool
    ) -> str:
        welcome = "🌾 Bienvenue sur AgriConnect ! Je suis votre assistant marketplace.\n\n" if is_new else ""

        if result.get("error"):
            return f"{welcome}⚠️ {result['error']}"

        if intent == "REGISTER_STOCK":
            return self._fallback_register_stock(result, welcome)
        if intent == "SELL_PRODUCT":
            return self._fallback_sell_product(result, welcome)
        if intent == "CHECK_STOCK":
            return self._fallback_check_stock(result, welcome)
        if intent == "FIND_BUYERS":
            return self._fallback_find_buyers(result, welcome)
        if intent == "HELP":
            return self._fallback_help(welcome)

        return f"{welcome}Action effectuée. Que souhaitez-vous faire ensuite ?"

    def _fallback_register_stock(self, result: Dict, welcome: str) -> str:
        return (
            f"{welcome}✅ Stock enregistré !\n"
            f"📦 {result.get('item_name')} : +{result.get('added_kg', 0):.0f} kg\n"
            f"Total en stock : {result.get('new_total_kg', 0):.0f} kg"
        )

    def _fallback_sell_product(self, result: Dict, welcome: str) -> str:
        return (
            f"{welcome}🛒 Produit mis en vente !\n"
            f"📦 {result.get('name')} — {result.get('price_fcfa', 0):.0f} FCFA/kg\n"
            f"Code : {result.get('short_code')}"
        )

    def _fallback_check_stock(self, result: Dict, welcome: str) -> str:
        stocks = result.get("stocks", [])
        if not stocks:
            return f"{welcome}📦 Votre stock est vide. Dites-moi ce que vous avez récolté !"
        lines = [f"  • {s.get('item_name')} : {s.get('quantity', 0):.0f} kg" for s in stocks]
        return f"{welcome}📦 **Votre stock :**\n" + "\n".join(lines)

    def _fallback_find_buyers(self, result: Dict, welcome: str) -> str:
        buyers = result.get("buyers", [])
        if not buyers:
            return f"{welcome}🔍 Aucun acheteur trouvé pour le moment. Je crée une alerte."
        lines = [f"  • {b.get('buyer_phone')} ({b.get('zone_name', '')})" for b in buyers]
        return f"{welcome}🎯 Acheteurs intéressés :\n" + "\n".join(lines)

    def _fallback_help(self, welcome: str) -> str:
        return (
            f"{welcome}🌾 Je peux vous aider à :\n"
            "  📦 Enregistrer votre stock (ex: 'J'ai 10 sacs de maïs')\n"
            "  🛒 Mettre en vente (ex: 'Je vends du sorgho à 250 FCFA/kg')\n"
            "  🔍 Trouver des acheteurs (ex: 'Qui cherche du mil ?')\n"
            "  📊 Voir votre stock (ex: 'Mon stock')\n"
            "  🛍️ Commander (ex: 'Je cherche du riz')\n"
        )

    # ════════════════════════════════════════════════════════════════
    # BUILD — Compilation du graphe LangGraph
    # ════════════════════════════════════════════════════════════════

    def build(self):
        workflow = StateGraph(MarketplaceState)

        workflow.add_node("identify_user", self.identify_user_node)
        workflow.add_node("parse_intent", self.parse_intent_node)
        workflow.add_node("execute_action", self.execute_action_node)
        workflow.add_node("confirm", self.confirm_node)
        workflow.add_node("match_check", self.match_check_node)

        workflow.set_entry_point("identify_user")

        def _route_after_identify(state: MarketplaceState) -> str:
            if state.get("status") == "ERROR":
                return END
            return "parse_intent"

        def _route_after_execute(state: MarketplaceState) -> str:
            """Short-circuit to END when in DEGRADED_MODE; else normal confirm."""
            if state.get("degraded_mode") or state.get("status") == "DEGRADED_MODE":
                return END
            return "confirm"

        workflow.add_conditional_edges("identify_user", _route_after_identify)
        workflow.add_edge("parse_intent", "execute_action")
        workflow.add_conditional_edges(
            "execute_action",
            _route_after_execute,
            {END: END, "confirm": "confirm"},
        )
        workflow.add_edge("confirm", "match_check")
        workflow.add_edge("match_check", END)

        return workflow.compile()


# ── Test standalone ──────────────────────────────────────────────
if __name__ == "__main__":
    agent = MarketplaceAgent()
    app = agent.build()

    test_cases = [
        {
            "name": "ENREGISTRER STOCK",
            "query": "J'ai récolté 15 sacs de maïs",
            "phone": "+22670000001",
        },
        {
            "name": "METTRE EN VENTE",
            "query": "Je veux vendre mon sorgho à 250 le kilo, j'en ai 5 sacs",
            "phone": "+22670000001",
        },
        {
            "name": "CHERCHER ACHETEUR",
            "query": "Qui cherche du mil dans ma zone ?",
            "phone": "+22670000001",
        },
        {
            "name": "AIDE",
            "query": "Comment ça marche ?",
            "phone": "+22670000002",
        },
    ]

    print("🚀 Tests MarketplaceAgent...\n")
    for case in test_cases:
        print(f"--- {case['name']} ---")
        print(f"Query: {case['query']}")

        initial = {
            "user_query": case["query"],
            "user_phone": case["phone"],
            "zone_id": None,
            "warnings": [],
        }

        try:
            result = app.invoke(initial)
            print(f"Intent: {result.get('intent')}")
            print(f"Réponse:\n{result.get('final_response')}\n")
        except Exception as e:
            print(f"❌ Erreur : {e}\n")
        print("-" * 50)
