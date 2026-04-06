import hashlib
import json
from typing import Any, Dict, List, Optional

class TransactionService:
    """Handles transaction payload construction, hashing, and UI proposal generation."""

    @staticmethod
    def construct_payload(intent: str, state: Dict[str, Any], normalized_updates: Dict[str, Any]) -> Dict[str, Any]:
        """Builds the normalized transaction payload from state and updates."""
        
        # Extract normalized values or fall back to raw state
        qty = normalized_updates.get("normalized_quantity_kg") if normalized_updates.get("normalized_quantity_kg") is not None else state.get("quantity_mentioned", 0)
        
        payload = {
            "action_type": intent,
            "product": state.get("product"),
            "quantity": qty,
            "price": state.get("price_mentioned"),
            "location": state.get("location"),
            "unit": state.get("unit_mentioned", "kg"),
            # User ID handled by caller/repo context usually, but good to include if present
            "user_id": state.get("user_profile", {}).get("user_id")
        }
        return payload

    @staticmethod
    def create_proposal(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Generates the UI proposal card structure."""
        intent = payload.get("action_type")
        fields = []
        display_key_title = ""
        tool_name = ""
        
        if intent == "CREATE_PRODUCT":
            tool_name = "create_product"
            display_key_title = "Confirmation d'ajout au catalogue"
            # For catalog creation, quantity and location are optional/secondary
            price_disp = payload.get('price') if payload.get('price') is not None else 0
            fields = [
                    {"label": "Produit", "value": str(payload.get("product"))},
                    {"label": "Prix Catalogue", "value": f"{price_disp} FCFA"},
            ]
            if payload.get("quantity", 0) and float(payload.get("quantity", 0)) > 0:
                    fields.append({"label": "Stock Initial", "value": f"{payload.get('quantity')} kg"})
        else:
            tool_name = "register_surplus_offer"
            display_key_title = "Confirmation de mise en vente"
            fields = [
                    {"label": "Produit", "value": str(payload.get("product"))},
                    {"label": "Quantité", "value": f"{payload.get('quantity')} kg"},
                    {"label": "Prix", "value": f"{payload.get('price', 'Non précisé')} FCFA"},
                    {"label": "Lieu", "value": str(payload.get("location"))},
            ]
        
        return {
            "type": "proposal",
            "tool_name": tool_name,
            "display_card": {
                "title": display_key_title,
                "fields": fields
            },
            "payload": payload
        }
    
    @staticmethod
    def build_transaction_hash(payload: Dict[str, Any]) -> str:
        """Create a unique hash for idempotent handling."""
        try:
            # Sort keys to ensure consistent hash
            s = json.dumps(payload, sort_keys=True, default=str)
            return hashlib.md5(s.encode()).hexdigest()
        except Exception:
            return ""
