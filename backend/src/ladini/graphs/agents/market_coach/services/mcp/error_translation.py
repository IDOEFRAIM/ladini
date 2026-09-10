"""MCP Error Translation — user-friendly French error messages.

Extracted from ``nodes/executor.py``.
"""

from __future__ import annotations

from typing import Dict

GENERIC_TECHNICAL_ERROR = (
    "Une erreur technique est survenue. Veuillez réessayer dans quelques instants. "
    "Si le problème persiste, contactez le support."
)

_MCP_ERROR_TRANSLATIONS: Dict[str, str] = {
    "not_found": "L'élément demandé n'a pas été trouvé. Vérifiez les données ou reformulez.",
    "duplicate": "Cet enregistrement existe déjà. Voulez-vous le modifier plutôt ?",
    "permission": "Vous n'avez pas les droits pour cette opération.",
    "invalid": "Les données envoyées ne sont pas valides. Vérifiez et réessayez.",
    "stock": "Problème lié au stock. Vérifiez vos quantités.",
    "closed": "Cette enchère ou offre est déjà clôturée.",
    "timeout": "Le service met trop de temps à répondre. Veuillez réessayer.",
    "connection": "Impossible de joindre le service. Veuillez réessayer dans quelques instants.",
    "unavailable": "Le service est temporairement indisponible. Veuillez réessayer.",
    # (2026-09-02, refonte GPS/erreurs) : `BusinessRuleException("... hors du
    # Burkina Faso.")` (services/database/auction.py::select_winning_bid,
    # buyer.py, escrow.py — geofencing en défense en profondeur) ne matchait
    # AUCUNE clé ci-dessus et retombait donc sur `GENERIC_TECHNICAL_ERROR` —
    # un rejet géographique explicite affiché comme une panne technique
    # ("réessayez"), qui ne changera jamais rien puisque le point reste hors
    # zone. Voir core/geofencing.py::OUT_OF_COUNTRY_MESSAGE (même famille de
    # message, contexte différent : ici après coup, sur la commande).
    "burkina faso": (
        "📍 Le point de livraison associé à cette commande est hors du "
        "Burkina Faso — notre service de livraison y est limité pour le "
        "moment."
    ),
    # (2026-09-03, idempotence MCP réelle) : `mcp_idempotency_store.py`
    # refuse une même clé réutilisée avec un payload différent — jamais
    # résolu silencieusement (une des deux tentatives a un contenu erroné,
    # l'utilisateur doit recommencer proprement plutôt que de risquer une
    # confusion entre deux transactions).
    "idempotency_conflict": (
        "Cette action semble avoir déjà été traitée avec des informations "
        "différentes — par prudence, elle n'a pas été rejouée. Veuillez "
        "recommencer votre demande."
    ),
    "idempotency_in_progress": (
        "Cette action est déjà en cours de traitement — merci de patienter "
        "un instant avant de réessayer."
    ),
}

# Produit absent du catalogue (ex: "Produit 'antilope' inconnu.") — ce n'est
# PAS une erreur technique, c'est un cas métier normal : LADINI n'a pas encore
# ce produit. Le message générique ("erreur technique, réessayez") était
# trompeur (rien ne change en réessayant). Clé composée ("produit" + "inconnu")
# pour ne jamais matcher "zone inconnue" / "statut inconnu" (autres messages
# sans rapport avec le catalogue produit).
_UNKNOWN_PRODUCT_MESSAGE = (
    "Ce produit n'est pas encore disponible sur LADINI — nous n'acceptons pas "
    "ce type de produit pour le moment. Si vous souhaitez qu'il soit ajouté au "
    "catalogue, contactez notre service client."
)

INFRA_ERROR_CODE = "infrastructure_unavailable"
BUSINESS_ERROR_CODE = "business_error"

_INFRA_KEYWORDS = frozenset(
    {
        "timeout",
        "connection",
        "unavailable",
        "unreachable",
        "refused",
        "reset",
        "eof",
        "broken pipe",
        "dns",
        "ssl",
        "tls",
    }
)


def classify_error(raw_error: str) -> str:
    lower = (raw_error or "").lower()
    if any(kw in lower for kw in _INFRA_KEYWORDS):
        return INFRA_ERROR_CODE
    return BUSINESS_ERROR_CODE


def translate_mcp_error(raw_error: str) -> str:
    lower = (raw_error or "").lower()
    if "produit" in lower and "inconnu" in lower:
        return _UNKNOWN_PRODUCT_MESSAGE
    for key, msg in _MCP_ERROR_TRANSLATIONS.items():
        if key in lower:
            return msg
    return GENERIC_TECHNICAL_ERROR


__all__ = [
    "GENERIC_TECHNICAL_ERROR",
    "translate_mcp_error",
    "classify_error",
    "INFRA_ERROR_CODE",
    "BUSINESS_ERROR_CODE",
]
