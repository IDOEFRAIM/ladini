"""Shared helpers for the database service package.

Provides validation utilities, limit clamping, UUID generation,
and DDL statements for performance indexes.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from agriconnect.domain.orm_base import _uuid4

logger = getLogger("agriconnect.services.database")


def _uuid() -> str:
	"""Return a new UUID4 string."""
	return _uuid4()


import re
from typing import Any, Optional

# Caractères de contrôle (hors tab/newline) : neutralisés silencieusement pour
# éviter qu'un payload agent bruité ne pollue les logs ou les colonnes texte.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: Any, field: str = "champ", *, required: bool = False, max_length: int = 255) -> Optional[str]:
    """Nettoyage générique pour les chaînes de caractères.

    Note sécurité : ne protège PAS contre l'injection SQL (inutile — toutes les
    requêtes passent par l'ORM paramétré). Sert à borner la longueur, retirer
    les caractères de contrôle et normaliser les espaces avant persistance/log.
    """
    if value is None:
        if required:
            raise ValueError(f"{field} est obligatoire")
        return None

    # Conversion en string, retrait des caractères de contrôle, trim.
    text = _CONTROL_CHARS_RE.sub("", str(value)).strip()

    if not text:
        if required:
            raise ValueError(f"{field} ne peut pas être vide")
        return None

    return text[:max_length]


def escape_like(term: str) -> str:
    """Échappe les métacaractères LIKE/ILIKE (`%`, `_`, `\\`) d'un terme LITTÉRAL.

    Sans ça, `Product.name.ilike(f"%{term}%")` avec `term="%"` matche TOUTE la
    table (sur-exposition + full scan = DoS applicatif), et `term="a_b"` traite
    `_` comme un joker. À utiliser sur tout terme de recherche libre injecté
    dans un pattern LIKE, combiné à `.ilike(pattern, escape="\\")`.

    NB : ce n'est PAS une protection anti-injection SQL (l'ORM paramètre déjà la
    valeur) mais une protection contre l'injection de MÉTACARACTÈRES LIKE.
    """
    if term is None:
        return ""
    return (
        str(term)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

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


# CORRECTIF AUDIT : les DDL étaient (a) NON qualifiés par schéma alors que les
# tables vivent dans auth/marketplace/governance/intelligence → échec silencieux
# (le try/except du dispatcher masquait l'erreur), et (b) référençaient
# `anomalies`, table INEXISTANTE. Qualifiés + extension pg_trgm garantie (requise
# par la recherche floue trigram, cf. search.py).
#
# Périmètre EXCLUSIF (chirurgical, cf. audit) — un index GIN trigram UNIQUEMENT
# sur les champs textuels descriptifs/catalogue à fort impact métier (tolérance
# aux fautes de frappe agricoles). AUCUN index trigram sur les champs
# structurels/d'isolation (phone, id, status, email...) : ces champs restent en
# égalité stricte, portée par leurs index B-tree usuels (déjà déclarés dans les
# `__table_args__` des modèles ORM, non dupliqués ici).
#
# Note : `auctions.title` n'existe PAS dans le schéma (`Auction` n'a pas de
# colonne de titre libre) — la recherche produit sur les enchères passe par
# `sub_categories.name` (déjà indexé ci-dessous), qui est le VRAI champ
# textuel interrogé par `auction.py` (resolve_sub_category, create_auction,
# get_auctions, get_price_recommendation).
PERFORMANCE_INDEX_DDL = (
	# Extension requise pour SIMILARITY / opérateur `%` / index GIN trigram.
	"CREATE EXTENSION IF NOT EXISTS pg_trgm",

	# products.name — recherche catalogue (get_public_products, search_products).
	"CREATE INDEX IF NOT EXISTS ix_products_name_trgm "
	"ON marketplace.products USING gin (name gin_trgm_ops)",
	# market_offers.product_label — recherche offres/préventes futures.
	"CREATE INDEX IF NOT EXISTS ix_market_offers_label_trgm "
	"ON marketplace.market_offers USING gin (product_label gin_trgm_ops)",
	# sub_categories.name — résolution produit pour enchères/appels d'offres.
	"CREATE INDEX IF NOT EXISTS ix_subcategories_name_trgm "
	"ON governance.sub_categories USING gin (name gin_trgm_ops)",
	# zones.name — résolution de secteur logistique (livraison, marché local).
	"CREATE INDEX IF NOT EXISTS ix_zones_name_trgm "
	"ON governance.zones USING gin (name gin_trgm_ops)",
)


__all__ = [
	"_uuid",
	"logger",
	"clean_text",
	"escape_like",
	"normalize_phone",
	"normalize_uuid",
	"positive_float",
	"clamp_limit",
	"PERFORMANCE_INDEX_DDL",
]
