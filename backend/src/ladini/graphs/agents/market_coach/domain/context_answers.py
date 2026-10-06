"""Réponses aux QUESTIONS de contexte (« il livre ? », « c'est combien au total ? », « il reste combien ? », « lequel est moins cher ? »).

Principe : le modèle COMPREND la question (sujet, cible) ; il ne la RÉPOND jamais. La réponse est calculée ici, en Python, à partir de faits
que le système possède réellement (snapshot du menu, panier) — et, quand le fait n'existe pas, la réponse est « Je n'ai pas cette information »
(jamais un oui/non inventé : observé avec le vrai modèle, « il livre ? » -> « Oui, nous effectuons bien la livraison »).

Invariant : répondre à une question ne MUTE rien (l'appelant garde menu, choix, panier, attente en cours).

Ce n'est pas un chatbot FAQ : aucune connaissance générale, seulement les faits visibles du contexte courant.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Mapping, Optional, Sequence

from pydantic import BaseModel, model_validator

from ladini.graphs.agents.market_coach.domain.selection_reference import (
    Criterion,
    ReferenceType,
    SelectionReference,
    Status,
    VisibleOption,
    resolve_reference,
)

UNKNOWN_FACT = "Je n'ai pas cette information"

#: Au-delà, un stock affiché n'est plus présenté comme actuel (aligné sur `menu_facts.MENU_FACTS_TTL_SECONDS`).
_STALE_SECONDS = 600.0


class QuestionTopic(str, Enum):
    PRICE = "PRICE"
    STOCK = "STOCK"
    DELIVERY = "DELIVERY"
    CERTIFICATION = "CERTIFICATION"
    LOCATION = "LOCATION"
    TOTAL = "TOTAL"
    #: « le plus proche » : aucune distance réelle n'est connue (seulement la région) — jamais calculé.
    DISTANCE = "DISTANCE"
    COMPARISON = "COMPARISON"
    OTHER = "OTHER"


class ContextQuestion(BaseModel):
    """Question STRUCTURÉE émise par le modèle (jamais sa réponse)."""

    topic: QuestionTopic
    #: COMPARISON : critère objectif demandé (« lequel est moins cher ? »).
    criterion: Optional[Criterion] = None
    #: COMPARISON : producteurs nommés (« entre Gilbert et Moussa »).
    names: List[str] = []
    #: Option visée (« il livre ? » à propos de « celui à 500 ») ; à défaut, le producteur choisi.
    target: Optional[SelectionReference] = None

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _null_names(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("names") is None:
            data = {**data, "names": []}
        return data


def _fmt(value: Optional[float]) -> str:
    return "" if value is None else f"{value:g}"


def _price_text(o: VisibleOption) -> str:
    return o.price_label or (f"{_fmt(o.price)} FCFA/{o.unit.lower()}" if o.price is not None else "prix non indiqué")


def _stock_text(o: VisibleOption) -> str:
    return f"{_fmt(o.availability)} {o.unit.lower()}".strip() if o.availability is not None else "stock non indiqué"


def _subject(options: Sequence[VisibleOption], q: ContextQuestion, chosen_id: Optional[str]) -> Optional[VisibleOption]:
    if q.target is not None:
        res = resolve_reference(q.target, options)
        if res.status == Status.EXACT and res.index is not None:
            return next((o for o in options if o.index == res.index), None)
        return None
    if chosen_id:
        return next((o for o in options if o.entity_id == chosen_id), None)
    return None


def _named(options: Sequence[VisibleOption], names: Sequence[str]) -> List[VisibleOption]:
    found: List[VisibleOption] = []
    for name in names:
        ref = SelectionReference(reference_type=ReferenceType.ATTRIBUTE, producer_name=name)
        res = resolve_reference(ref, options)
        if res.status == Status.EXACT and res.index is not None:
            found.extend(o for o in options if o.index == res.index and o not in found)
    return found


def _comparison(options: Sequence[VisibleOption], q: ContextQuestion) -> str:
    pool = _named(options, q.names) if q.names else list(options)
    if len(pool) < 2:
        return f"{UNKNOWN_FACT} pour comparer : dis-moi lesquels te intéressent parmi ceux affichés."
    if q.criterion in (Criterion.CHEAPEST, Criterion.HIGHEST_AVAILABILITY):
        res = resolve_reference(SelectionReference(reference_type=ReferenceType.PREFERENCE, criterion=q.criterion), pool)
        if res.status == Status.EXACT and res.index is not None:
            winner = next(o for o in pool if o.index == res.index)
            label = "le moins cher" if q.criterion == Criterion.CHEAPEST else "celui qui a le plus de stock"
            return f"{winner.name} est {label} : {_price_text(winner)}, {_stock_text(winner)} dispo. Tu le prends ?"
        return res.message or UNKNOWN_FACT
    # comparaison SANS critère objectif (« lequel est mieux ? ») : on donne les FAITS et on laisse choisir — jamais un classement global inventé.
    cheapest = resolve_reference(SelectionReference(reference_type=ReferenceType.PREFERENCE, criterion=Criterion.CHEAPEST), pool)
    stock = resolve_reference(SelectionReference(reference_type=ReferenceType.PREFERENCE, criterion=Criterion.HIGHEST_AVAILABILITY), pool)
    lines = [f"{o.name} : {_price_text(o)}, {_stock_text(o)} dispo" + (f", {o.region}" if o.region else "") for o in pool[:3]]
    verdict: List[str] = []
    if cheapest.status == Status.EXACT and cheapest.index is not None:
        verdict.append(f"{next(o for o in pool if o.index == cheapest.index).name} est moins cher")
    if stock.status == Status.EXACT and stock.index is not None:
        verdict.append(f"{next(o for o in pool if o.index == stock.index).name} a plus de stock")
    tail = (" " + ", ".join(verdict) + ".") if verdict else ""
    return "\n".join(lines) + f"\n{tail.strip()} Tu préfères quoi ?".strip()


def answer_question(
    q: ContextQuestion,
    options: Sequence[VisibleOption],
    *,
    chosen_id: Optional[str] = None,
    cart: Sequence[Mapping[str, Any]] = (),
    created_at: Optional[float] = None,
    now: Optional[float] = None,
) -> str:
    """Réponse DÉTERMINISTE (texte court) — ou « Je n'ai pas cette information ». Ne modifie rien."""
    stale = created_at is not None and now is not None and (now - created_at) > _STALE_SECONDS
    topic = q.topic

    if topic == QuestionTopic.TOTAL:
        lines = [i for i in cart if isinstance(i, Mapping)]
        if not lines:
            return "Ton panier est vide pour l'instant."
        total = sum(float(i.get("line_total") or 0.0) for i in lines)
        n = len(lines)
        return f"Total estimé : {total:g} FCFA ({n} article{'s' if n > 1 else ''})."

    if topic == QuestionTopic.DISTANCE:
        return "Je ne connais que la région de chaque producteur, pas la distance : je ne peux pas dire qui est le plus proche."

    if topic == QuestionTopic.COMPARISON:
        return _comparison(options, q)

    subject = _subject(options, q, chosen_id)
    if subject is None:
        if topic in (QuestionTopic.STOCK, QuestionTopic.PRICE, QuestionTopic.LOCATION) and options:
            facts = {QuestionTopic.STOCK: _stock_text, QuestionTopic.PRICE: _price_text, QuestionTopic.LOCATION: lambda o: o.region or "région non indiquée"}[topic]
            return "\n".join(f"{o.name} : {facts(o)}" for o in list(options)[:4])
        return f"{UNKNOWN_FACT}."

    if topic == QuestionTopic.STOCK:
        note = " (liste affichée il y a un moment : ça a pu changer)" if stale else ""
        return f"{subject.name} : {_stock_text(subject)} disponible{note}."
    if topic == QuestionTopic.PRICE:
        return f"{subject.name} : {_price_text(subject)}."
    if topic == QuestionTopic.LOCATION:
        return f"{subject.name} est à {subject.region}." if subject.region else f"{UNKNOWN_FACT} sur la localisation de {subject.name}."
    if topic == QuestionTopic.DELIVERY:
        return f"{UNKNOWN_FACT} sur la livraison de {subject.name}."
    if topic == QuestionTopic.CERTIFICATION:
        return f"{UNKNOWN_FACT} sur la certification de {subject.name}."
    return f"{UNKNOWN_FACT}."


__all__ = ["ContextQuestion", "QuestionTopic", "UNKNOWN_FACT", "answer_question"]
