"""
MarketplaceAgent v3 — Agent conversationnel Agribusiness (LangGraph async).

Gère les interactions agriculteurs (WhatsApp / Web) :
  • Identification par téléphone (auto-onboarding)
  • Gestion stock, dépenses, cycles de culture
  • Mise en vente (produits) avec détection prix suspect
  • Matching acheteur ↔ vendeur par zone (DB + enchères)
  • Commandes
  • Dashboard producteur
  • Enchères (appels d'offres)

Architecture : graphe LangGraph async
  IDENTIFY → PARSE → VALIDATE → EXECUTE → CONFIRM → MATCH
  (VALIDATE inclut price-check, financial safety + trust-score)
"""

import json
import logging
import re
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph

from agriconnect.graphs.prompts import MARKETPLACE_SYSTEM_PROMPT
from agriconnect.tools.marketplace_v3 import MarketplaceToolV3
from agriconnect.tools.intelligence import IntelligenceTool
from agriconnect.services.database.database_service import AgriDatabaseService
from agriconnect.protocols.mcp.infrastructure import runtime
from agriconnect.protocols.mcp.client import AgriMCPClient

# Protocols
from agriconnect.protocols.mcp import MCPDatabaseServer

logger = logging.getLogger("Agent.Marketplace.v3")


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
    trust_score: Optional[Dict[str, Any]]
    # ── Intent parsing
    intent: str
    parsed: Dict[str, Any]
    # ── Validation
    price_check: Optional[Dict[str, Any]]
    validation_warnings: List[str]
    # ── Execution
    action_result: Dict[str, Any]
    transaction_payload: Optional[Dict[str, Any]]
    # ── Matching
    matches: List[Dict[str, Any]]
    # ── Output
    final_response: str
    agri_response: Optional[Dict[str, Any]]
    status: str
    warnings: List[str]
    errors: List[str]
    # ── Loop guards
    retry_counter: int
    degraded_mode: bool
    # ── Financial safety
    requires_human_review: bool
    # ── HITL / handoff flags
    requires_human: bool
    handoff_to: str
    clarification_needed: str


# Maximum execute-action retries before activating degraded mode
_MAX_RETRIES: int = 2
# Financial transactions above this amount require explicit user confirmation
_FINANCIAL_THRESHOLD_FCFA: int = 100_000


# ── Intents reconnus (enrichis) ─────────────────────────────────
INTENTS = [
    "REGISTER_STOCK",
    "SELL_PRODUCT",
    "CHECK_STOCK",
    "CHECK_ORDERS",
    "UPDATE_STOCK",
    "REMOVE_STOCK",
    "FIND_BUYERS",
    "FIND_PRODUCTS",
    "CREATE_ORDER",
    "CHECK_AUCTIONS",      # Nouveau : enchères
    "PLACE_BID",            # Nouveau : soumettre offre
    "ADD_EXPENSE",          # Nouveau : dépenses
    "CHECK_EXPENSES",       # Nouveau : voir dépenses
    "DASHBOARD",            # Nouveau : tableau de bord
    "REGISTER_CROP_CYCLE",  # Nouveau : cycle culture
    "CHECK_PRICE",          # Nouveau : prix de référence
    "MANAGE_CLIENTS",       # Nouveau : clients
    "HELP",
]

from agriconnect.agents.base import BaseAgent


