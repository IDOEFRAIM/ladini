from __future__ import annotations


def normalize_role(role: str | None) -> str:
    """Normalisation STRICTE (2026-09-08, refonte double-rôle) : une valeur
    non reconnue retourne "UNKNOWN", jamais un défaut silencieux vers
    "PRODUCER". Avant ce correctif, toute valeur absente/inattendue
    (`None`, `""`, une faute de frappe, un rôle futur pas encore mappé)
    devenait silencieusement PRODUCER — un mensonge structurel sur
    l'identité de l'utilisateur, alors que le rôle de profil ne doit de
    toute façon plus décider du domaine métier courant (voir
    `core/router.py::_goal_domain`, résolu PAR GOAL, pas par rôle).
    Les appelants qui ont besoin d'UNE valeur concrète (ex: sélection du
    graphe compilé) doivent explicitement décider de leur propre repli —
    ce n'est plus la responsabilité de cette fonction de le deviner.
    """
    role_up = str(role or "").upper().strip()
    if role_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
        return "BUYER"
    if role_up in {"PRODUCER", "PRODUCTEUR", "PRODUCTRICE"}:
        return "PRODUCER"
    return "UNKNOWN"


__all__ = [
    "normalize_role",
]
