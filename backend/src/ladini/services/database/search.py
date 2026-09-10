"""Recherche floue tolérante aux fautes de frappe (jargon agricole).

Axe ADAPTABILITÉ MÉTIER : l'audit a montré que la recherche produit
(`search_products`, `get_public_products`) utilisait `ILIKE %terme%` — un
substring STRICT. « tomte » ne matche pas « tomate » → l'agent est bloqué,
alors qu'un index GIN trigram (`pg_trgm`) existe déjà côté schéma mais n'est
exploité QUE pour les zones (`base.py::get_zone_by_name`).

Ce module généralise le pattern trigram déjà validé sur les zones, avec une
stratégie « haute sensibilité » : on élargit d'abord (similarité + substring),
l'agent trie ensuite. Mieux vaut un faux positif qu'un blocage.

Prérequis : extension `pg_trgm` + index GIN (voir `common.PERFORMANCE_INDEX_DDL`).
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, or_

from ladini.services.database.common import escape_like

# Seuil de similarité trigram (0-1). Postgres a 0.3 par défaut ; on descend un
# peu pour privilégier le rappel (jargon/fautes agricoles) — l'agent filtre.
DEFAULT_SIMILARITY_THRESHOLD = 0.22

# En dessous de 2 caractères, la similarité trigram n'a pas de sens (bruit) :
# on retombe sur un simple préfixe pour ne pas ramener toute la table.
_MIN_TRIGRAM_LEN = 2


def fuzzy_match(
    column: ColumnElement, term: str, *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> ColumnElement:
    """Prédicat de recherche floue sur `column` pour `term`.

    Combine (OR) trois signaux, du plus tolérant au plus strict :
      1. similarité trigram  `similarity(col, term) >= threshold`  (« tomte »→« tomate »)
      2. substring insensible casse/accents-partiels  `col ILIKE %term%` (wildcards ÉCHAPPÉS)
      3. préfixe  `col ILIKE term%`  (utile pour les termes très courts)

    Les métacaractères LIKE du terme sont échappés (défense contre le
    sur-matching `%`/`_`). Retourne une clause à passer à `.where(...)`.
    """
    cleaned = (term or "").strip()
    if not cleaned:
        # Terme vide/None → prédicat toujours faux (aucun résultat plutôt que
        # tout) : défense en profondeur, cohérent avec `similarity_rank`
        # ci-dessous qui applique la même garde.
        return func.coalesce(column, "") == "\x00__never__"

    safe = escape_like(cleaned)
    substring = column.ilike(f"%{safe}%", escape="\\")
    prefix = column.ilike(f"{safe}%", escape="\\")

    if len(cleaned) < _MIN_TRIGRAM_LEN:
        return or_(substring, prefix)

    trigram = func.similarity(column, cleaned) >= threshold
    return or_(trigram, substring, prefix)


def similarity_rank(column: ColumnElement, term: str):
    """Expression de tri par pertinence décroissante (à passer à `.order_by`).

    À utiliser conjointement avec `fuzzy_match` pour que les correspondances les
    plus proches remontent en tête — l'agent lit alors le résultat le plus
    probable en premier.
    """
    cleaned = (term or "").strip()
    if not cleaned:
        return func.now()  # ordre neutre si pas de terme
    return func.similarity(column, cleaned).desc()


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "fuzzy_match",
    "similarity_rank",
]