class MarketplaceAgentV3(BaseAgent):
    """
    Agent agribusiness v3 — async, multi-schema, rich features.
    Sous-graphe LangGraph appelé par l'orchestrateur.
    """

    def __init__(
        self, llm_client=None,
        mcp_session=None,
        db_service: AgriDatabaseService = None,
        mcp_client: AgriMCPClient = None,
    ):
        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"

        # Shield injected by the orchestrator (Host-Centric pattern)
        self.mcp_session = mcp_session
        # Backward-compat aliases for nodes that reference self.mcp_db / mcp_host
        self.mcp_db = mcp_session
        self.mcp_host = None  # host lives in orchestrator only
        self._raw_mcp = None

        # Prefer the centralized runtime DB service. If a specific
        # `db_service` is provided, use it (useful for tests). Otherwise
        # use the global `runtime.db` instance, unless `mcp_client` is used.
        svc = db_service or runtime.db
        
        # If mcp_client provided, we don't necessarily need a local service instance
        if not mcp_client and not svc:
            svc = AgriDatabaseService()

        # Pass mcp_client to tool wrapper
        self.tool = MarketplaceToolV3(db_service=svc, mcp_client=mcp_client)
        self.intel = IntelligenceTool(db_service=svc)

        try:
            from agriconnect.rag.components import get_groq_sdk
            self.llm = llm_client if llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("Impossible d'initialiser le LLM Marketplace : %s", exc)
            self.llm = None

    # ════════════════════════════════════════════════════════════════
    # NOEUD 1 : IDENTIFICATION
    # ════════════════════════════════════════════════════════════════

    async def identify_user_node(self, state: MarketplaceState) -> MarketplaceState:
        """Identifie l'utilisateur par téléphone. Charge profil + ferme + trust score."""
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
            user = await self.tool.identify_or_create_user(phone, zone_id=zone_id)
            state["user_profile"] = user
            state["producer_id"] = user.get("producer_id")

            if user.get("is_new"):
                state["warnings"] = ["NOUVEAU_UTILISATEUR"]
                logger.info("Nouvel agriculteur onboardé : %s", phone)
            else:
                state["warnings"] = []

            # Récupérer la ferme
            if state["producer_id"]:
                farm = await self.tool.get_or_create_farm(state["producer_id"], zone_id=zone_id)
                state["farm_id"] = farm["id"]

            # Charger le trust score
            trust = await self.intel.get_trust_score(user["id"])
            state["trust_score"] = trust

            state["status"] = "IDENTIFIED"
        except Exception as e:
            logger.error("Identification erreur : %s", e)
            state["status"] = "ERROR"
            state["final_response"] = "Erreur d'identification. Réessayez."

        return state

    # ════════════════════════════════════════════════════════════════
    # NOEUD 2 : PARSE INTENT
    # ════════════════════════════════════════════════════════════════

    async def parse_intent_node(self, state: MarketplaceState) -> MarketplaceState:
        """Analyse l'intention et extrait les entités."""
        state = dict(state)
        if state.get("status") == "ERROR":
            return state

        query = state.get("user_query", "")
        if not query:
            state["intent"] = "HELP"
            state["parsed"] = {}
            return state

        intents_str = "|".join(f'"{i}"' for i in INTENTS)
        prompt = (
            "Tu es un assistant agricole. Analyse le message de l'agriculteur.\n\n"
            f"Message : {query}\n\n"
            "Extrais les informations au format JSON strict :\n"
            "{{\n"
            f'  "intent": {intents_str},\n'
            '  "product": "nom du produit ou null",\n'
            '  "quantity": number ou null,\n'
            '  "unit": "sac"|"tine"|"plat"|"kg"|"tonne" ou null,\n'
            '  "price": number ou null,\n'
            '  "category": "Céréales"|"Légumineuses"|"Légumes"|"Fruits"|"Tubercules"|"Autres" ou null,\n'
            '  "description": "détails supplémentaires ou null",\n'
            '  "expense_label": "libellé dépense ou null",\n'
            '  "expense_amount": number ou null,\n'
            '  "expense_category": "SEEDS"|"FERTILIZER"|"LABOR"|"TRANSPORT"|"EQUIPMENT"|"OTHER" ou null\n'
            "}}\n\n"
            "Exemples :\n"
            '- "J\'ai 10 sacs de maïs" → intent=REGISTER_STOCK, product=maïs, quantity=10, unit=sac\n'
            '- "Je veux vendre mon sorgho à 250 le kg" → intent=SELL_PRODUCT, product=sorgho, price=250\n'
            '- "Combien j\'ai en stock ?" → intent=CHECK_STOCK\n'
            '- "Qui cherche du mil ?" → intent=FIND_BUYERS, product=mil\n'
            '- "Je cherche du riz" → intent=FIND_PRODUCTS, product=riz\n'
            '- "Mon tableau de bord" → intent=DASHBOARD\n'
            '- "J\'ai dépensé 5000 en engrais" → intent=ADD_EXPENSE, expense_label=engrais, expense_amount=5000, expense_category=FERTILIZER\n'
            '- "Voir les enchères" → intent=CHECK_AUCTIONS\n'
            '- "Quel est le prix du maïs" → intent=CHECK_PRICE, product=maïs\n'
            '- "Mes dépenses" → intent=CHECK_EXPENSES\n'
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

        # Financial safety guardrail
        price = parsed.get("price") or 0
        qty = parsed.get("quantity") or 0
        estimated_fcfa = float(price) * float(qty)
        if estimated_fcfa > _FINANCIAL_THRESHOLD_FCFA:
            state = self.require_human(
                state,
                reason=f"Montant estimé {estimated_fcfa:,.0f} FCFA — confirmation requise.",
            )
            state["warnings"] = list(state.get("warnings", []))
            state["warnings"].append(
                f"FINANCIAL_REVIEW_REQUIRED: {estimated_fcfa:,.0f} FCFA"
            )
        else:
            state["requires_human"] = False

        return state

    # ════════════════════════════════════════════════════════════════
    # NOEUD 3 : VALIDATION (prix, confiance, anomalies)
    # ════════════════════════════════════════════════════════════════

    async def validate_node(self, state: MarketplaceState) -> MarketplaceState:
        """Valide l'action avant exécution : prix suspect, trust score bas."""
        state = dict(state)
        if state.get("status") == "ERROR":
            return state

        state["validation_warnings"] = []
        intent = state.get("intent")
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")

        # Prix suspect ?
        if intent in ("SELL_PRODUCT", "CREATE_ORDER") and parsed.get("price") and parsed.get("product"):
            try:
                check = await self.tool.check_price_anomaly(
                    product_name=parsed["product"],
                    proposed_price=float(parsed["price"]),
                    zone_id=zone_id or "",
                )
                state["price_check"] = check
                if check.get("is_anomaly"):
                    state["validation_warnings"].append(
                        f"PRIX_SUSPECT: {check['reason']}"
                    )
                    # Signaler l'anomalie
                    if zone_id:
                        await self.intel.report_price_anomaly(
                            zone_id=zone_id,
                            product_name=parsed["product"],
                            proposed_price=float(parsed["price"]),
                            reference_price=check.get("reference_price", 0),
                            producer_phone=state.get("user_phone"),
                        )
            except Exception as e:
                logger.warning("Price check failed: %s", e)

        # Trust score bas ?
        trust = state.get("trust_score")
        if trust and trust.get("global_score", 1) < 0.3:
            state["validation_warnings"].append(
                f"LOW_TRUST: Score de confiance {trust['global_score']:.2f}/1.0"
            )

        state["status"] = "VALIDATED"
        return state

    # ════════════════════════════════════════════════════════════════
    # NOEUD 4 : EXECUTION
    # ════════════════════════════════════════════════════════════════

    async def execute_action_node(self, state: MarketplaceState) -> MarketplaceState:
        """Exécute l'action CRUD correspondant à l'intention."""
        state = dict(state)
        if state.get("status") == "ERROR":
            return state

        # Degraded-mode loop guard
        retry_counter = int(state.get("retry_counter", 0))
        if retry_counter > _MAX_RETRIES:
            state["degraded_mode"] = True
            state["status"] = "DEGRADED_MODE"
            state["final_response"] = (
                "Le service rencontre des difficultés temporaires. "
                "Votre demande a été enregistrée et sera traitée dès que possible."
            )
            return state
        state["retry_counter"] = retry_counter + 1

        intent = state.get("intent", "HELP")
        handler = self._get_intent_handler(intent)
        try:
            result = await handler(state)
        except Exception as e:
            logger.error("Execute action error (%s) : %s", intent, e)
            result = {"error": str(e)}

        # For write intents: store as pending transaction
        write_intents = {"SELL_PRODUCT", "CREATE_ORDER", "REGISTER_STOCK", "UPDATE_STOCK", "REMOVE_STOCK", "ADD_EXPENSE", "PLACE_BID"}
        if intent in write_intents and not result.get("error"):
            state["transaction_payload"] = {
                "intent": intent,
                "data": result,
                "parsed": state.get("parsed", {}),
                "status": "PENDING",
                "requires_human_review": state.get("requires_human", False),
            }
            state["action_result"] = result
        else:
            state["action_result"] = result
            state["transaction_payload"] = None

        # Log conversation + audit
        try:
            user_id = state.get("user_profile", {}).get("id")
            if user_id:
                await self.intel.log_conversation(
                    user_id=user_id,
                    query=state.get("user_query", ""),
                    response=json.dumps(result, default=str, ensure_ascii=False)[:500],
                    agent_type="marketplace",
                    zone_id=state.get("zone_id"),
                )
        except Exception:
            pass

        state["status"] = "EXECUTED"
        return state

    def _get_intent_handler(self, intent: str):
        return {
            "REGISTER_STOCK": self._handle_register_stock,
            "SELL_PRODUCT": self._handle_sell_product,
            "CHECK_STOCK": self._handle_check_stock,
            "CHECK_ORDERS": self._handle_check_orders,
            "UPDATE_STOCK": self._handle_update_stock,
            "REMOVE_STOCK": self._handle_remove_stock,
            "FIND_BUYERS": self._handle_find_buyers,
            "FIND_PRODUCTS": self._handle_find_products,
            "CREATE_ORDER": self._handle_create_order,
            "CHECK_AUCTIONS": self._handle_check_auctions,
            "PLACE_BID": self._handle_place_bid,
            "ADD_EXPENSE": self._handle_add_expense,
            "CHECK_EXPENSES": self._handle_check_expenses,
            "DASHBOARD": self._handle_dashboard,
            "REGISTER_CROP_CYCLE": self._handle_register_crop_cycle,
            "CHECK_PRICE": self._handle_check_price,
            "MANAGE_CLIENTS": self._handle_manage_clients,
            "HELP": self._handle_help,
        }.get(intent, self._handle_help)

    # ── Handlers ──────────────────────────────────────────────────

    async def _handle_register_stock(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        parsed = state.get("parsed", {})
        return await self.tool.add_stock(
            farm_id=farm_id,
            item_name=parsed.get("product", "produit"),
            quantity=parsed.get("quantity", 0),
            unit=parsed.get("unit", "kg"),
            reason=f"Déclaré via WhatsApp: {state.get('user_query', '')}",
        )

    async def _handle_sell_product(self, state: MarketplaceState) -> dict:
        producer_id = state.get("producer_id")
        if not producer_id:
            return {"error": "Aucun producteur associé."}
        parsed = state.get("parsed", {})
        return await self.tool.create_product(
            producer_id=producer_id,
            name=parsed.get("product", "produit"),
            price=parsed.get("price", 0),
            quantity_for_sale=parsed.get("quantity", 0),
            unit=parsed.get("unit", "kg"),
            category_label=parsed.get("category"),
            description=parsed.get("description"),
        )

    async def _handle_check_stock(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        stocks = await self.tool.get_stocks(farm_id)
        return {"stocks": stocks, "count": len(stocks)}

    async def _handle_check_orders(self, state: MarketplaceState) -> dict:
        phone = state.get("user_phone")
        orders = await self.tool.get_orders(buyer_phone=phone)
        # Aussi lister les produits en vente
        producer_id = state.get("producer_id")
        products = await self.tool.list_products(producer_id) if producer_id else []
        return {"orders": orders, "products_on_sale": products}

    async def _handle_update_stock(self, state: MarketplaceState) -> dict:
        return await self._handle_register_stock(state)

    async def _handle_remove_stock(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        parsed = state.get("parsed", {})
        return await self.tool.remove_stock(
            farm_id=farm_id,
            item_name=parsed.get("product", "produit"),
            quantity=parsed.get("quantity", 0),
            unit=parsed.get("unit", "kg"),
            reason=parsed.get("description", "Retrait"),
        )

    async def _handle_find_buyers(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        product = parsed.get("product", "")
        # Cherche enchères + commandes en attente
        buyers = await self.tool.find_buyers_for_product(product, zone_id)
        # Prix de référence
        ref_price = await self.tool.get_reference_price(product, zone_id or "") if product else None
        return {
            "buyers": buyers,
            "reference_price": ref_price,
            "count": len(buyers),
        }

    async def _handle_find_products(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        product = parsed.get("product", "")
        products = await self.tool.find_products_for_buyer(product, zone_id)
        return {"products_available": products, "count": len(products)}

    async def _handle_create_order(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        phone = state.get("user_phone", "")
        product_name = parsed.get("product", "")
        quantity = parsed.get("quantity", 1)

        available = await self.tool.find_products_for_buyer(product_name, zone_id)
        if available:
            best = available[0]
            return await self.tool.create_order(
                product_id=best["id"], quantity=quantity,
                buyer_phone=phone,
                buyer_name=state.get("user_profile", {}).get("name"),
                zone_id=zone_id,
            )
        return {"error": f"Aucun {product_name} disponible dans votre zone."}

    async def _handle_check_auctions(self, state: MarketplaceState) -> dict:
        zone_id = state.get("zone_id")
        auctions = await self.tool.get_open_auctions(zone_id)
        return {"auctions": auctions, "count": len(auctions)}

    async def _handle_place_bid(self, state: MarketplaceState) -> dict:
        producer_id = state.get("producer_id")
        if not producer_id:
            return {"error": "Aucun producteur associé."}
        parsed = state.get("parsed", {})
        auction_id = parsed.get("auction_id")
        price = parsed.get("price", 0)
        if not auction_id:
            return {"error": "Veuillez préciser l'enchère (auction_id)."}
        return await self.tool.place_bid(auction_id, producer_id, price)

    async def _handle_add_expense(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        parsed = state.get("parsed", {})
        label = parsed.get("expense_label") or parsed.get("description") or "Dépense"
        amount = parsed.get("expense_amount") or parsed.get("price") or 0
        category = parsed.get("expense_category", "OTHER")
        return await self.tool.add_expense(farm_id, label, float(amount), category)

    async def _handle_check_expenses(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        expenses = await self.tool.get_expenses(farm_id)
        summary = await self.tool.get_expense_summary(farm_id)
        return {"expenses": expenses, "summary": summary}

    async def _handle_dashboard(self, state: MarketplaceState) -> dict:
        producer_id = state.get("producer_id")
        if not producer_id:
            return {"error": "Aucun producteur associé."}
        return await self.tool.get_dashboard(producer_id)

    async def _handle_register_crop_cycle(self, state: MarketplaceState) -> dict:
        farm_id = state.get("farm_id")
        if not farm_id:
            return {"error": "Aucune ferme associée."}
        parsed = state.get("parsed", {})
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        return await self.tool.create_crop_cycle(
            farm_id=farm_id,
            crop_type=parsed.get("product", "inconnu"),
            area_size=parsed.get("quantity", 1),
            planted_at=now,
            expected_harvest_date=now + timedelta(days=120),
            expected_yield=0,
        )

    async def _handle_check_price(self, state: MarketplaceState) -> dict:
        parsed = state.get("parsed", {})
        zone_id = state.get("zone_id")
        product = parsed.get("product", "")
        ref = await self.tool.get_reference_price(product, zone_id or "")
        # Aussi chercher prix pratiqués sur la plateforme
        market = await self.tool.search_products(product, zone_id, limit=5) if product else []
        return {
            "reference_price": ref,
            "market_prices": market,
            "product": product,
        }

    async def _handle_manage_clients(self, state: MarketplaceState) -> dict:
        producer_id = state.get("producer_id")
        if not producer_id:
            return {"error": "Aucun producteur associé."}
        clients = await self.tool.get_clients(producer_id)
        return {"clients": clients, "count": len(clients)}

    async def _handle_help(self, state: MarketplaceState) -> dict:
        return {"help": True}

    # ════════════════════════════════════════════════════════════════
    # NOEUD 5 : CONFIRM (génération réponse LLM + AG-UI)
    # ════════════════════════════════════════════════════════════════

    async def confirm_node(self, state: MarketplaceState) -> MarketplaceState:
        """Génère la réponse conversationnelle (texte + AG-UI)."""
        state = dict(state)
        if state.get("status") == "ERROR":
            return state

        intent = state.get("intent", "HELP")
        result = state.get("action_result", {})
        parsed = state.get("parsed", {})
        user = state.get("user_profile", {})
        is_new = "NOUVEAU_UTILISATEUR" in state.get("warnings", [])
        needs_review = state.get("requires_human", False)
        tx_payload = state.get("transaction_payload")
        validation_warnings = state.get("validation_warnings", [])
        trust = state.get("trust_score")

        context = {
            "intent": intent,
            "result": result,
            "parsed": parsed,
            "user_name": user.get("name", "Agriculteur"),
            "is_new_user": is_new,
            "requires_human": needs_review,
            "transaction_payload": tx_payload,
            "validation_warnings": validation_warnings,
            "trust_score": trust.get("global_score") if trust else None,
        }

        review_instruction = ""
        if needs_review:
            review_instruction = (
                "\nIMPORTANT : Cette transaction dépasse 100 000 FCFA. "
                "Demande explicitement à l'agriculteur de confirmer avec OUI.\n"
            )
        if validation_warnings:
            review_instruction += (
                "\nATTENTION : " + " | ".join(validation_warnings) + "\n"
                "Signale ces avertissements à l'agriculteur de manière simple.\n"
            )

        prompt = (
            f"{MARKETPLACE_SYSTEM_PROMPT}\n\n"
            "CONTEXTE DE L'ACTION :\n"
            f"{json.dumps(context, ensure_ascii=False, default=str)}\n\n"
            f"Message original : {state.get('user_query', '')}\n\n"
            f"{review_instruction}"
            "Génère une réponse claire et chaleureuse en français simple.\n"
            "Si c'est un nouvel utilisateur, souhaite-lui la bienvenue.\n"
            "Confirme l'action réalisée avec les détails importants.\n"
            "Utilise des emojis pertinents.\n"
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
        # Build a simple MCP-friendly response (plain dict) instead of AG-UI
        resp: Dict[str, Any] = {
            "text": final_text,
            "agent": "marketplace",
            "actions": [],
            "cards": [],
            "suggested": [],
        }

        if needs_review:
            resp["actions"].append({
                "label": "OUI, confirmer la transaction",
                "type": "confirm",
                "payload": {"action": "commit_transaction", "tx": tx_payload},
            })
            resp["actions"].append({
                "label": "Non, annuler",
                "type": "cancel",
                "payload": {"action": "cancel_transaction"},
            })
        elif intent == "SELL_PRODUCT":
            resp["suggested"] = [
                {"id": "view_offers", "label": "Voir les offres"},
                {"id": "modify_price", "label": "Modifier prix"},
                {"id": "check_auctions", "label": "Voir enchères"},
            ]
        elif intent == "CHECK_STOCK":
            stocks = result.get("stocks", [])
            if stocks:
                stock_lines = "\n".join(
                    f"  {s.get('item_name')}: {s.get('quantity', 0):.0f} {s.get('unit', 'kg')}" for s in stocks
                )
                resp["cards"].append({"title": "Votre Stock", "body": stock_lines})
        elif intent == "DASHBOARD":
            if not result.get("error"):
                fin = result.get("financials", {})
                resp["cards"].append({
                    "title": "Finances",
                    "body": (
                        f"Revenus: {fin.get('total_revenue_fcfa', 0):,.0f} FCFA\n"
                        f"Dépenses: {fin.get('total_expenses_fcfa', 0):,.0f} FCFA\n"
                        f"Profit: {fin.get('profit_fcfa', 0):,.0f} FCFA"
                    ),
                })
        elif intent == "CHECK_AUCTIONS":
            auctions = result.get("auctions", [])
            if auctions:
                items = [
                    {"id": a.get("id", ""), "label": f"Enchère: {a.get('quantity', '')} — max {a.get('max_price_per_unit', '')} FCFA"}
                    for a in auctions[:5]
                ]
                resp["suggested"] = items

        state["agri_response"] = resp
        state["status"] = "CONFIRMED"
        return state

    # ════════════════════════════════════════════════════════════════
    # NOEUD 6 : MATCH CHECK
    # ════════════════════════════════════════════════════════════════

    async def match_check_node(self, state: MarketplaceState) -> MarketplaceState:
        """Vérifie les matchs acheteur/vendeur; diffusion inter-agent gérée par l'orchestrateur."""
        state = dict(state)
        intent = state.get("intent")

        if intent != "SELL_PRODUCT":
            state["status"] = "COMPLETED"
            return state

        result = state.get("action_result", {})
        product_name = state.get("parsed", {}).get("product", "")
        zone_id = state.get("zone_id")
        phone = state.get("user_phone", "")
        product_id = result.get("product_id")

        if product_name and product_id:
            # Local matching
            matches = await self.tool.auto_match(product_name, zone_id, phone, product_id)
            if matches:
                state["matches"] = matches
                match_msg = (
                    f"\n\n{len(matches)} acheteur(s) potentiel(s) "
                    f"pour du {product_name} dans votre zone :\n"
                )
                for m in matches:
                    if m.get("type") == "auction":
                        match_msg += f"  Enchère: max {m.get('max_price', '')} FCFA — deadline {m.get('deadline', '')}\n"
                    else:
                        match_msg += f"  Commande en attente: {m.get('buyer_phone', '')}\n"
                match_msg += "\nJe peux les mettre en contact avec vous. Voulez-vous ?"
                state["final_response"] = state.get("final_response", "") + match_msg

            # Inter-agent broadcasting is handled by the orchestrator; skip here
            logger.debug("Inter-agent broadcast skipped (node-level disabled) for product %s", product_name)

            # Points de confiance pour mise en vente
            try:
                user_id = state.get("user_profile", {}).get("id")
                if user_id:
                    await self.intel.update_trust_score(
                        user_id=user_id, agent_name="marketplace",
                        justification="Mise en vente de produit",
                        data_points={"product": product_name, "event": "product_listed"},
                        reliability_delta=0.01,
                    )
            except Exception:
                pass

            # Émettre un événement territorial
            try:
                if zone_id:
                    await self.intel.emit_market_movement(
                        zone_id=zone_id, product_name=product_name,
                        direction="SUPPLY",
                        details={"quantity": state.get("parsed", {}).get("quantity"), "price": state.get("parsed", {}).get("price")},
                    )
            except Exception:
                pass

        state["status"] = "COMPLETED"
        return state

    def _handle_inter_agent_broadcast(self, state):
        """No-op: inter-agent broadcasting disabled at node-level.

        Orchestrator or dedicated services should perform inter-agent
        broadcasts if required.
        """
        product_name = state.get("parsed", {}).get("product", "")
        logger.debug("Inter-agent broadcast skipped (node-level disabled) for product %s", product_name)

    # ════════════════════════════════════════════════════════════════
    # FALLBACK
    # ════════════════════════════════════════════════════════════════

    def _fallback_response(self, intent: str, result: Dict, is_new: bool) -> str:
        welcome = "Bienvenue sur AgriConnect ! Je suis votre assistant marketplace.\n\n" if is_new else ""
        if result.get("error"):
            return f"{welcome}{result['error']}"

        handlers = {
            "REGISTER_STOCK": lambda: (
                f"{welcome}Stock enregistré !\n"
                f"{result.get('item_name')} : +{result.get('added', 0):.0f} kg\n"
                f"Total : {result.get('new_total', 0):.0f} kg"
            ),
            "SELL_PRODUCT": lambda: (
                f"{welcome}Produit mis en vente !\n"
                f"{result.get('name')} — {result.get('price_fcfa', 0):.0f} FCFA/kg\n"
                f"Code : {result.get('short_code')}"
            ),
            "CHECK_STOCK": lambda: self._fallback_stock_list(result, welcome),
            "DASHBOARD": lambda: self._fallback_dashboard(result, welcome),
            "CHECK_EXPENSES": lambda: (
                f"{welcome}Dépenses:\n"
                f"Total: {result.get('summary', {}).get('grand_total', 0):,.0f} FCFA"
            ),
            "ADD_EXPENSE": lambda: (
                f"{welcome}Dépense enregistrée: {result.get('label')} — {result.get('amount', 0):,.0f} FCFA"
            ),
            "CHECK_AUCTIONS": lambda: (
                f"{welcome}{result.get('count', 0)} enchère(s) ouverte(s)."
            ),
            "HELP": lambda: self._fallback_help(welcome),
        }
        handler = handlers.get(intent, lambda: f"{welcome}Action effectuée.")
        return handler()

    def _fallback_stock_list(self, result, welcome):
        stocks = result.get("stocks", [])
        if not stocks:
            return f"{welcome}Votre stock est vide."
        lines = [f"  {s.get('item_name')}: {s.get('quantity', 0):.0f} kg" for s in stocks]
        return f"{welcome}Votre stock :\n" + "\n".join(lines)

    def _fallback_dashboard(self, result, welcome):
        if result.get("error"):
            return f"{welcome}{result['error']}"
        fin = result.get("financials", {})
        return (
            f"{welcome}Tableau de bord :\n"
            f"Revenus: {fin.get('total_revenue_fcfa', 0):,.0f} FCFA\n"
            f"Dépenses: {fin.get('total_expenses_fcfa', 0):,.0f} FCFA\n"
            f"Profit: {fin.get('profit_fcfa', 0):,.0f} FCFA\n"
            f"Clients: {result.get('clients_count', 0)}"
        )

    def _fallback_help(self, welcome):
        return (
            f"{welcome}Je peux vous aider à :\n"
            "  Enregistrer votre stock (ex: 'J'ai 10 sacs de maïs')\n"
            "  Mettre en vente (ex: 'Je vends du sorgho à 250 FCFA/kg')\n"
            "  Trouver des acheteurs (ex: 'Qui cherche du mil ?')\n"
            "  Voir votre stock (ex: 'Mon stock')\n"
            "  Commander (ex: 'Je cherche du riz')\n"
            "  Enregistrer dépenses (ex: 'J'ai dépensé 5000 en engrais')\n"
            "  Voir enchères (ex: 'Voir les enchères')\n"
            "  Tableau de bord (ex: 'Mon dashboard')\n"
            "  Prix du marché (ex: 'Quel est le prix du maïs ?')\n"
        )

    # ════════════════════════════════════════════════════════════════
    # BUILD — Compilation du graphe LangGraph
    # ════════════════════════════════════════════════════════════════

    def build(self):
        workflow = StateGraph(MarketplaceState)

        workflow.add_node("identify_user", self.identify_user_node)
        workflow.add_node("parse_intent", self.parse_intent_node)
        workflow.add_node("validate", self.validate_node)
        workflow.add_node("execute_action", self.execute_action_node)
        workflow.add_node("confirm", self.confirm_node)
        workflow.add_node("match_check", self.match_check_node)

        workflow.set_entry_point("identify_user")

        def _route_after_identify(state: MarketplaceState) -> str:
            if state.get("status") == "ERROR":
                return END
            return "parse_intent"

        def _route_after_execute(state: MarketplaceState) -> str:
            if state.get("degraded_mode") or state.get("status") == "DEGRADED_MODE":
                return END
            return "confirm"

        workflow.add_conditional_edges("identify_user", _route_after_identify)
        workflow.add_edge("parse_intent", "validate")
        workflow.add_edge("validate", "execute_action")
        workflow.add_conditional_edges(
            "execute_action",
            _route_after_execute,
            {END: END, "confirm": "confirm"},
        )
        workflow.add_edge("confirm", "match_check")
        workflow.add_edge("match_check", END)

        return workflow.compile()
