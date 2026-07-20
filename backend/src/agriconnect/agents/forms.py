"""DRY Slot-Filling Form Engine for AgriConnect.

Provides a declarative, reusable mechanism for multi-step conversational
data collection used by both MarketCoach and FormationCoach agents.

Architecture:
  1. Define a FormSpec (name, ordered slots with labels/validators).
  2. Call `run_form_step(spec, state, extracted, mc_runtime)`.
  3. The engine returns a dict patch:
     - If slots remain: sets `active_form`, `form_data`, `form_step`,
       `response_strategy=ASK_MISSING_FIELD`, `final_response` (question).
     - If all slots filled: sets `active_form=None`, `form_step=COMPLETE`,
       `form_data` with all collected values.

Agents plug this into their graph via a single node that delegates to
`run_form_step` — zero duplication of collection logic.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from agriconnect.graphs.agents.market_coach.services.domain.quantity_unit import (
    parse_compound_quantity,
    extract_unit_only_from_text,
    default_unit_for_product,
)


def _coerce_number(v: Any) -> float:
    """Extract the first numeric value from a string, tolerating units.

    Handles "500", "500 kg", "300 fcfa", "1 500,50", "2.5 tonnes", 500.
    Raises ValueError if no number can be extracted.
    """
    if isinstance(v, (int, float)):
        return float(v)
    text = str(v).strip()
    if not text:
        raise ValueError("empty")
    # Match first number (integer or decimal, with , or . separator)
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", text)
    if not match:
        raise ValueError(f"no number in '{text}'")
    raw = match.group(1).replace(" ", "").replace(",", ".")
    return float(raw)


_FRENCH_MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12,
}
_RELATIVE_DEADLINE_RE = re.compile(
    r"dans\s+(\d+)\s*(jour|jours|semaine|semaines|mois)", re.IGNORECASE
)
_ABSOLUTE_DEADLINE_RE = re.compile(
    r"(\d{1,2})\s*(?:er)?\s+([a-zéû]+)", re.IGNORECASE
)


def _coerce_deadline(v: Any) -> str:
    """Convertit une date-limite en ISO (YYYY-MM-DD), langage naturel inclus.

    Contrairement à `estimated_available_at`/`expected_harvest_date`, `deadline`
    n'est PAS dans le schéma JSON de l'interpréteur (voir interpreter/routing.py)
    — le LLM n'a donc aucune consigne de normalisation ISO pour ce champ, et
    `form_node.py` injecte le texte utilisateur BRUT pour ce slot. Sans cette
    coercition, une réponse comme "ça sera disponible le 12 novembre" finissait
    stockée telle quelle et affichée verbatim dans le récap de confirmation.
    Lève ValueError si rien n'est reconnu (le slot reste alors "manquant" et
    l'utilisateur est re-sollicité, plutôt que d'accepter un texte inexploitable).
    """
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    text = str(v).strip()
    if not text:
        raise ValueError("empty")

    # 1. Déjà au format ISO (ou proche).
    try:
        return datetime.fromisoformat(text.replace(" ", "T")).date().isoformat()
    except ValueError:
        pass

    # 2. Relatif : "dans 7 jours", "dans 2 semaines", "dans 1 mois".
    m = _RELATIVE_DEADLINE_RE.search(text.lower())
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("jour"):
            delta = timedelta(days=amount)
        elif unit.startswith("semaine"):
            delta = timedelta(weeks=amount)
        else:  # mois — approximation à 30 jours, suffisant pour une échéance.
            delta = timedelta(days=amount * 30)
        return (datetime.now() + delta).date().isoformat()

    # 3. Absolu en langage naturel : "12 novembre", "1er septembre".
    # `finditer` (et non `search`) : un message de correction multi-champs
    # comme "prix 49750 date 30 juillet" contient de faux candidats ("50 date"
    # — 'date' n'est pas un mois). On parcourt TOUTES les correspondances et on
    # retient la première dont le mois ET le jour sont valides, au lieu de
    # lever sur le premier faux positif.
    for m in _ABSOLUTE_DEADLINE_RE.finditer(text.lower()):
        month = _FRENCH_MONTHS.get(m.group(2).strip())
        if not month:
            continue
        day = int(m.group(1))
        if not (1 <= day <= 31):
            continue
        now = datetime.now()
        try:
            candidate = date(now.year, month, day)
        except ValueError:
            continue
        # Une date déjà passée cette année est presque toujours l'année suivante.
        if candidate < now.date():
            candidate = date(now.year + 1, month, day)
        return candidate.isoformat()

    raise ValueError(f"unrecognized deadline expression: '{text}'")


def _coerce_quantity(v: Any) -> float:
    """Extract quantity from text, handling compound expressions.

    Handles "2 tonnes et 375 kg" → 2375.0,  "500 kg" → 500.0,  "3 sacs" → 3.0.
    Falls back to simple number extraction for plain numerics.
    """
    if isinstance(v, (int, float)):
        return float(v)
    text = str(v).strip()
    if not text:
        raise ValueError("empty")
    result = parse_compound_quantity(text)
    if result.quantity is not None:
        return result.quantity
    return _coerce_number(v)


logger = logging.getLogger("AgriConnect.Forms")


# ── Slot definition ─────────────────────────────────────────────────
@dataclass(frozen=True)
class SlotSpec:
    """One piece of information to collect from the user."""
    name: str
    label: str
    prompt: str
    aliases: Sequence[str] = ()
    validator: Optional[Callable[[Any], bool]] = None
    coerce: Optional[Callable[[Any], Any]] = None
    required: bool = True
    business_reason: str = ""


@dataclass(frozen=True)
class FormSpec:
    """Declarative definition of a multi-step conversational form."""
    form_id: str
    title: str
    slots: Sequence[SlotSpec]
    confirm_before_submit: bool = True
    summary_template: str = ""

    def required_slot_names(self) -> List[str]:
        return [s.name for s in self.slots if s.required]


# ── Engine result ───────────────────────────────────────────────────
@dataclass
class FormStepResult:
    """Returned by run_form_step — ready to merge into agent state."""
    patch: Dict[str, Any] = field(default_factory=dict)
    is_complete: bool = False
    summary: str = ""


# ── Core engine ─────────────────────────────────────────────────────
def _resolve_slot_value(
    slot: SlotSpec,
    extracted: Dict[str, Any],
    existing_data: Dict[str, Any],
) -> Any:
    """Try to find a value for this slot from extracted entities or existing form_data."""
    # Check primary name first
    for key in [slot.name, *slot.aliases]:
        val = extracted.get(key)
        if val not in (None, "", [], {}):
            if slot.coerce:
                try:
                    val = slot.coerce(val)
                except (TypeError, ValueError):
                    continue
            if slot.validator and not slot.validator(val):
                continue
            return val
    # Fallback: already collected
    val = existing_data.get(slot.name)
    if val not in (None, "", [], {}):
        return val
    return None


def _build_summary(spec: FormSpec, data: Dict[str, Any]) -> str:
    """Build a human-readable summary for confirmation."""
    if spec.summary_template:
        try:
            return spec.summary_template.format(**data)
        except (KeyError, IndexError):
            pass
    parts = []
    for slot in spec.slots:
        val = data.get(slot.name, "—")
        parts.append(f"• {slot.label} : {val}")
    return f"📋 {spec.title}\n" + "\n".join(parts)


_SLOT_TO_EXPECTED_INPUT: Dict[str, str] = {
    "product": "PRODUCT",
    "product_name": "PRODUCT",
    "name": "PRODUCT",
    "quantity": "QUANTITY",
    "quantity_mentioned": "QUANTITY",
    "qty": "QUANTITY",
    "price": "PRICE",
    "price_mentioned": "PRICE",
    "unit_price": "PRICE",
    "unit": "UNIT",
    "unit_mentioned": "UNIT",
    "zone": "LOCATION",
    "zone_name": "LOCATION",
    "location": "LOCATION",
    "region": "LOCATION",
    "deadline": "DATE",
    "end_date": "DATE",
    "sowing_date": "DATE",
    "date": "DATE",
}


def _canonical_expected_input(slot_name: str) -> str:
    if not slot_name:
        return "NONE"
    normalized = slot_name.lower().strip()
    return _SLOT_TO_EXPECTED_INPUT.get(normalized, slot_name.upper())


_QUANTITY_SLOT_NAMES = frozenset({"quantity", "quantity_mentioned", "qty"})


def run_form_step(
    spec: FormSpec,
    state: Dict[str, Any],
    extracted: Dict[str, Any],
) -> FormStepResult:
    """Execute one turn of the form engine.

    Parameters
    ----------
    spec : FormSpec
        The form definition.
    state : dict
        Current agent state (must contain `form_data`, optionally `form_step`).
    extracted : dict
        Entities extracted from the current user message.

    Returns
    -------
    FormStepResult with `.patch` ready to merge into the state graph.
    """
    existing_data: Dict[str, Any] = dict(state.get("form_data") or {})
    current_step = state.get("form_step")

    if current_step and current_step not in ("CONFIRMING", "COMPLETE") and extracted:
        allowed_keys = {current_step}
        step_slot = next((slot for slot in spec.slots if slot.name == current_step), None)
        if step_slot:
            allowed_keys.update(str(alias) for alias in step_slot.aliases)
        for key in list(extracted.keys()):
            if key not in allowed_keys:
                extracted.pop(key, None)

    # ── 1. Absorb all newly extracted values ────────────────────────
    for slot in spec.slots:
        val = _resolve_slot_value(slot, extracted, existing_data)
        if val is not None:
            existing_data[slot.name] = val

    # ── 1b. Cross-slot inference: auto-fill unit from quantity text ──
    qty_slot_name = next(
        (s.name for s in spec.slots if s.name in _QUANTITY_SLOT_NAMES),
        None,
    )
    unit_slot_name = next(
        (s.name for s in spec.slots if s.name in ("unit", "unit_mentioned")),
        None,
    )
    if (
        qty_slot_name
        and unit_slot_name
        and existing_data.get(qty_slot_name) not in (None, "", [], {})
        and existing_data.get(unit_slot_name) in (None, "", [], {})
    ):
        raw_qty_text = str(
            extracted.get(qty_slot_name)
            or extracted.get("quantity")
            or extracted.get("quantity_mentioned")
            or state.get("normalized_text")
            or state.get("user_query")
            or ""
        )
        unit_from_text = extract_unit_only_from_text(raw_qty_text)
        if unit_from_text:
            existing_data[unit_slot_name] = unit_from_text
        else:
            # Livestock (poussins, moutons…) are counted per head, not weighed.
            product_name = existing_data.get("product") or existing_data.get("product_name")
            existing_data[unit_slot_name] = default_unit_for_product(product_name)

    # ── 2. Find first missing required slot ─────────────────────────
    missing_slot: Optional[SlotSpec] = None
    missing_names: List[str] = []
    for slot in spec.slots:
        if slot.required and existing_data.get(slot.name) in (None, "", [], {}):
            missing_names.append(slot.name)
            if missing_slot is None:
                missing_slot = slot

    # ── 3. If all slots filled → form complete ──────────────────────
    if not missing_slot:
        summary = _build_summary(spec, existing_data)
        logger.info("[Form:%s] All slots filled. Summary: %s", spec.form_id, summary)

        if spec.confirm_before_submit and current_step != "CONFIRMING":
            return FormStepResult(
                patch={
                    "active_form": spec.form_id,
                    "form_data": existing_data,
                    "form_step": "CONFIRMING",
                    "response_strategy": "CONFIRMATION",
                    "confirmation_summary": summary,
                    "waiting_for_confirmation": True,
                    "status": "WAITING_CONFIRMATION",
                    "final_response": summary + "\n\nConfirmez-vous ces informations ? (Oui/Non)",
                    "ag_ui_component": {
                        "lc_type": "constructor",
                        "id": ["ag_ui", "FormConfirmation"],
                        "kwargs": {
                            "title": spec.title,
                            "summary": summary,
                            "submit_label": "Confirmer",
                            "cancel_label": "Annuler",
                            "metadata": {"form_id": spec.form_id},
                        },
                    },
                },
                is_complete=False,
                summary=summary,
            )

        # Already confirmed or no confirmation needed
        return FormStepResult(
            patch={
                "active_form": None,
                "form_data": existing_data,
                "form_step": "COMPLETE",
            },
            is_complete=True,
            summary=summary,
        )

    # ── 4. Ask for the first missing slot ───────────────────────────
    filled_count = len(spec.required_slot_names()) - len(missing_names)
    total_count = len(spec.required_slot_names())
    progress = f"({filled_count}/{total_count})"

    prompt = missing_slot.prompt
    if missing_slot.business_reason:
        prompt += f"\n💡 {missing_slot.business_reason}"
    prompt += f"\n📊 Progression : {progress}"

    logger.info(
        "[Form:%s] Asking for slot '%s' %s",
        spec.form_id, missing_slot.name, progress,
    )

    return FormStepResult(
        patch={
            "active_form": spec.form_id,
            "form_data": existing_data,
            "form_step": missing_slot.name,
            "response_strategy": "ASK_MISSING_FIELD",
            "last_missing_field": missing_slot.name,
            "missing_fields": missing_names,
            "expected_input": _canonical_expected_input(missing_slot.name),
            "last_agent_question": prompt,
            "status": "WAITING_INPUT",
            "final_response": prompt,
            "ag_ui_component": None,
        },
        is_complete=False,
    )


# ── Pre-built form specs (DIRECTIVE 1) ──────────────────────────────

PRODUCT_FORM = FormSpec(
    form_id="PRODUCT_CREATE",
    title="Publication d'un produit",
    slots=[
        SlotSpec(
            name="product",
            label="Nom du produit",
            prompt=(
                "🌾 Quel produit souhaitez-vous mettre en vente ?\n"
                "💡 _Exemples : Maïs, Tomate, Sorgho, Riz, Mil..._"
            ),
            aliases=["product_name", "name", "item_name"],
            business_reason="Pour cibler les bons acheteurs.",
        ),
        SlotSpec(
            name="quantity",
            label="Quantité",
            prompt=(
                "📦 Quelle quantité avez-vous à vendre ?\n"
                "💡 _Exemples : 500 kg, 3 tonnes, 20 sacs..._"
            ),
            aliases=["quantity_mentioned", "qty"],
            coerce=_coerce_quantity,
            business_reason="Pour que les acheteurs sachent ce qui est disponible.",
        ),
        SlotSpec(
            name="unit",
            label="Unité",
            prompt=(
                "⚖️ Quelle unité pour cette quantité ?\n"
                "💡 _Répondez : KG, TONNE, SAC..._"
            ),
            aliases=["unit_mentioned"],
            required=False,
            business_reason="Pour éviter toute confusion à la livraison.",
        ),
        SlotSpec(
            name="price",
            label="Prix unitaire (FCFA)",
            prompt=(
                "💰 À quel prix unitaire souhaitez-vous vendre (en FCFA) ?\n"
                "💡 _Exemples : 300, 250 FCFA/kg, 1500 le sac..._"
            ),
            aliases=["price_mentioned", "unit_price"],
            coerce=_coerce_number,
            business_reason="Pour positionner votre offre sur le marché.",
        ),
        SlotSpec(
            name="zone",
            label="Localisation",
            prompt=(
                "📍 Où se trouve votre produit ?\n"
                "💡 _Exemples : Ouagadougou, Bobo Dioulasso, Koudougou..._"
            ),
            aliases=["zone_name", "location", "locality"],
            required=False,
            business_reason="Pour connecter avec les acheteurs locaux.",
        ),
    ],
    confirm_before_submit=False,
    summary_template="📦 Produit : {product}\n📊 Quantité : {quantity} {unit}\n💰 Prix : {price} FCFA",
)

AUCTION_FORM = FormSpec(
    form_id="AUCTION_CREATE",
    title="Création d'une enchère",
    slots=[
        SlotSpec(
            name="product",
            label="Produit concerné",
            prompt="Quel produit pour l'enchère ? (ex: Maïs, Riz, Coton)",
            aliases=["product_name", "product_query"],
            business_reason="Pour publier sur le bon marché.",
        ),
        SlotSpec(
            name="price",
            label="Prix de départ minimum (FCFA)",
            prompt="Quel est le prix de départ minimum ? (en FCFA)",
            aliases=["price_mentioned", "min_price", "max_price"],
            coerce=_coerce_number,
            business_reason="Les producteurs répondront au-dessus de ce seuil.",
        ),
        SlotSpec(
            name="quantity",
            label="Quantité souhaitée",
            prompt="Quelle quantité recherchez-vous ? (ex: 1000 kg)",
            aliases=["quantity_mentioned", "qty"],
            coerce=_coerce_quantity,
            business_reason="Pour filtrer les offres adaptées.",
        ),
        SlotSpec(
            name="deadline",
            label="Date de fin",
            prompt="Jusqu'à quand l'enchère est-elle ouverte ? (ex: dans 7 jours, 2025-07-01)",
            aliases=["end_date", "expiry"],
            coerce=_coerce_deadline,
            required=True,
            business_reason="Les producteurs doivent savoir quand répondre.",
        ),
    ],
    confirm_before_submit=True,
    summary_template=(
        "🔨 Enchère : {product}\n"
        "📊 Quantité : {quantity}\n"
        "💰 Prix min : {price} FCFA\n"
        "📅 Date limite : {deadline}"
    ),
)

CROP_CYCLE_FORM = FormSpec(
    form_id="CROP_CYCLE_CREATE",
    title="Création d'un cycle de culture",
    slots=[
        SlotSpec(
            name="identified_crop",
            label="Type de culture",
            prompt="Quelle culture souhaitez-vous suivre ? (ex: Maïs, Sorgho, Niébé, Riz)",
            aliases=["crop_name", "crop_type", "product", "crop"],
            business_reason="Pour vous envoyer des conseils adaptés à cette culture.",
        ),
        SlotSpec(
            name="sowing_date",
            label="Date de semis",
            prompt="Quand avez-vous semé (ou prévoyez-vous de semer) ? (ex: 15 juin, la semaine dernière)",
            aliases=["planting_date", "date_semis", "start_date"],
            business_reason="Pour calibrer les conseils au bon stade de croissance.",
        ),
        SlotSpec(
            name="area_ha",
            label="Superficie (hectares)",
            prompt="Quelle est la superficie de cette parcelle ? (ex: 2 ha, 5000 m²)",
            aliases=["surface", "area", "size"],
            coerce=_coerce_number,
            business_reason="Pour adapter les doses d'intrants et les rendements attendus.",
        ),
        SlotSpec(
            name="zone_category",
            label="Zone géographique",
            prompt="Dans quelle zone/région se trouve cette parcelle ? (ex: Centre, Hauts-Bassins)",
            aliases=["zone", "zone_name", "location", "region"],
            business_reason="Pour tenir compte du climat et du sol de votre zone.",
        ),
    ],
    summary_template="🌱 Culture : {identified_crop}\n📅 Semis : {sowing_date}\n📐 Superficie : {area_ha} ha\n📍 Zone : {zone_category}",
)


# Registry for lookup by form_id
FORM_REGISTRY: Dict[str, FormSpec] = {
    PRODUCT_FORM.form_id: PRODUCT_FORM,
    AUCTION_FORM.form_id: AUCTION_FORM,
    CROP_CYCLE_FORM.form_id: CROP_CYCLE_FORM,
}

__all__ = [
    "SlotSpec",
    "FormSpec",
    "FormStepResult",
    "run_form_step",
    "PRODUCT_FORM",
    "AUCTION_FORM",
    "CROP_CYCLE_FORM",
    "FORM_REGISTRY",
]
