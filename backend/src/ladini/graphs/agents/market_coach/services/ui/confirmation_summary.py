"""Confirmation summary builder — goal-aware French summaries.

Extracted from ``nodes/confirmation_gate.py`` to decouple business-goal
knowledge from the confirmation orchestration node.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ladini.core.formatting import fmt_num as _fmt_num
from ladini.graphs.agents.market_coach.utils import canonical_unit_label


def _safe_price_unit(raw: Any, fallback: str) -> str:
    """Nettoie `payload["price_unit"]` avant affichage dans "FCFA/{unit}".

    `price_unit` est un champ libre rempli par le LLM (voir interpreter/
    routing.py) — un LLM ne respecte pas toujours à 100% la consigne "renvoie
    UNIQUEMENT le token d'unité" et peut recopier tout ou partie du contexte
    ("FCFA/UNITE" au lieu de "UNITE"). Sans ce garde-fou, ça produisait un
    récapitulatif "FCFA/FCFA/UNITE" bien visible pour l'utilisateur. On rejette
    toute valeur contenant déjà "FCFA"/"CFA"/"/" — signe de contamination —
    et on retombe sur l'unité de la quantité plutôt que d'afficher du bruit.
    """
    text = str(raw or "").strip().upper()
    if not text or "FCFA" in text or "CFA" in text or "/" in text:
        return fallback
    return canonical_unit_label(text, fallback)


def _unit_assumed_note(payload: Dict[str, Any], unit_label: str) -> str:
    """Avertissement visible quand l'unité n'a PAS été écrite par le
    producteur mais seulement supposée d'après la nature du produit
    (`validation.py::_apply_slot_defaults`, drapeau `unit_was_assumed`).

    Incident réel (2026-09-15) : « Vente de 25 LITRE de boeufs » — un défaut
    silencieux, jamais distingué d'une unité confirmée, laissait passer une
    supposition fausse jusqu'au récapitulatif final. Même famille que
    `mismatch_note` ci-dessous : on ne bloque pas la conversation avec une
    question supplémentaire (le récap à confirmer/annuler existe déjà), mais
    on rend l'hypothèse impossible à manquer et on dit comment la corriger —
    un producteur qui confirme par habitude, sans tout relire, doit quand
    même voir CE point précis.

    Formulation corrigée (2026-09-15, retour terrain) : la première version
    disait « Dites *modifier unité : ...* » — une SYNTAXE DE COMMANDE, pas
    une valeur. Un producteur qui répond littéralement « modifier unite »
    (sans valeur, comme observé en usage réel) ne donne rien d'exploitable
    au tour suivant, qui ne peut que réafficher le MÊME récap inchangé. La
    consigne demande maintenant le MOT lui-même (« tête », « sac »…) — c'est
    exactement ce que `extract_unit_only_from_text`/`resolve_product_unit`
    savent reconnaître de façon fiable dans une réponse libre, sans exiger
    une syntaxe particulière."""
    if not payload.get("unit_was_assumed"):
        return ""
    return (
        f"\n⚠️ Unité non précisée par vous : *{unit_label}* supposée d'après le "
        "produit. Si ce n'est pas la bonne, répondez simplement avec l'unité "
        "correcte (ex : *tête*, *sac*, *kg*)."
    )


def _resolve_units(
    payload: Dict[str, Any], default_unit: str = "KG"
) -> Tuple[str, str]:
    conversion = payload.get("unit_conversion") or {}
    converted_unit = payload.get("unit") or conversion.get("to_unit") or default_unit
    display_unit = (
        payload.get("unit_display") or conversion.get("from_unit") or converted_unit
    )
    canonical_display = canonical_unit_label(
        display_unit, canonical_unit_label(default_unit)
    )
    canonical_converted = canonical_unit_label(
        converted_unit, canonical_unit_label(default_unit)
    )
    return canonical_display, canonical_converted


def _format_quantity(
    payload: Dict[str, Any], default_unit: str = "KG"
) -> Optional[str]:
    converted_qty = payload.get("quantity")
    if converted_qty in (None, "", [], {}):
        return None

    display_qty = payload.get("quantity_display")
    if display_qty in (None, "", [], {}):
        display_qty = payload.get("original_quantity")

    display_unit, converted_unit = _resolve_units(payload, default_unit)

    if display_qty not in (None, "", [], {}):
        return f"{_fmt_num(display_qty)} {display_unit}".strip()

    return f"{_fmt_num(converted_qty)} {converted_unit}".strip()


def _format_pricing_tiers(payload: Dict[str, Any]) -> Optional[str]:
    """Récapitulatif groupé des déclinaisons de prix/conditionnement pour un
    MÊME produit (2026-08-27, ex: "500f le demi-litre en sachet et 600f le
    bidon"). `unit` y est affiché TEL QUEL, littéralement — jamais passé par
    `canonical_unit_label` (ni aucune autre normalisation d'unité) : c'est
    exactement le point de cette fonctionnalité, voir
    domain/catalog/models.py::Product.pricing_tiers. Renvoie `None` si
    `pricing_tiers` est absent/vide/mal formé (un seul tarif → rendu classique
    inchangé, géré ailleurs)."""
    tiers = payload.get("pricing_tiers")
    if not isinstance(tiers, list) or not tiers:
        return None
    lines = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        qty = tier.get("quantity")
        unit = str(tier.get("unit") or "").strip()
        price = tier.get("price")
        packaging = tier.get("packaging")
        if qty in (None, "", [], {}) or not unit or price in (None, "", [], {}):
            continue
        label = f"{_fmt_num(qty)} {unit}"
        if packaging:
            label += f" ({packaging})"
        lines.append(f"  • {label} — {_fmt_num(price)} FCFA")
    if not lines:
        return None
    return "\n".join(lines)


def build_confirmation_summary(goal: str, payload: Dict[str, Any]) -> str:
    if goal == "PRODUCTION_DECLARE_FUTURE":
        production_type = str(payload.get("production_type") or "CROP").upper()
        product = payload.get("product") or payload.get("species") or "production"
        default_unit = payload.get("unit") or (
            "KG" if production_type == "CROP" else "HEAD"
        )
        quantity_line = _format_quantity(payload, default_unit)
        display_unit, converted_unit = _resolve_units(payload, default_unit)
        price = payload.get("price") or payload.get("price_per_unit")
        eta = payload.get("estimated_available_at") or payload.get(
            "expected_harvest_date"
        )
        farm = payload.get("farm_name") or payload.get("farm_id")
        # Le prix a sa propre base (ex: "10000 FCFA/kg" alors que la quantité
        # totale est en tonnes) — ne jamais réutiliser aveuglément l'unité de
        # la quantité pour l'affichage du prix si l'utilisateur en a donné une
        # explicitement (voir `price_unit`, interpreter/routing.py).
        price_display_unit = _safe_price_unit(
            payload.get("price_unit"),
            canonical_unit_label(display_unit or converted_unit),
        )

        lines = [
            f"Type : {production_type}",
            f"Produit : {product}",
            f"Quantité prévue : {quantity_line}" if quantity_line else None,
            (
                f"Prix unitaire : {_fmt_num(price)} FCFA/{price_display_unit}"
                if price not in (None, "", [], {})
                else None
            ),
            f"Disponible vers : {eta}" if eta not in (None, "", [], {}) else None,
            f"Exploitation : {farm}" if farm not in (None, "", [], {}) else None,
        ]
        bullet_list = "\n".join(f"- {line}" for line in lines if line)
        summary = "Déclaration d'un lot futur"
        note = _unit_assumed_note(payload, canonical_unit_label(converted_unit))
        return f"{summary} :\n{bullet_list}{note}" if bullet_list else summary

    if goal == "PRODUCTION_UPDATE_FUTURE":
        # Récap dynamique : n'affiche QUE les champs réellement fournis (mise à
        # jour partielle) — sans ça le générique ne montrait que quantité/unité
        # et omettait silencieusement un changement de nom (bug vécu : "le nom
        # c'est maïs" confirmé mais absent du récap).
        lines = []
        if payload.get("product") not in (None, "", [], {}):
            lines.append(f"Nouveau nom : {payload.get('product')}")
        if payload.get("price") not in (None, "", [], {}):
            unit_for_price = _safe_price_unit(
                payload.get("price_unit"),
                canonical_unit_label(payload.get("unit") or "KG"),
            )
            lines.append(
                f"Nouveau prix : {_fmt_num(payload.get('price'))} FCFA/{unit_for_price}"
            )
        if payload.get("quantity") not in (None, "", [], {}):
            unit_for_qty = canonical_unit_label(payload.get("unit") or "KG")
            lines.append(
                f"Nouvelle quantité : {_fmt_num(payload.get('quantity'))} {unit_for_qty}"
            )
        if (
            payload.get("unit") not in (None, "", [], {})
            and payload.get("price") in (None, "", [], {})
            and payload.get("quantity") in (None, "", [], {})
        ):
            lines.append(
                f"Nouvelle unité : {canonical_unit_label(payload.get('unit'))}"
            )
        if payload.get("estimated_available_at") not in (None, "", [], {}):
            lines.append(
                f"Nouvelle date de disponibilité : {payload.get('estimated_available_at')}"
            )
        if payload.get("production_type") not in (None, "", [], {}):
            lines.append(f"Nouveau type : {payload.get('production_type')}")
        bullet_list = "\n".join(f"- {line}" for line in lines)
        summary = "Mise à jour de la production"
        return f"{summary} :\n{bullet_list}" if bullet_list else summary

    if goal == "SALES_UPDATE_PRODUCT":
        # Même logique que PRODUCTION_UPDATE_FUTURE : n'afficher QUE les champs
        # réellement fournis (mise à jour partielle du catalogue).
        lines = []
        if payload.get("product") not in (None, "", [], {}):
            lines.append(f"Nouveau nom : {payload.get('product')}")
        if payload.get("price") not in (None, "", [], {}):
            unit_for_price = _safe_price_unit(
                payload.get("price_unit"),
                canonical_unit_label(payload.get("unit") or "KG"),
            )
            lines.append(
                f"Nouveau prix : {_fmt_num(payload.get('price'))} FCFA/{unit_for_price}"
            )
        if payload.get("quantity") not in (None, "", [], {}):
            unit_for_qty = canonical_unit_label(payload.get("unit") or "KG")
            lines.append(
                f"Nouvelle quantité : {_fmt_num(payload.get('quantity'))} {unit_for_qty}"
            )
        if (
            payload.get("unit") not in (None, "", [], {})
            and payload.get("price") in (None, "", [], {})
            and payload.get("quantity") in (None, "", [], {})
        ):
            lines.append(
                f"Nouvelle unité : {canonical_unit_label(payload.get('unit'))}"
            )
        bullet_list = "\n".join(f"- {line}" for line in lines)
        summary = "Mise à jour du produit"
        return f"{summary} :\n{bullet_list}" if bullet_list else summary

    product = payload.get("product")
    price = payload.get("price")
    quantity_line = _format_quantity(payload)
    display_unit, converted_unit = _resolve_units(payload)
    # Le prix a sa propre base si l'utilisateur en a donné une explicitement
    # (ex: "10000 FCFA/kg" sur une quantité totale exprimée en tonnes) — voir
    # `price_unit` (interpreter/routing.py) ; sinon on retombe sur l'unité de
    # la quantité, comportement inchangé pour le cas courant (même unité).
    price_unit = _safe_price_unit(
        payload.get("price_unit"), canonical_unit_label(converted_unit)
    )
    price_fmt = _fmt_num(price) if price not in (None, "", [], {}) else None

    # `payload["quantity"]`/`payload["unit"]` (= `converted_unit` ici) sont
    # DÉJÀ normalisés en KG par `_normalize_quantity_to_kg` (nodes/
    # validation.py) quand l'utilisateur a donné une quantité en tonnes —
    # c'est l'unité RÉELLEMENT utilisée à l'exécution (create_product...).
    # `display_unit` ne sert qu'à réafficher le mot de l'utilisateur ("200
    # TONNE") sans jamais toucher au stockage — l'utiliser ici pour décider
    # d'un "mismatch" comparait donc le prix à l'unité D'AFFICHAGE au lieu de
    # l'unité RÉELLEMENT appliquée, et déclenchait un faux avertissement
    # ("le prix sera appliqué par TONNE") alors que la quantité avait déjà
    # été convertie en KG et que le prix (FCFA/KG) correspondait exactement.
    quantity_unit_for_price = canonical_unit_label(converted_unit)
    price_unit_mismatch = (
        bool(payload.get("price_unit"))
        and price_fmt is not None
        and price_unit != quantity_unit_for_price
    )
    mismatch_note = (
        f"\n⚠️ Le prix sera appliqué par {quantity_unit_for_price} (unité de l'offre), "
        f"pas par {price_unit} — dites *modifier prix* si ce n'est pas ce que vous vouliez."
        if price_unit_mismatch
        else ""
    )
    # Ce que le récap AFFICHE doit être ce qui est EXÉCUTÉ : l'exécution applique le prix à
    # l'unité de la quantité. Incident 2026-09-28 : « à 500 FCFA/SAC » affiché, puis
    # « appliqué par LITRE » en avertissement — le montant en gras mentait. Le validateur
    # (`domain/price_basis.py`) empêche normalement d'arriver ici avec un écart ; ce filet
    # garantit qu'aucun chemin (brouillon, ancien état) n'affiche une base non exécutée.
    if price_unit_mismatch:
        price_unit = quantity_unit_for_price
    conversion_note = (
        f" (soit {payload.get('price_conversion_note')})"
        if payload.get("price_conversion_note")
        else ""
    )
    unit_note = _unit_assumed_note(payload, quantity_unit_for_price)

    pricing_tiers_block = _format_pricing_tiers(payload)

    # Incident réel (2026-09-14) : quand `pricing_tiers` est présent, le
    # récapitulatif n'affichait QUE les déclinaisons de prix — la quantité
    # TOTALE en stock (root `quantity`/`unit`, ex: "900 LITRE" issus de "60
    # bidons de 5L et 30 bidons de 20L") disparaissait complètement de ce que
    # voit le producteur avant de confirmer. Il ne pouvait donc jamais
    # repérer une extraction fausse du stock total — l'agent doit guider,
    # pas cacher ce qu'il a compris. Ajoutée en complément des tarifs, jamais
    # à leur place.
    _quantity_with_tiers_suffix = (
        f"\nQuantité totale disponible : {quantity_line}" if quantity_line else ""
    )

    mapping = {
        "SALES_PUBLISH_PRODUCT": (
            f"Vente de {product} — plusieurs déclinaisons :\n{pricing_tiers_block}"
            f"{_quantity_with_tiers_suffix}{unit_note}"
            if pricing_tiers_block
            else (
                (
                    f"Vente de {quantity_line} de {product}"
                    f" à {price_fmt} FCFA/{price_unit}{conversion_note}."
                    if price_fmt
                    else f"Vente de {quantity_line} de {product}."
                )
                + mismatch_note
                + unit_note
            )
            if quantity_line
            else None
        ),
        "SALES_RECORD_DIRECT": (
            (
                f"Enregistrement d'une vente directe : {quantity_line} de {product}"
                f" à {price_fmt} FCFA."
                if price_fmt
                else f"Enregistrement d'une vente directe : {quantity_line} de {product}."
            )
            + unit_note
        )
        if quantity_line
        else None,
        "PROCUREMENT_CREATE_REQUEST": (
            (
                f"Lancement d'un appel d'offres pour {quantity_line} de {product}"
                f" au prix plafond de {price_fmt} FCFA/{price_unit}{conversion_note}."
                if price_fmt
                else f"Lancement d'un appel d'offres pour {quantity_line} de {product}."
            )
            + mismatch_note
            + unit_note
        )
        if quantity_line
        else None,
        "SALES_PLACE_BID": (
            f"Soumission d'une offre de {price_fmt} FCFA sur cette enchère."
            if price_fmt
            else "Soumission d'une offre sur cette enchère."
        ),
        "STOCK_REGISTER_HARVEST": (
            f"Enregistrement d'une récolte de {product} — plusieurs déclinaisons :\n{pricing_tiers_block}"
            f"{_quantity_with_tiers_suffix}{unit_note}"
            if pricing_tiers_block
            else (
                (
                    f"Enregistrement d'une récolte : {quantity_line} de {product} en stock."
                    if quantity_line
                    else "Enregistrement d'une récolte en stock."
                )
                + unit_note
            )
        ),
        "FINANCE_LOG_EXPENSE": (
            f"Enregistrement d'une dépense de {price_fmt} FCFA ({product})."
            if price_fmt
            else f"Enregistrement d'une dépense pour {product}."
        ),
        "FARM_CREATE": "Déclaration d'une nouvelle exploitation.",
    }
    summary = mapping.get(goal)
    if summary:
        return summary
    # Bug réel confirmé (2026-08-18) : un goal sans gabarit dédié (ex:
    # BUYER_REQUEST — normalement `handled_by_flow`, ne devrait jamais
    # atteindre ce point, mais un mauvais aiguillage l'y a fait arriver)
    # affichait littéralement le nom de la constante interne à
    # l'utilisateur : "Validation de l'opération : BUYER_REQUEST" — illisible
    # et manifestement un bug technique exposé. Ne JAMAIS montrer un nom de
    # goal interne ; retomber sur ce qu'on sait de concret (produit/quantité)
    # plutôt que sur l'identifiant machine.
    fallback_quantity = (
        quantity_line or payload.get("quantity_display") or payload.get("quantity")
    )
    if fallback_quantity not in (None, "", [], {}):
        # Bug réel confirmé (2026-09-23, "10 KG KG") : `quantity_line` (via
        # `_format_quantity`) porte DÉJÀ l'unité ("10 KG") — lui rajouter
        # `{display_unit or converted_unit}" en dupliquait l'unité. Seuls les
        # deux replis bruts (`quantity_display`/`quantity`, de simples
        # nombres sans unité) en ont encore besoin ici.
        quantity_text = (
            str(fallback_quantity)
            if fallback_quantity is quantity_line
            else f"{fallback_quantity} {display_unit or converted_unit}"
        )
        return (
            f"Confirmez-vous cette opération pour {quantity_text}"
            + (f" de *{product}*" if product not in (None, "", [], {}) else "")
            + " ?"
        )
    if product not in (None, "", [], {}):
        return f"Confirmez-vous cette opération concernant *{product}* ?"
    return "Confirmez-vous cette opération ?"


__all__ = ["build_confirmation_summary"]
