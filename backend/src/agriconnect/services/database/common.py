"""Shared helpers for the database service package.

Provides validation utilities, limit clamping, UUID generation,
and DDL statements for performance indexes.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from agriconnect.domain.models import _uuid4

logger = getLogger("agriconnect.services.database")


def _uuid() -> str:
	"""Return a new UUID4 string."""
	return _uuid4()


import re
from typing import Any, Optional

def clean_text(value: Any, field: str="efraaaaa", *, required: bool = False, max_length: int = 255) -> Optional[str]:
    """Nettoyage générique pour les chaînes de caractères."""
    if value is None:
        if required:
            raise ValueError(f"{field} est obligatoire")
        return None
    
    # Conversion en string et nettoyage des espaces blancs extrêmes
    text = str(value).strip()
    
    if not text:
        if required:
            raise ValueError(f"{field} ne peut pas être vide")
        return None
        
    return text[:max_length]

def normalize_phone(phone: Any, required: bool = True) -> str:
    """
    Normalisation stricte des numéros de téléphone.
    Supprime les espaces, tirets, parenthèses et points.
    Conserve le '+' initial si présent.
    """
    if phone is None:
        if required:
            raise ValueError("Le numéro de téléphone est obligatoire")
        return ""
    
    phone_str = str(phone).strip()
    
    # On ne garde que les chiffres et le symbole '+'
    # Utile pour contrer les saisies type "+226 70 00 00 00" -> "+22670000000"
    normalized = re.sub(r"[^\d+]", "", phone_str)
    
    if required and not normalized:
        raise ValueError("Numéro de téléphone invalide")
        
    return normalized

def normalize_uuid(uuid_val: Any) -> Optional[str]:
    """Assure qu'un UUID est bien au format string propre pour SQL."""
    if not uuid_val:
        return None
    return str(uuid_val).strip().lower()


def positive_float(value: Any, field: str, *, allow_zero: bool = False) -> float:
	try:
		number = float(value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{field} must be numeric") from exc
	if number < 0 or (number == 0 and not allow_zero):
		raise ValueError(f"{field} must be positive")
	return number


def clamp_limit(value: Any, default: int = 20, maximum: int = 100) -> int:
	try:
		limit = int(value)
	except (TypeError, ValueError):
		limit = default
	return max(1, min(limit, maximum))


PERFORMANCE_INDEX_DDL = (
	# Auth / Users
	"CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone)",

	# Producers / Marketplace
	"CREATE INDEX IF NOT EXISTS ix_producers_user_id ON producers (user_id)",
	"CREATE INDEX IF NOT EXISTS ix_producers_zone_id ON producers (zone_id)",

	# Farms
	"CREATE INDEX IF NOT EXISTS ix_farms_producer_id ON farms (producer_id)",
	"CREATE INDEX IF NOT EXISTS ix_farms_zone_idx ON farms (zone_id)",

	# Warehouses
	"CREATE INDEX IF NOT EXISTS ix_warehouses_zone_idx ON warehouses (zone_id)",

	# Products
	"CREATE INDEX IF NOT EXISTS ix_products_producer_idx ON products (producer_id)",
	"CREATE INDEX IF NOT EXISTS ix_products_category_idx ON products (category_label)",
	"CREATE INDEX IF NOT EXISTS ix_products_subcategory_idx ON products (sub_category_id)",
	"CREATE INDEX IF NOT EXISTS ix_products_price_idx ON products (price)",
	"CREATE INDEX IF NOT EXISTS ix_products_created_idx ON products (created_at)",
	"CREATE INDEX IF NOT EXISTS ix_products_name_trgm ON products USING gin (name gin_trgm_ops)",

	# Clients
	"CREATE INDEX IF NOT EXISTS ix_clients_phone_idx ON clients (phone)",
	"CREATE INDEX IF NOT EXISTS ix_clients_producer_idx ON clients (producer_id)",

	# Conversations / Agent actions / Telemetry
	"CREATE INDEX IF NOT EXISTS ix_conversations_user_idx ON conversations (user_id)",
	"CREATE INDEX IF NOT EXISTS ix_conversations_agent_idx ON conversations (agent_type)",
	"CREATE INDEX IF NOT EXISTS ix_agent_actions_status_idx ON agent_actions (status)",
	"CREATE INDEX IF NOT EXISTS ix_agent_actions_batch_idx ON agent_actions (batch_id)",

	# Trust & Anomalies
	"CREATE INDEX IF NOT EXISTS ix_trust_scores_user_id ON trust_scores (user_id)",
	"CREATE INDEX IF NOT EXISTS ix_anomalies_zone_idx ON anomalies (zone_id)",
)


__all__ = [
	"_uuid",
	"logger",
	"clean_text",
	"positive_float",
	"clamp_limit",
	"PERFORMANCE_INDEX_DDL",
]
