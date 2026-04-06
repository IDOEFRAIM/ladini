from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger("AgriConnect.ValidationService")

class ValidationService:
    """Encapsulates business rules for market transactions."""

    @staticmethod
    def validate_product(product: str | None) -> List[str]:
        missing = []
        if not product:
            missing.append("produit (maïs, sorgho...)")
        return missing

    def validate_quantity(self, quantity: Any, intent: str, unit: str = "kg") -> tuple[float | None, List[str], List[str]]:
        """Validate and normalize quantity.
        Returns: (normalized_value, errors, missing_fields)
        """
        errors = []
        missing = []
        
        # Quantity is optional for CREATE_PRODUCT (catalog mode) but mandatory for selling stock
        if intent == "CREATE_PRODUCT":
            try:
                val = float(quantity) if quantity is not None else 0.0
                return val, errors, missing
            except:
                return 0.0, errors, missing # Default to 0 for catalog

        if quantity in (None, ""):
            missing.append("quantité")
            return None, errors, missing

        try:
            val = float(quantity)
            if val <= 0:
                errors.append("Quantité doit être positive.")
            
            # Simple unit normalization (could be extracted to a UnitService)
            factor = 1.0
            u = unit.lower()
            if u in ("t", "tonne", "tonnes"): factor = 1000.0
            elif u in ("sac", "sacs"): factor = 100.0 # Heuristic default
            
            return val * factor, errors, missing
        except (ValueError, TypeError):
            errors.append("Quantité invalide (nombre attendu).")
            return None, errors, missing

    def validate_location(self, location: str | None, intent: str) -> tuple[List[str], List[str]]:
        """Returns (missing_fields, warnings)."""
        missing = []
        warnings = []
        
        # Location mandatory for selling/buying, optional for pure catalog creation?
        # Actually in AgriConnect creation usually implies location or producer address.
        if not location and intent != "CREATE_PRODUCT":
             missing.append("lieu")
        
        # Check against known locations if list available (omitted for now to keep it simple/stateless)
        return missing, warnings

    def validate_price(self, price: Any, intent: str) -> tuple[float | None, List[str], List[str]]:
        """Returns (normalized_price, errors, missing)."""
        errors = []
        missing = []
        
        # Price mandatory for sales and catalog
        if price in (None, ""):
             missing.append("prix")
             return None, errors, missing
        
        try:
            val = float(price)
            if val < 0:
                errors.append("Prix ne peut pas être négatif.")
            return val, errors, missing
        except:
            errors.append("Prix invalide.")
            return None, errors, missing

    def validate_request(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Orchestrates validation for a full request state."""
        intent = state.get("intent")
        updates = {"validation_errors": [], "missing_fields": []}
        
        # Skip validation for non-transaction intents
        if intent not in ["REGISTER_SURPLUS", "SELL_OFFER", "BUY_OFFER", "CREATE_PRODUCT"]:
            return updates

        # Product
        missing = self.validate_product(state.get("product"))
        updates["missing_fields"].extend(missing)

        # Quantity
        norm_qty, errs, miss = self.validate_quantity(state.get("quantity_mentioned"), intent, state.get("unit_mentioned", "kg"))
        if norm_qty is not None:
            updates["normalized_quantity_kg"] = norm_qty
        updates["validation_errors"].extend(errs)
        updates["missing_fields"].extend(miss)

        # Price
        norm_price, errs, miss = self.validate_price(state.get("price_mentioned"), intent)
        if norm_price is not None:
             # Store normalized price back if needed, or just update state in caller
             pass
        updates["validation_errors"].extend(errs)
        # Determine if price is truly missing based on intent rules
        # (Already handled in validate_price but double check consistency)
        price_val = state.get("price_mentioned")
        if intent in ["REGISTER_SURPLUS", "CREATE_PRODUCT"] and price_val is None:
             pass # Already added to missing because validate_price returned missing

        updates["missing_fields"].extend(miss)

        # Location
        miss, warns = self.validate_location(state.get("location"), intent)
        updates["missing_fields"].extend(miss)
        if warns:
            updates["validation_warnings"] = warns # New field

        return updates