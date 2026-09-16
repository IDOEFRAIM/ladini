"""Slot enrichment — text-based extraction of structured slot values.

Centralises all heuristic and LLM-based extraction that was previously
scattered across validator.py.  Called by the SlotResolver (memory_update)
as the single enrichment pass before validation.

Classification par comportement (2026-09-09, audit Bloc 2, Blocker C) —
`memory_update` n'est PAS zero-LLM à cause de ce module, et c'est documenté
ici explicitement plutôt que prétendu autrement :

- NORMALIZATION / DETERMINISTIC ENRICHMENT (0 LLM, légitimement downstream) :
  `extract_quantity_unit_from_text`, `extract_unit_only`,
  `extract_production_type_from_text`, `extract_surface_from_text`,
  `extract_future_datetime_from_text`, la résolution d'unité via
  `domain/quantity_unit.py::resolve_product_unit`.
- VALIDATION (sanitize la SORTIE du LLM ci-dessous, jamais l'entrée
  utilisateur) : `SlotExtractionPayload`.
- LLM EXTRACTION — `LEGACY_SLOT_RECOVERY` borné, la seule exception réelle :
  `llm_extract_quantity_unit`, appelé UNIQUEMENT pour `BUYER_ADD_TO_CART`,
  UNIQUEMENT si `quantity`/`unit` manquent encore après l'extraction primaire
  de `input_interpreter` (Bloc 1) + le repli déterministe ci-dessus, ou si
  `product` porte un artefact de parsing connu. Pourquoi ce second appel
  existe (au lieu d'être absorbé par `input_interpreter`) : réparation
  ciblée d'une limite connue de l'extraction primaire sur les réponses
  composées ("5kg de tomates à 200f" mal segmentées) — PAS une
  réinterprétation de l'intention (`SlotExtractionPayload` n'expose que
  quantity/unit/product ; tout le reste renvoyé par le LLM est
  silencieusement ignoré par construction Pydantic — voir
  tests/unit/test_slot_enrichment_llm_recovery.py). Ne remplace jamais une
  valeur DÉJÀ explicite (fill-if-missing, sauf `product` quand il est
  positivement identifié comme un artefact — `product_is_dirty`). Absorber
  cette réparation dans `input_interpreter` nécessiterait de rouvrir Bloc 1
  (gelé) — documenté comme modification future, non entreprise ici sans
  validation explicite.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ValidationError

from ladini.core.logger import get_logger
from ladini.domain.quantity_unit import (
    extract_unit_only_from_text as _extract_unit_only,
)
from ladini.domain.quantity_unit import (
    default_unit_for_product as _default_unit_for_product,
)
from ladini.domain.quantity_unit import (
    is_livestock_product as _is_livestock_product,
)
from ladini.domain.quantity_unit import (
    parse_quantity_unit_from_text as _parse_qty_unit,
)
from ladini.domain.quantity_unit import (
    resolve_product_unit as _resolve_product_unit,
)
from ladini.graphs.agents.market_coach.llm_gateway import (
    LLMGatewayExhausted,
    resolve_gateway,
    resolve_profile,
)

logger = get_logger("Ladini.MarketCoach.SlotEnrichment")


class SlotValidationError(RuntimeError):
    """Raised when an LLM payload fails schema validation."""


# Mots désignant le TYPE de production (culture/élevage), jamais un produit.
# Restreint aux mots GÉNÉRIQUES : surtout PAS "poulet"/"poussins"/"mais" qui
# sont de vrais produits. Empêche qu'une réponse à « culture ou élevage ? »
# (« c'est une culture ») soit enregistrée comme nom de produit "culture".
#
# SOURCE UNIQUE de ce vocabulaire métier : ce module possède déjà la logique
# de type de production (`extract_production_type_from_text`,
# `_PRODUCTION_TYPE_SYNONYMS`). `interpreter/entities.py` importe d'ici au
# lieu de maintenir sa propre copie — les deux listes étaient auparavant
# identiques mais séparées, donc vouées à diverger (un mot ajouté ici mais
# pas là-bas = produit nommé « culture » de nouveau accepté d'un côté).
PRODUCTION_TYPE_WORDS = frozenset(
    {
        "culture",
        "cultures",
        "elevage",
        "élevage",
        "elevages",
        "élevages",
        "betail",
        "bétail",
        "animal",
        "animaux",
        "plante",
        "plantes",
        "vegetal",
        "végétal",
        "crop",
        "livestock",
    }
)
#: Mots de liaison ignorés pour décider si une réponse ne contient QUE des
#: mots de type ("c'est une culture" → "culture" après retrait des fillers).
PRODUCTION_TYPE_FILLER = frozenset(
    {
        "cest",
        "c'est",
        "une",
        "un",
        "de",
        "du",
        "des",
        "la",
        "le",
        "les",
        "ceci",
        "ca",
        "ça",
        "juste",
        "plutot",
        "plutôt",
        "genre",
        "type",
    }
)

# Alias internes (compat des usages existants dans ce module).
_PRODUCT_TYPE_ONLY_WORDS = PRODUCTION_TYPE_WORDS
_PRODUCT_TYPE_FILLER = PRODUCTION_TYPE_FILLER


class SlotExtractionPayload(BaseModel):
    quantity: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    unit: Optional[str] = Field(default=None)
    product: Optional[str] = Field(default=None, max_length=80)

    @staticmethod
    def _clean_product(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        # Block obvious SQL/control characters used during poisoning attempts.
        if any(token in cleaned for token in ("--", ";", "/*", "*/", "DROP", "ALTER")):
            raise ValueError("Produit contient des instructions interdites")
        # Rejet des mots de type de production (culture/élevage/…) — pas un produit.
        meaningful = [
            t.replace("'", "").replace("’", "") for t in cleaned.lower().split()
        ]
        meaningful = [t for t in meaningful if t and t not in _PRODUCT_TYPE_FILLER]
        if meaningful and all(t in _PRODUCT_TYPE_ONLY_WORDS for t in meaningful):
            return None
        return cleaned

    @staticmethod
    def _clean_unit(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        allowed = {"KG", "TONNE", "SAC", "PANIER", "TETE"}
        if normalized and normalized not in allowed:
            raise ValueError(f"Unité non supportée: {value}")
        return normalized or None

    @staticmethod
    def _clean_quantity(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            if not float("inf") > float(value) >= 0:
                raise ValueError("Quantité hors bornes")
            return float(value)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise ValueError("Quantité invalide") from None
        if parsed < 0:
            raise ValueError("Quantité négative interdite")
        return parsed

    @property
    def sanitized(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if self.quantity is not None:
            data["quantity"] = self.quantity
        if self.unit:
            data["unit"] = self.unit
        product = self._clean_product(self.product)
        if product:
            data["product"] = product
        return data

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            instance = cls(
                quantity=cls._clean_quantity(payload.get("quantity")),
                unit=cls._clean_unit(payload.get("unit")),
                product=payload.get("product"),
            )
        except ValueError as exc:
            raise SlotValidationError(str(exc)) from exc
        except ValidationError as exc:
            raise SlotValidationError(str(exc)) from exc
        return instance.sanitized


_PRODUCTION_TYPE_SYNONYMS = {
    "livestock": "LIVESTOCK",
    "elevage": "LIVESTOCK",
    "élevage": "LIVESTOCK",
    "betail": "LIVESTOCK",
    "bétail": "LIVESTOCK",
    "volaille": "LIVESTOCK",
    "poulet": "LIVESTOCK",
    "poussins": "LIVESTOCK",
    "culture": "CROP",
    "cultures": "CROP",
    "agriculture": "CROP",
    "champ": "CROP",
    "plantation": "CROP",
}


def extract_quantity_unit_from_text(text: str) -> Optional[Dict[str, Any]]:
    parsed = _parse_qty_unit(text or "")
    if not parsed:
        return None
    result = parsed.as_dict()
    return result if result else None


def extract_unit_only(text: str) -> Optional[str]:
    return _extract_unit_only(text) if text else None


def extract_production_type_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for token, value in _PRODUCTION_TYPE_SYNONYMS.items():
        if token in lowered:
            return value
    stripped = lowered.strip().upper()
    if stripped in {"CROP", "LIVESTOCK"}:
        return stripped
    return None


def extract_surface_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(
        r"(\d+[\d\s,.]*)\s*(ha|hectare|hectares|m2|m²)", text, re.IGNORECASE
    )
    if not match:
        return None
    raw_value = match.group(1).replace(" ", "").replace(",", ".")
    unit = match.group(2).lower()
    try:
        numeric = float(raw_value)
    except (TypeError, ValueError):
        return None
    if unit in {"m2", "m²"}:
        return numeric / 10000.0
    return numeric


def extract_future_datetime_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    absolute = re.search(r"(\d{4}-\d{2}-\d{2}(?:[tT ]\d{2}:\d{2}(?::\d{2})?)?)", text)
    if absolute:
        candidate = absolute.group(1).replace(" ", "T")
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    month_match = re.search(r"dans\s+(\d+)\s*mois", text, re.IGNORECASE)
    if month_match:
        months = int(month_match.group(1))
        return (now + timedelta(days=30 * months)).isoformat()

    week_match = re.search(r"dans\s+(\d+)\s*semaines?", text, re.IGNORECASE)
    if week_match:
        weeks = int(week_match.group(1))
        return (now + timedelta(weeks=weeks)).isoformat()

    day_match = re.search(r"dans\s+(\d+)\s*jours?", text, re.IGNORECASE)
    if day_match:
        days = int(day_match.group(1))
        return (now + timedelta(days=days)).isoformat()

    return None


def _contains_quantitative_hint(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


async def llm_extract_quantity_unit(
    mc_runtime: Any,
    user_text: str,
) -> Optional[Dict[str, Any]]:
    if not user_text:
        return None
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return None

    prompt = (
        "Tu extrais des entités de commande agricole. "
        "Réponds strictement un JSON avec les clés: "
        "quantity (number|null), unit (KG|TONNE|SAC|PANIER|null), "
        "product (string|null). "
        "Ne retourne rien d'autre."
    )

    try:
        # LLM Gateway (2026-09-02) : budget/repli/disjoncteur portés par le
        # Gateway (profil résolu comme avant via `mc_runtime.current_goal`,
        # voir `profile_answer`), plus de `asyncio.wait_for` local — voir
        # `llm_gateway/gateway.py`.
        completion = await resolve_gateway(mc_runtime).complete(
            profile=resolve_profile(mc_runtime),
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=90,
            agent_node="llm_extract_quantity_unit",
        )
        parsed = json.loads(completion.choices[0].message.content or "{}")
        return SlotExtractionPayload.validate_payload(parsed) or None
    except SlotValidationError as validation_err:
        logger.warning(
            "SLOT_ENRICHMENT_VALIDATION_FAILED | reason=%s | snippet=%r",
            validation_err,
            (user_text or "")[:120],
        )
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "SLOT_ENRICHMENT_LLM_TIMEOUT | user_text=%r", (user_text or "")[:160]
        )
        return None
    except LLMGatewayExhausted:
        # Gateway épuisé (tous les candidats du profil down/indisponibles
        # dans le budget) — équivalent au timeout ci-dessus, même repli.
        logger.warning(
            "SLOT_ENRICHMENT_LLM_TIMEOUT | user_text=%r", (user_text or "")[:160]
        )
        return None
    except Exception as exc:
        logger.warning("SLOT_ENRICHMENT_LLM_ERROR | error=%s", exc)
        return None


def _needs_structured_extraction(payload: Dict[str, Any], fields: tuple) -> bool:
    return any(payload.get(field) in (None, "", 0, [], {}) for field in fields)


async def enrich_payload_from_text(
    payload: Dict[str, Any],
    text: str,
    goal: Optional[str],
    mc_runtime: Any,
) -> Dict[str, Any]:
    """Single entry point for all text-based slot enrichment.

    Applies deterministic regex extraction first, then LLM extraction
    as fallback for critical goals. Mutates and returns ``payload``.
    """
    goal_upper = str(goal or "").upper()

    if goal_upper == "BUYER_ADD_TO_CART":
        # LLM-PRIMARY : l'interpréteur (routing.py) a déjà extrait quantity/unit
        # dans `extracted_entities`, appliqués au payload par memory_update AVANT
        # cet enrichissement. La regex ne fait que COMBLER les trous — jamais
        # écraser ce que le LLM a déjà décidé (sinon un parse regex approximatif
        # remplaçait silencieusement une extraction LLM correcte). C'est aussi le
        # comportement « fill-if-missing » déjà appliqué à tous les autres champs
        # de cette fonction (unit/surface/date/production_type) — ici on
        # l'alignait pour BUYER_ADD_TO_CART qui écrasait, seul cas incohérent.
        extracted = extract_quantity_unit_from_text(text)
        if extracted:
            for k, v in extracted.items():
                if v not in (None, "", 0, [], {}) and payload.get(k) in (
                    None,
                    "",
                    0,
                    [],
                    {},
                ):
                    payload[k] = v

        product_str = str(payload.get("product") or "")
        product_is_dirty = _contains_quantitative_hint(product_str) or any(
            u in product_str.lower() for u in ["kg", "tonne", "sac", "panier"]
        )

        # (2026-09-09, audit Bloc 2, Blocker C) : ce second appel LLM est un
        # LEGACY_SLOT_RECOVERY borné — jamais une réinterprétation de
        # l'intention (`SlotExtractionPayload` n'a que 3 champs :
        # quantity/unit/product, tout le reste renvoyé par le LLM est
        # silencieusement ignoré par construction Pydantic) — appelé
        # UNIQUEMENT si l'extraction primaire (input_interpreter) + le repli
        # déterministe ci-dessus n'ont toujours pas rempli quantity/unit, ou
        # si `product` porte un artefact de parsing connu (un nombre/une
        # unité capturés par erreur comme nom de produit).
        if (
            _needs_structured_extraction(payload, ("quantity", "unit"))
            or product_is_dirty
        ):
            try:
                llm_extracted = await llm_extract_quantity_unit(mc_runtime, text)
            except SlotValidationError as validation_exc:
                reasons = list(payload.get("clarification_reasons") or [])
                reasons.append(str(validation_exc))
                payload["clarification_reasons"] = reasons
                payload["slot_enrichment_force_clarification"] = True
                logger.info(
                    "SLOT_ENRICHMENT_CLARIFICATION | goal=%s | reason=%s",
                    goal_upper,
                    validation_exc,
                )
            else:
                if llm_extracted:
                    for key, value in llm_extracted.items():
                        if value in (None, "", 0, [], {}):
                            continue
                        if key == "product":
                            # `product` est le SEUL champ pour lequel un
                            # écrasement est légitime : `product_is_dirty`
                            # signifie que la valeur déjà en place est un
                            # artefact de parsing CONNU-MAUVAIS (ex: "5kg"
                            # capturé comme nom de produit), pas une donnée
                            # explicite de l'utilisateur à protéger.
                            if product_is_dirty or payload.get(key) in (
                                None,
                                "",
                                0,
                                [],
                                {},
                            ):
                                payload[key] = value
                            continue
                        # quantity/unit : bug réel corrigé ici (avant ce
                        # correctif, `payload.update(...)` écrasait
                        # inconditionnellement) — `_needs_structured_
                        # extraction` se déclenche dès que L'UN des deux
                        # manque, donc l'AUTRE peut déjà être une valeur
                        # EXPLICITE de l'utilisateur (ex: quantity=500 déjà
                        # posée, unit manquant) : ce repli ne doit jamais la
                        # remplacer, même si le LLM en renvoie une différente
                        # — même politique fill-if-missing que la regex
                        # déterministe plus haut dans cette fonction.
                        if payload.get(key) in (None, "", 0, [], {}):
                            payload[key] = value

    # ── UNITÉ : décision centralisée ──────────────────────────────────
    # Voir `domain/quantity_unit.py::resolve_product_unit` — autorité UNIQUE,
    # précédence par FIABILITÉ de la source et non par ordre d'exécution.
    # Ces deux règles vivaient ici sous forme de `if unité est vide`, ce qui
    # les rendait incapables de CORRIGER quoi que ce soit : un « KG » posé
    # plus tôt par un défaut aveugle gagnait définitivement contre la nature
    # du produit (incident réel 2026-09-08, « 6500 KG de poulets »).
    #
    # `text_unit` n'est proposé que si aucune unité n'est encore posée : la
    # correction explicite en cours de conversation reste la propriété de
    # `interpreter/routing.py` (garde anti-ancrage) et de
    # `nodes/memory.py::_apply_slot` (qui purge le prix en cascade quand
    # l'unité change) — voir « Périmètre assumé » du docstring de l'autorité.
    _unit_text_hint = (
        extract_unit_only(text)
        if text and payload.get("unit") in (None, "", [], {})
        else None
    )
    # (2026-09-15) Ni le texte ni un tour précédent ne portaient d'unité :
    # si `resolve_product_unit` en renvoie une quand même, c'est UNIQUEMENT
    # sa règle 4 (nature du produit, en DÉFAUT) qui a parlé — une
    # SUPPOSITION, jamais ce que le producteur a écrit. Incident réel :
    # « Vente de 25 LITRE de boeufs » (avant le correctif d'élision) laissait
    # passer un défaut faux jusqu'au récapitulatif final SANS que rien ne le
    # distingue d'une unité confirmée — un producteur qui confirme par
    # habitude, sans tout relire, ne le voyait jamais. Voir
    # `unit_was_assumed`, lu par `services/ui/confirmation_summary.py` pour
    # avertir explicitement, et effacé dès qu'une vraie correction fixe
    # l'unité (`nodes/memory.py::_apply_slot`).
    _unit_was_unknown = (
        payload.get("product")
        and payload.get("unit") in (None, "", [], {})
        and not _unit_text_hint
    )
    resolved_unit = _resolve_product_unit(
        payload.get("product"),
        current_unit=payload.get("unit"),
        text_unit=_unit_text_hint,
    )
    # `resolve_product_unit` refuse À DESSEIN de deviner pour une culture
    # (règle 4 : `None`, « à l'appelant de demander ») — mais rien
    # n'appelait jamais l'appelant : `unit` restait `None` jusqu'à
    # `SalesPublishProductPayload` (Optional[str]=None) puis
    # `services/database/producer.py::create_product`, où `unit = clean_text
    # (unit, ...) or "KG"` le devinait quand même, SANS AUCUNE conscience de
    # la nature du produit (un élevage y recevrait "KG" aussi) et hors de
    # portée de tout récapitulatif — l'antithèse de l'autorité unique que
    # `resolve_product_unit` est censée être. On applique donc ICI le même
    # filet qu'à l'élevage (`default_unit_for_product`), TOUJOURS marqué
    # comme supposé, plutôt que de laisser une couche invisible trancher.
    if not resolved_unit and _unit_was_unknown:
        resolved_unit = _default_unit_for_product(payload.get("product"))
    if resolved_unit:
        if _unit_was_unknown:
            payload["unit_was_assumed"] = True
        payload["unit"] = resolved_unit

    if payload.get("surface") in (None, "", [], {}) and text:
        surface_value = extract_surface_from_text(text)
        if surface_value is not None:
            payload["surface"] = surface_value

    if payload.get("estimated_available_at") in (None, "", [], {}) and text:
        estimated = extract_future_datetime_from_text(text)
        if estimated:
            payload["estimated_available_at"] = estimated

    production_type_value = payload.get("production_type")
    if isinstance(production_type_value, str) and production_type_value.strip():
        candidate = production_type_value.strip().upper()
        if candidate in {"CROP", "LIVESTOCK"}:
            payload["production_type"] = candidate
        else:
            payload["production_type"] = None
    if payload.get("production_type") in (None, "", [], {}) and text:
        extracted_type = extract_production_type_from_text(text)
        if extracted_type:
            payload["production_type"] = extracted_type
    # Le nom du produit ("mouton", "vache", "poulet"...) est un signal plus
    # fiable que le texte libre : réutilise la même liste d'animaux que le
    # défaut d'unité (TETE) pour éviter de redemander à l'utilisateur culture
    # vs élevage quand le produit le dit déjà sans ambiguïté.
    if payload.get("production_type") in (None, "", [], {}) and _is_livestock_product(
        payload.get("product")
    ):
        payload["production_type"] = "LIVESTOCK"

    return payload


__all__ = [
    "enrich_payload_from_text",
    "extract_quantity_unit_from_text",
    "extract_unit_only",
    "extract_production_type_from_text",
    "extract_surface_from_text",
    "extract_future_datetime_from_text",
    "llm_extract_quantity_unit",
]
