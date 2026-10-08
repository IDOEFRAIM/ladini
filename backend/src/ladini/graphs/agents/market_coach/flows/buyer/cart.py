"""Buyer cart management — add to cart, view cart, vendor selection."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ladini.domain.edit_validation import EditVerdict, validate_structured_edit
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.domain.cart_edit import (
    CartEditSpec,
    CartField,
    EditOutcome,
    EditStatus,
)
from ladini.graphs.agents.market_coach.domain.search_constraints import (
    apply_constraints,
    constraints_from_payload,
    no_result_message,
)
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
    parse_raw_action,
    validate_action,
    vendor_offer_id,
)
from ladini.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
    ProductLookupUnavailable,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    llm_deviation_reply,
    slot_has_value,
)

from .helpers import (
    additional_products_hint,
    capture_cart_draft,
    detect_cart_action,
    logger,
    resolve_product,
    resolve_quantity,
    reusable_menu_vendors,
)
from .preorder import create_preorder

# =====================================================================
# CART MANAGEMENT NODE
# =====================================================================


def _fresh_unit_this_turn(state: Dict[str, Any]) -> Any:
    """Unité écrite dans le message de CE tour, jamais celle du payload fusionné.

    Audit 2026-09-01 : `transaction_payload` est un canal `merge_dict` — l'unité
    de la demande initiale ("30 L de lait") y survit indéfiniment. La passer
    comme `buyer_unit` sur une réponse de NOMBRE DE PAQUETS ferait refuser un
    "3" pourtant parfaitement valide, alors que c'est justement un "30 litres"
    tapé MAINTENANT qu'il faut refuser (voir
    domain/pricing_tiers.py::classify_pack_count_unit).
    """
    return (state.get("extracted_entities") or {}).get("unit")


def _change_producer_hint(n_vendors: int) -> str:
    """Rappel de la commande EXPLICITE de changement de producteur pendant le slot quantité.

    (B7) Un nombre nu est ici une QUANTITÉ : ne JAMAIS demander à l'acheteur d'envoyer un numéro
    seul pour changer de producteur."""
    if n_vendors <= 1:
        return ""
    return "🔁 _Pour changer de producteur, écrivez « changer producteur » ou « producteur 3 »._\n"


def _declared_unit(payload: Dict[str, Any], state: Dict[str, Any]) -> Any:
    """Unité réellement DÉCLARÉE par l'acheteur, ou `None`.

    (B7/B8) `payload["unit"]` peut n'être qu'un DÉFAUT supposé (`unit_was_assumed`, ex. « KG » pour
    « lait ») : le passer comme `buyer_unit` faisait refuser un nombre nu (« 5 ») avec « vendu en
    LITRE, pas en kg » alors que l'acheteur n'a écrit aucune unité — l'unité de l'offre fait alors foi.

    Mais une unité ÉCRITE dans le message de CE tour est toujours explicite, même si elle coïncide avec
    le défaut supposé (« 10 kg » après un « KG » par défaut) : le drapeau `unit_was_assumed` est collant
    (canal `merge_dict`) et ne doit jamais effacer une unité que l'acheteur vient d'écrire — sinon
    « 10 kg » serait appliqué en silence à un produit vendu en LITRE (MASS ≠ VOLUME)."""
    fresh = (state.get("extracted_entities") or {}).get("unit")
    if fresh:
        return fresh
    if payload.get("unit_was_assumed"):
        return None
    return payload.get("unit")


def _tier_menu_working_memory_patch(state: Dict[str, Any]) -> Dict[str, Any]:
    """`working_memory` patch to attach to every tier-menu WAITING_INPUT
    response — see `nodes/memory.py`'s `mapping_kind` protection.

    Real incident (2026-08-30): a bare numeric reply to the tier menu was
    silently resolved by memory.py's GENERIC selection machinery against a
    STALE `menu_snapshot_id`/`available_mapping_kind` left over from an
    EARLIER, unrelated menu in the same conversation (e.g. a vendor list
    shown much earlier) — that stale resolution path popped
    `selection_index` from the payload before `cart_management` ever got a
    chance to read it, so the tier menu re-displayed forever with no error.
    Claiming `available_mapping_kind="pricing_tier"` explicitly (now a
    protected kind in memory.py, alongside `product_vendor`) AND wiping any
    stale `menu_snapshot_id`/`available_mapping` closes this off completely,
    regardless of what an earlier menu in this same conversation left behind.
    """
    wm = dict(state.get("working_memory") or {})
    wm["available_mapping_kind"] = "pricing_tier"
    wm["menu_snapshot_id"] = None
    return wm


def _tier_choice_hint(n_tiers: int) -> str:
    """« Répondez 1 ou 2. » — l'étape « choisir le conditionnement » (jamais le nombre de paquets)."""
    return "Répondez 1 ou 2." if n_tiers == 2 else f"Répondez par un numéro de 1 à {n_tiers}."


def _build_tier_menu_response(
    state: Dict[str, Any],
    tiers: List[Dict[str, Any]],
    display_name: str,
    product_id: Any,
    ctx_product_name: str,
    vendor_selection_context: Dict[str, Any],
    note: str = "",
) -> Dict[str, Any]:
    """Construit le state patch complet d'un menu de paliers en attente —
    utilisé par les DEUX branches (multi-vendeurs et vendeur unique) de
    `cart_management`.

    Faille C (audit 2026-09-01) : cette construction (texte du menu,
    `tier_selection_context`, synchronisation `working_memory`) était
    dupliquée aux deux sites d'affichage — tout ajout futur à l'un des deux
    sans le reporter à l'autre aurait pu rouvrir la fuite de contexte/menu
    périmé déjà corrigée une fois (voir `_tier_menu_working_memory_patch`).
    Un seul point de construction rend cette désynchronisation impossible.
    """
    tier_lines = "\n".join(
        f"{i}️⃣ {t.get('quantity')} {t.get('unit')}"
        + (f" ({t['packaging']})" if t.get("packaging") else "")
        + f" — {t.get('price')} FCFA"
        for i, t in enumerate(tiers, start=1)
    )
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "ASK_MISSING_FIELD",
        # (B8) ÉTAPE 1 seulement : choisir le conditionnement. Le nombre de paquets est une question
        # SÉPARÉE, posée après la sélection (jamais mélangée dans ce texte).
        "final_response": (
            f"{note}"
            f"📦 *{display_name}* propose plusieurs conditionnements :\n"
            f"{tier_lines}\n\nQuel conditionnement souhaitez-vous ? "
            f"{_tier_choice_hint(len(tiers))}"
        ),
        "tier_selection_context": {
            "product_id": product_id,
            # (2026-08-31) `product_name` — PAS `product_id` — est la clé
            # d'identité fiable pour retrouver ce contexte plus tard :
            # `product_id` peut légitimement varier entre deux recherches
            # catalogue du MÊME produit (voir l'incident réel 2026-08-30).
            "product_name": str(ctx_product_name or "").strip().lower(),
            "tiers": tiers,
        },
        "vendor_selection_context": vendor_selection_context,
        "working_memory": _tier_menu_working_memory_patch(state),
        "available_mapping": {},
        "ag_ui_component": None,
    }


def _tier_resolved_patch(
    state: Dict[str, Any],
    tiers: List[Dict[str, Any]],
    ctx_product_name: Any,
    product_id: Any,
    resolved_tier_id: Any,
) -> Dict[str, Any]:
    """Patch d'état pour l'entrée en `WAITING_FOR_PACKAGE_COUNT`.

    Audit 2026-09-01 : ces deux branches posaient `tier_selection_context =
    None` dès qu'un palier était résolu. Conséquences vérifiées :

    1. **Aucun changement d'avis possible.** Une fois le palier figé, la liste
       des paliers n'existait plus nulle part côté état — ni `cart_management`
       ni le prompt de l'interpréteur (`tier_menu_context`) ne pouvaient
       résoudre "finalement je prends le bidon de 5 L", qui retombait alors en
       simple quantité (→ 5 paquets du MAUVAIS palier).
    2. **`available_mapping_kind` restait bloqué sur `"pricing_tier"`** — un
       menu périmé qui continuait à protéger `selection_index` bien après la
       fermeture du menu.

    On garde donc le contexte, ESTAMPILLÉ `resolved_tier_id` : le menu ne se
    réaffiche plus (cf. le garde `resolved_tier_id` côté affichage) mais la
    liste reste résoluble pour une re-sélection EXPLICITE. Un chiffre nu, lui,
    reste un nombre de paquets — seul un `selected_value` (palier désigné sans
    ambiguïté) peut rouvrir le choix.
    """
    wm = dict(state.get("working_memory") or {})
    wm["available_mapping_kind"] = None
    wm["menu_snapshot_id"] = None
    patch: Dict[str, Any] = {"working_memory": wm}
    if not resolved_tier_id or not tiers:
        patch["tier_selection_context"] = None
        return patch
    patch["tier_selection_context"] = {
        "product_id": product_id,
        "product_name": str(ctx_product_name or "").strip().lower(),
        "tiers": tiers,
        "resolved_tier_id": resolved_tier_id,
    }
    return patch


# =====================================================================
# CONTRAT D'ACTION STRUCTURÉE — EXÉCUTEUR UNIQUE (2026-09-01)
# =====================================================================
# Voir domain/selection_actions.py pour le pourquoi. Ce bloc est LA seule
# porte d'entrée qui consomme `agent_action` : quand il résout complètement
# le tour (renvoie un patch non-None), TOUT le reste de `cart_management`
# (résolution `selection_index`/`selected_value`, heuristiques de purge de
# quantité héritée...) est court-circuité pour ce tour — un seul chemin
# d'exécution, jamais deux interprétations concurrentes du même message.
# Quand il renvoie `None` (aucune action structurée valide cette fois — LLM
# dégradé, formulation hors contrat), `cart_management` retombe sur ses
# heuristiques historiques : ce filet de sécurité n'est PAS supprimé.

_ACTION_PAYLOAD_FIELDS = (
    "agent_action",
    "action_offer_id",
    "action_producer_id",
    "action_pricing_tier_id",
    "action_package_count",
    "action_quantity",
    "action_unit",
)


def _clear_action_fields(payload: Dict[str, Any]) -> None:
    """`transaction_payload` est un canal `merge_dict` : une clé absente de
    `new` ne supprime rien de `old` (voir `agents/reducers.py::merge_dict`).
    Une action structurée consommée doit donc être explicitement mise à
    `None`, sans quoi elle survivrait et se ferait ré-interpréter comme
    périmée au tour suivant — même discipline que `selection_index`/
    `selected_value` ailleurs dans ce fichier."""
    for key in _ACTION_PAYLOAD_FIELDS:
        payload[key] = None


async def _execute_selection_action(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    payload: Dict[str, Any],
    cart: List[Dict[str, Any]],
    cart_service: CartDomainService,
    phone: str,
    product_name: Any,
) -> Optional[Dict[str, Any]]:
    """INTERPRÉTATION (déjà faite par routing.py) → VALIDATION → EXÉCUTION.

    Ne fait JAMAIS confiance à l'action proposée : `validate_action`
    re-vérifie l'id référencé contre le contexte RECONSTRUIT ici-même, à cet
    instant précis — jamais contre une liste potentiellement périmée que le
    LLM/FastPath aurait pu voir à un tour antérieur.
    """
    raw = parse_raw_action(payload)
    if raw is None:
        return None

    context = build_selection_context(state)
    action = validate_action(raw, context)
    # Consommée qu'elle soit valide ou non — un `agent_action` rejeté ne doit
    # jamais rester dans le payload pour être ré-essayé au tour suivant
    # contre un contexte qui aura changé entre-temps.
    _clear_action_fields(payload)

    if action is None:
        logger.warning(
            "cart_management: agent_action %r rejected against context "
            "expected_action=%s (producer_ids=%s tier_ids=%s)",
            raw.get("action"),
            context.expected_action,
            context.producer_ids(),
            context.tier_ids(),
        )
        return None

    vendor_ctx = dict(state.get("vendor_selection_context") or {})
    tier_ctx = dict(state.get("tier_selection_context") or {})

    if action.action == ActionType.SELECT_PRODUCER:
        vendors_list = vendor_ctx.get("vendors") or []
        # Résolution par OFFRE (jamais par producteur seul : un producteur peut
        # avoir plusieurs offres dans le même menu — incident 2026-09-28,
        # « 3 » retombait sur la 1ʳᵉ offre de Gilbert-prod).
        chosen = next(
            (
                v
                for position, v in enumerate(vendors_list, start=1)
                if isinstance(v, dict)
                and vendor_offer_id(v, position) == action.offer_id
            ),
            None,
        )
        if chosen is None:
            return None
        logger.info(
            "BUYER_OFFER_SELECTED | menu_id=%s | option_index=%s | product_id=%s | producer_id=%s",
            vendor_ctx.get("menu_id"),
            chosen.get("display_index"),
            chosen.get("product_id"),
            chosen.get("producer_id"),
        )
        vendor_ctx["chosen_vendor"] = chosen
        vendor_ctx.pop("resolved_tier_id", None)
        payload["product"] = chosen.get("name") or product_name
        display_name = chosen.get("name") or product_name

        raw_tiers = chosen.get("pricing_tiers")
        if isinstance(raw_tiers, list) and raw_tiers:
            return _build_tier_menu_response(
                state,
                raw_tiers,
                display_name,
                chosen.get("product_id"),
                chosen.get("name") or "",
                vendor_ctx,
            )

        # « je prends Gilbert, 10 litres » : la désignation ET la quantité dites dans la même phrase — on enchaîne sur l'ajout
        # (le domaine, `cart_management`, valide unité/stock/minimum) au lieu de redemander ce que l'utilisateur vient de dire.
        said_quantity = raw.get("quantity")
        if isinstance(said_quantity, (int, float)) and said_quantity > 0:
            payload["quantity"] = said_quantity
            if raw.get("unit"):
                payload["unit"] = raw.get("unit")
            logger.info("interaction_mode=NATURAL_REFERENCE | multi_slot=producer+quantity | quantity=%s", said_quantity)
            return await cart_management(
                {
                    **state,
                    "current_goal": "BUYER_ADD_TO_CART",
                    "vendor_selection_context": vendor_ctx,
                    "transaction_payload": payload,
                },
                mc_runtime,
            )

        vendor_label = chosen.get("vendor_name") or "ce producteur"
        unit_hint = chosen.get("unit") or "KG"
        price_hint = chosen.get("price")
        price_info = (
            f" (prix : {chosen['pricing_label']})"
            if chosen.get("pricing_label")
            else (f" (prix : {price_hint} FCFA/{unit_hint})" if price_hint else "")
        )
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                f"👤 Vous avez choisi *{vendor_label}* pour *{display_name}*"
                f"{price_info}.\n\n📦 Quelle quantité souhaitez-vous ?\n"
                f"💡 _Exemples : 50 {unit_hint.lower()}, 2 sacs, 100 kg..._\n"
                f"{_change_producer_hint(len(vendors_list))}"
            ).rstrip("\n"),
            "transaction_payload": payload,
            "vendor_selection_context": vendor_ctx,
            # (B7) menu producteur RÉSOLU : sa liste ne survit plus comme `expected_candidates`.
            "expected_candidates": [],
            "tier_selection_context": None,
            "ag_ui_component": None,
        }

    if action.action == ActionType.SELECT_PRICING_TIER:
        chosen_vendor = vendor_ctx.get("chosen_vendor")
        raw_tiers = tier_ctx.get("tiers")
        if not isinstance(raw_tiers, list) or not raw_tiers:
            raw_tiers = (
                chosen_vendor.get("pricing_tiers")
                if isinstance(chosen_vendor, dict)
                else None
            ) or []
        tier = next(
            (
                t
                for t in raw_tiers
                if isinstance(t, dict)
                and str(t.get("tier_id")) == action.pricing_tier_id
            ),
            None,
        )
        if tier is None:
            return None

        vendor_ctx["resolved_tier_id"] = action.pricing_tier_id
        # Option 1 (audit 2026-09-01, "aucune conversion silencieuse quantité
        # globale → nombre de paquets") : toute quantité/unité donnée AVANT
        # ou PENDANT la sélection du palier ("je veux 30 L de lait", puis "2")
        # décrit une intention pré-palier, jamais un nombre de paquets déjà
        # répondu — la purger ici est ce qui force la question explicite
        # "combien de bidons ?" juste en dessous, au lieu de la réutiliser en
        # silence. `agent_action`/`action_*` étant PAR CONSTRUCTION le seul
        # contenu utile de ce tour (voir la règle prompt "jamais quantity/unit
        # en plus d'un agent_action"), aucune quantité légitime de CE tour ne
        # peut être perdue par cette purge.
        payload["quantity"] = None
        payload["unit"] = None
        vendor_ctx["requested_quantity"] = None
        vendor_ctx["requested_unit"] = None
        product_id = tier_ctx.get("product_id") or (
            chosen_vendor.get("product_id") if isinstance(chosen_vendor, dict) else None
        )
        product_label = tier_ctx.get("product_name") or (
            str((chosen_vendor or {}).get("name") or product_name or "")
            .strip()
            .lower()
        )
        tier_selection_context = {
            "product_id": product_id,
            "product_name": product_label,
            "tiers": raw_tiers,
            "resolved_tier_id": action.pricing_tier_id,
        }
        vendor_label = (
            chosen_vendor.get("vendor_name")
            if isinstance(chosen_vendor, dict)
            else None
        ) or "ce producteur"
        tier_label = f"{tier.get('quantity')} {tier.get('unit')}" + (
            f" ({tier['packaging']})" if tier.get("packaging") else ""
        )
        switch_ack = ""
        if context.active_tier_id and context.active_tier_id != action.pricing_tier_id:
            switch_ack = "🔁 Changement de conditionnement pris en compte : "
        # « je prends le sachet de 500 ml, j'en veux 5 » : conditionnement ET nombre de paquets dits ensemble -> on ajoute
        # directement (le domaine valide stock/minimum) au lieu de poser deux questions de plus.
        said_count = raw.get("package_count")
        if isinstance(said_count, (int, float)) and said_count > 0 and isinstance(chosen_vendor, dict):
            logger.info("interaction_mode=NATURAL_REFERENCE | multi_slot=tier+package_count | count=%s", said_count)
            return await cart_service.add_to_cart_with_ref(
                phone,
                str(product_name),
                said_count,
                chosen_vendor,
                cart,
                {**state, "tier_selection_context": tier_selection_context, "vendor_selection_context": vendor_ctx},
                buyer_unit=None,
                tier_id=action.pricing_tier_id,
            )
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                f"{switch_ack}✅ Vous avez choisi *{tier_label}* chez "
                f"*{vendor_label}* ({tier.get('price')} FCFA).\n\n"
                f"📦 Combien de *{tier_label}* souhaitez-vous ?"
            ),
            "transaction_payload": payload,
            "vendor_selection_context": vendor_ctx,
            "tier_selection_context": tier_selection_context,
            # Le palier est RÉSOLU (on demande le nombre de paquets, pas un
            # nouveau choix) — contrairement à `_build_tier_menu_response`
            # (qui AFFICHE le menu et doit revendiquer `available_mapping_kind
            # = "pricing_tier"`), rien ici ne doit plus protéger un menu
            # numéroté déjà refermé. Même discipline que `_tier_resolved_patch`.
            "working_memory": {
                **dict(state.get("working_memory") or {}),
                "available_mapping_kind": None,
                "menu_snapshot_id": None,
            },
            "ag_ui_component": None,
        }

    if action.action == ActionType.SET_PACKAGE_COUNT:
        chosen_vendor = vendor_ctx.get("chosen_vendor")
        if not isinstance(chosen_vendor, dict) or not context.active_tier_id:
            return None
        return await cart_service.add_to_cart_with_ref(
            phone,
            str(product_name),
            action.package_count,
            chosen_vendor,
            cart,
            state,
            # Un `package_count` validé par le contrat est PAR CONSTRUCTION
            # sans dimension (voir domain/selection_actions.py::validate_action)
            # — jamais une unité héritée d'un tour antérieur.
            buyer_unit=None,
            tier_id=context.active_tier_id,
        )

    if action.action == ActionType.SET_QUANTITY:
        chosen_vendor = vendor_ctx.get("chosen_vendor")
        if not isinstance(chosen_vendor, dict):
            return None
        return await cart_service.add_to_cart_with_ref(
            phone,
            str(product_name),
            action.quantity,
            chosen_vendor,
            cart,
            state,
            buyer_unit=action.unit,
            tier_id=None,
        )

    return None


def _change_text(outcome: EditOutcome) -> str:
    if outcome.field == "REMOVE":
        return "✅ Ligne retirée du panier."
    label = "Nombre de paquets" if outcome.field == "PACKAGE_COUNT" else "Quantité"
    return f"✅ {label} mise à jour : {outcome.old_value:g} → {outcome.new_value:g}."


async def _edit_cart_line(
    state: Dict[str, Any],
    mc_runtime: Any,
    payload: Dict[str, Any],
    cart: List[Dict[str, Any]],
    cart_service: CartDomainService,
    phone: str,
) -> Dict[str, Any]:
    """BUYER_EDIT_CART : modifie UNE ligne du panier par une commande de domaine (`CartDomainService.edit_cart_line`).

    Précommande déjà préparée (brouillon `DRAFT`) : le brouillon est RECOMPOSÉ depuis le panier édité (nouvelle version, ancien ordre SUPERSEDED) — l'ancienne
    confirmation devient périmée (`STALE_TARGET`), le récapitulatif est RÉAFFICHÉ et une confirmation FRAÎCHE est exigée : un « oui » donné pour l'ancienne quantité
    ne confirme jamais la nouvelle. Brouillon engagé (EXECUTING/EXECUTED…) : aucune modification silencieuse."""
    raw = payload.get("cart_edit") or (state.get("extracted_entities") or {}).get("cart_edit")
    logger.info("business_edit_requested | entity=cart_line | has_spec=%s", bool(raw))
    try:
        spec = CartEditSpec.model_validate(raw)
    except Exception:  # noqa: BLE001 - édition illisible : on demande, on ne devine pas
        return {
            "status": "WAITING_INPUT",
            "response_strategy": "SUCCESS",
            "final_response": "Qu'est-ce que tu veux modifier : la quantité d'un article, ou le retirer du panier ?",
            "ag_ui_component": None,
        }

    if spec.field in (CartField.QUANTITY, CartField.PACKAGE_COUNT) and spec.value is not None:
        # le modèle n'est pas l'autorité : « 280 francs le kg » ne devient jamais une quantité de ligne (le prix d'une ligne ne s'édite pas ici)
        check = validate_structured_edit(
            field="quantity" if spec.field == CartField.QUANTITY else "package_count",
            value=spec.value,
            unit=spec.unit,
            text=state.get("normalized_text") or state.get("user_query"),
        )
        if check.verdict is EditVerdict.RECLASSIFIED or check.verdict is EditVerdict.CLARIFY:
            logger.info("business_edit_unsafe_blocked | entity=cart_line | reason=%s", check.reason)
            return {
                "status": "WAITING_INPUT",
                "response_strategy": "SUCCESS",
                "final_response": "Je ne peux pas changer le prix d'un article ici. Dis-moi la *quantité* voulue (ex : « mets 20 litres »), ou « retire » pour l'enlever.",
                "ag_ui_component": None,
            }

    draft_dict = state.get("preorder_draft")
    if isinstance(draft_dict, dict) and draft_dict:
        draft_status = str(draft_dict.get("status") or "DRAFT").upper()
        if draft_status != "DRAFT":
            logger.info("business_edit_rejected | entity=cart_line | reason=preorder_not_editable | status=%s", draft_status)
            return {
                "status": "WAITING_INPUT",
                "response_strategy": "SUCCESS",
                "final_response": "Ta précommande est déjà engagée : je ne peux plus la modifier ici. Dis « annuler » si tu veux la reprendre.",
                "ag_ui_component": None,
            }

    _viewed = (state.get("working_memory") or {}).get("cart_viewed_version")
    outcome = await cart_service.edit_cart_line(phone, spec, cart, state, expected_version=int(_viewed) if isinstance(_viewed, (int, float)) else None)
    meta = outcome.meta
    base: Dict[str, Any] = {"status": "COMPLETED", "preorder_workflow": {"phase": "CART"}, "ag_ui_component": None}

    if outcome.status in (EditStatus.NOT_FOUND, EditStatus.AMBIGUOUS, EditStatus.REJECTED, EditStatus.CONFLICT):
        logger.info("business_edit_%s | entity=cart_line | status=%s", "conflict" if outcome.status == EditStatus.CONFLICT else "rejected", outcome.status.value)
        render = cart_service.render_cart_menu(outcome.cart, meta) if outcome.status == EditStatus.CONFLICT else {}
        text = outcome.message or "Je n'ai pas pu appliquer cette modification."
        if render.get("final_response"):
            text = f"{text}\n\n{render['final_response']}"
        return {**base, "response_strategy": "SUCCESS", "status": "WAITING_INPUT", "final_response": text, "active_cart": outcome.cart, "cart_meta": meta}

    patch: Dict[str, Any] = {**base, "active_cart": outcome.cart, "cart_meta": meta, "response_strategy": "SELECTION_MENU" if outcome.cart else "SUCCESS"}

    if outcome.status == EditStatus.APPLIED and isinstance(draft_dict, dict) and draft_dict and outcome.cart:
        from ladini.graphs.agents.market_coach.flows.buyer.preorder import (
            cart_to_items_payload,
        )
        from ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation import (
            bootstrap_preorder_draft,
        )

        edited_state = {**state, "active_cart": outcome.cart, "cart_meta": meta}
        recap = await bootstrap_preorder_draft(edited_state, mc_runtime, items_payload=cart_to_items_payload(outcome.cart), meta=meta)
        logger.info("confirmation_invalidated | entity=preorder_draft | reason=cart_line_edited")
        recap["active_cart"] = outcome.cart
        recap["cart_meta"] = meta
        recap["working_memory"] = {**(state.get("working_memory") or {}), **(recap.get("working_memory") or {}), "cart_viewed_version": meta["version"]}
        recap["final_response"] = f"{_change_text(outcome)}\n\n{recap.get('final_response') or ''}".strip()
        return recap

    render = cart_service.render_cart_menu(outcome.cart, meta)
    head = "Rien à changer : c'est déjà cette valeur." if outcome.status == EditStatus.UNCHANGED else _change_text(outcome)
    patch["final_response"] = f"{head}\n\n{render.get('final_response') or ''}".strip()
    return patch


async def cart_management(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Cart management node — handles add-to-cart, view, and vendor selection.

    Deterministic routing based on goal and available entities:
    - BUYER_VIEW_CART → render cart
    - BUYER_ADD_TO_CART → resolve product, check vendors, add line
    - Free-text preorder keyword → delegate to preorder workflow
    """
    goal = (state.get("current_goal") or "").upper()
    event = str(state.get("interpreted_event") or "").upper().strip()
    user_text = str(state.get("normalized_text") or state.get("user_query") or "")

    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    if not payload and state.get("extracted_entities"):
        payload = dict(state.get("extracted_entities"))

    phone = str(state.get("user_phone") or "")
    cart: List[Dict[str, Any]] = list(state.get("active_cart") or [])
    stable_entities = state.get("stable_entities") or {}
    cart_service = CartDomainService(mc_runtime)

    def _with_base(extra: Dict[str, Any]) -> Dict[str, Any]:
        base: Dict[str, Any] = {"active_cart": cart}
        if goal and "current_goal" not in extra:
            base["current_goal"] = goal
            if "goal_status" not in extra:
                base["goal_status"] = "ACTIVE"
        if "final_response" not in extra:
            base["final_response"] = None
        if "ag_ui_component" not in extra:
            base["ag_ui_component"] = None
        base.update(extra)

        # Persist the last known cart snapshot in working_memory to survive
        # cross-goal transitions (ex: précommande). This is cheap (replace_list)
        # and prevents empty payloads when the planner reroutes via INIT.
        snapshot = base.get("active_cart") or cart
        if snapshot:
            wm_patch = dict(base.get("working_memory") or {})
            wm_patch["last_active_cart"] = snapshot
            base["working_memory"] = wm_patch
        _meta_out = base.get("cart_meta")
        if isinstance(_meta_out, dict) and _meta_out.get("version") is not None:
            # Version du panier que l'utilisateur VOIT : une édition ultérieure est refusée (CONFLICT) si le panier a changé depuis.
            wm_view = dict(base.get("working_memory") or state.get("working_memory") or {})
            wm_view["cart_viewed_version"] = _meta_out["version"]
            base["working_memory"] = wm_view

        return base

    # --- ACTIONS DE MENU (affiner / montrer les autres / aucun ne convient) : mêmes handlers que la recherche, quel que soit le parcours qui a affiché le menu
    #     (demande complète avec quantité = `cart_management`, demande simple = `buyer_request_resolver`) ---
    _menu_action = str(((state.get("extracted_entities") or {}).get("menu_action")) or "").upper()
    _vctx_live = state.get("vendor_selection_context")
    if _menu_action in {"SHOW_MORE", "REFINE", "NONE_OF_THESE"} and isinstance(_vctx_live, dict) and _vctx_live.get("vendors") and not _vctx_live.get("chosen_vendor"):
        from ladini.graphs.agents.market_coach.flows.buyer import (
            procurement as _procurement,
        )

        if _menu_action == "SHOW_MORE":
            return _with_base(_procurement._show_more_offers(state, mc_runtime, _vctx_live))
        if _menu_action == "REFINE":
            return _with_base(_procurement._refine_offers(state, mc_runtime, _vctx_live))
        return _with_base(_procurement._none_of_these(_vctx_live))

    # --- Free-text preorder trigger ---
    cart_action = detect_cart_action(state)
    if cart_action == "PREORDER" and cart:
        logger.info("cart_management: preorder command detected via free text")
        next_state = dict(state)
        next_state["current_goal"] = "BUYER_PREORDER_INIT"
        next_state["active_cart"] = cart
        next_state["transaction_payload"] = dict(payload)
        preorder_patch = await create_preorder(next_state, mc_runtime)
        return _with_base(preorder_patch)

    # --- Resolve entities ---
    product_name = resolve_product(payload, stable_entities, state)

    # Recover product name from an active vendor context BEFORE the missing-field
    # guards. When the buyer already picked a vendor (or a single vendor was
    # seeded) the product lives in vendor_selection_context, not always in the
    # payload — without this the flow would wrongly re-ask for the product.
    if not product_name:
        _vctx = state.get("vendor_selection_context")
        if isinstance(_vctx, dict) and not _vctx.get("__reset__"):
            _chosen = _vctx.get("chosen_vendor")
            product_name = (
                _chosen.get("name") if isinstance(_chosen, dict) else None
            ) or _vctx.get("product")

    if product_name:
        payload.setdefault("product", product_name)

    quantity = resolve_quantity(payload, stable_entities)

    # Même logique que ci-dessus, pour la quantité : un "oui" confirmant un
    # match incertain (voir needs_confirmation plus bas) n'a pas de quantité
    # dans CE tour — elle a été donnée dans le message précédent et vit dans
    # `requested_quantity`. Sans ce garde-fou, le "Missing product or
    # quantity" ci-dessous redemandait la quantité AVANT même d'atteindre la
    # logique de confirmation, qui ne s'exécute que plus bas. Voir
    # [[buyer-search-fuzzy-match-safety-2026-08]].
    if quantity in (None, "", 0):
        _vctx_qty = state.get("vendor_selection_context")
        if isinstance(_vctx_qty, dict) and not _vctx_qty.get("__reset__"):
            _requested_qty = _vctx_qty.get("requested_quantity")
            if _requested_qty not in (None, "", 0):
                quantity = _requested_qty
                payload["quantity"] = _requested_qty

    # --- EDIT CART LINE : la conversation a interprété l'édition, le DOMAINE l'exécute ---
    if goal == "BUYER_EDIT_CART":
        return _with_base(await _edit_cart_line(state, mc_runtime, payload, cart, cart_service, phone))

    # --- VIEW CART ---
    if goal == "BUYER_VIEW_CART":
        meta = CartDomainService.recompute_cart_meta(cart)
        meta["version"] = int((state.get("cart_meta") or {}).get("version") or 0)  # la consultation ne change pas la version
        pending_draft = state.get("draft_payload")
        if not pending_draft or pending_draft.get("__reset__"):
            pending_draft = state.get("suspended_payload")
        render = cart_service.render_cart_menu(cart, meta, pending_draft=pending_draft)
        return _with_base(
            {
                "status": "COMPLETED",
                "response_strategy": "SELECTION_MENU" if cart else "SUCCESS",
                "preorder_workflow": {"phase": "CART"},
                "cart_meta": meta,
                **render,
            }
        )

    # --- Guard: insufficient info for non-view goals ---
    if goal not in {"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"} and not (
        product_name and quantity not in (None, "", 0)
    ):
        cart_hint = (
            f" Vous avez {len(cart)} article(s) dans votre panier." if cart else ""
        )
        return _with_base(
            {
                "status": "PLANNING",
                "final_response": (
                    f"🛒 Comment puis-je vous aider ?{cart_hint}\n\n"
                    "💡 _Dites par exemple :_\n"
                    "• *50 kg de maïs* — pour ajouter au panier\n"
                    "• *mon panier* — pour voir votre panier\n"
                    "• *précommander* — pour valider votre commande"
                ),
                "ag_ui_component": None,
            }
        )

    # --- CONTRAT D'ACTION STRUCTURÉE (2026-09-01) ---
    # Priorité absolue sur toute la mécanique historique ci-dessous : quand
    # routing.py (LLM ou FastPath) a produit un `agent_action` pour CE tour,
    # c'est l'UNIQUE interprétation retenue — voir
    # domain/selection_actions.py et le bloc `_execute_selection_action`
    # au-dessus. Ne s'active que si le tunnel producteur/palier/paquets est
    # réellement actif (`vendor_selection_context` présent) ; sinon aucune
    # action structurée n'a de sens et on ne paie même pas le coût de la
    # construire.
    if state.get("vendor_selection_context"):
        action_patch = await _execute_selection_action(
            state, mc_runtime, payload, cart, cart_service, phone, product_name
        )
        if action_patch is not None:
            return _with_base(action_patch)

    # --- VENDOR SELECTION: check if returning from vendor menu ---
    #
    # Bug réel (2026-08-17) : ce bloc vivait APRÈS le garde "Missing product
    # or quantity" ci-dessous. Tant que la quantité n'était pas encore
    # donnée (le cas NORMAL juste après l'affichage du menu producteurs),
    # ce garde renvoyait TOUJOURS en premier, empêchant selection_index de
    # jamais être résolu en chosen_vendor sur le tour où l'acheteur répond
    # réellement au menu (ou tente d'en choisir un autre). selection_index
    # restait "collant" (voir nodes/memory.py::mapping_kind=="product_vendor")
    # et n'était consommé que plus tard, sur le tour où la quantité arrivait
    # enfin — appliquant alors une sélection potentiellement PÉRIMÉE (un
    # changement d'avis entre-temps écrasait silencieusement le choix
    # précédent, sans jamais être confirmé à l'acheteur). Déplacé AVANT le
    # garde de quantité pour que la sélection/le changement de producteur
    # soit résolu et confirmé sur le MÊME tour où il est demandé — exactement
    # le comportement que le commentaire de memory.py suppose déjà
    # ("cart_management pops it itself once it has successfully located the
    # vendor").
    vendor_ctx = state.get("vendor_selection_context")
    vendor_ctx_active = bool(vendor_ctx) and not (
        isinstance(vendor_ctx, dict) and vendor_ctx.get("__reset__")
    )
    if vendor_ctx_active:
        vendor_ctx_payload: Dict[str, Any] = dict(vendor_ctx or {})
        chosen_vendor = vendor_ctx_payload.get("chosen_vendor")
        previous_vendor = chosen_vendor
        # Robustness: recover product name from the vendor context if it did not
        # survive in the payload (avoids resolve_product_vendors("None")).
        if not product_name:
            product_name = (
                chosen_vendor.get("name") if isinstance(chosen_vendor, dict) else None
            ) or vendor_ctx_payload.get("product")
            if product_name:
                payload["product"] = product_name
        selection_idx = payload.get("selection_index")
        vendor_switched = False

        # (B8) numéro de conditionnement HORS PLAGE (« 5 », « 50 » avec 2 paliers) : message explicite,
        # menu inchangé, AUCUNE mutation (ni quantité, ni nombre de paquets, ni palier, ni vendeur).
        _raw_chosen = vendor_ctx_payload.get("chosen_vendor")
        _chosen_now: Dict[str, Any] = _raw_chosen if isinstance(_raw_chosen, dict) else {}
        _tiers_now = [t for t in (_chosen_now.get("pricing_tiers") or []) if isinstance(t, dict)]
        if (
            (state.get("raw_analysis") or {}).get("path") == "deterministic_invalid_tier_index"
            and len(_tiers_now) > 1
        ):
            return _with_base(
                _build_tier_menu_response(
                    state,
                    _tiers_now,
                    str(_chosen_now.get("name") or product_name or ""),
                    _chosen_now.get("product_id"),
                    str(_chosen_now.get("name") or ""),
                    vendor_ctx_payload,
                    note=f"⚠️ Je n'ai que {len(_tiers_now)} conditionnements disponibles." + chr(10) * 2,
                )
            )

        # (B7) « changer producteur » (sans numéro) pendant le slot quantité : on ré-affiche le menu
        # producteur VIVANT (mêmes candidats, aucun nouveau `search_products`).
        _vendors_live = [v for v in (vendor_ctx_payload.get("vendors") or []) if isinstance(v, dict)]
        if (
            (state.get("raw_analysis") or {}).get("path") == "deterministic_change_producer"
            and len(_vendors_live) > 1
        ):
            _menu_patch, _menu = cart_service.build_product_selection_menu(
                str(product_name or vendor_ctx_payload.get("product") or ""),
                _vendors_live,
                extra_context={
                    "requested_quantity": vendor_ctx_payload.get("requested_quantity"),
                    "requested_unit": vendor_ctx_payload.get("requested_unit"),
                },
                phone=phone,
            )
            return _with_base(_menu_patch)

        # (2026-08-30) Incident réel : une sélection de PALIER en cours
        # (`tier_selection_context` actif pour CE produit) se faisait
        # intercepter ici — le code de sélection VENDEUR consommait
        # aveuglément `selection_index` en le validant contre la liste des
        # VENDEURS (souvent un seul élément), renvoyant "Numéro invalide"
        # dès que l'index dépassait 1. Le vendeur est déjà figé à ce stade
        # (`chosen_vendor` déjà résolu) — un `selection_index` reçu pendant
        # que le palier est en attente appartient au menu de PALIERS, jamais
        # au menu vendeur. Voir le bloc de résolution de palier plus bas.
        _tier_ctx_pending = state.get("tier_selection_context")
        # (2026-08-31) Incident réel : un palier ne peut être légitimement
        # "en attente" QUE si un vendeur a DÉJÀ été résolu sur un tour
        # précédent (`chosen_vendor` déjà présent dans le contexte
        # persisté) — un menu de paliers n'existe structurellement pas tant
        # qu'aucun vendeur n'a été choisi. Sans ce garde, un
        # `tier_selection_context` PÉRIMÉ (laissé par un achat précédent
        # d'un produit totalement différent, ex: "lait" avant "poulets" —
        # canal `replace_value`, jamais effacé implicitement) empêchait à
        # tort la résolution du `selection_index` reçu pour CHOISIR un
        # vendeur, faisant échouer toute la sélection.
        # (2026-09-01) `tier_selection_context` survit désormais APRÈS la
        # résolution du palier (estampillé `resolved_tier_id`, voir
        # `_tier_resolved_patch`) pour permettre un changement d'avis. Un
        # contexte ainsi résolu n'attend PLUS de réponse : il ne doit donc plus
        # bloquer la résolution d'un `selection_index` destiné au menu VENDEUR
        # (régression sinon : "changer de producteur" cassé pendant toute la
        # phase nombre-de-paquets).
        _tier_selection_pending = (
            bool(_tier_ctx_pending)
            and not (
                isinstance(_tier_ctx_pending, dict) and _tier_ctx_pending.get("__reset__")
            )
            and not (
                isinstance(_tier_ctx_pending, dict)
                and _tier_ctx_pending.get("resolved_tier_id")
            )
            and isinstance(chosen_vendor, dict)
        )

        # (B8) une commande producteur EXPLICITE (« producteur 3 ») prime sur le menu de paliers en
        # attente : son numéro désigne un PRODUCTEUR, jamais un conditionnement.
        if (state.get("raw_analysis") or {}).get("path") == "deterministic_producer_command":
            _tier_selection_pending = False
            vendor_ctx_payload.pop("resolved_tier_id", None)
            # L'index vient des entités de CE tour : après la résolution d'un palier, le drapeau de
            # mapping de menu est volontairement effacé et `memory_update` ne le fusionne plus.
            _cmd_idx = (state.get("extracted_entities") or {}).get("selection_index")
            if _cmd_idx is not None:
                selection_idx = _cmd_idx
        if selection_idx is not None and not _tier_selection_pending:
            vendors_list = vendor_ctx_payload.get("vendors") or []
            try:
                idx = int(selection_idx) - 1
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(vendors_list):
                chosen_vendor = vendors_list[idx]
                vendor_ctx_payload["chosen_vendor"] = chosen_vendor
                # `transaction_payload` est un canal `merge_dict` : un
                # `payload.pop(...)` local ne se propage PAS à l'état
                # persisté (une clé absente de `new` ne supprime rien de
                # `old`, voir `agents/reducers.py::merge_dict`) — seule une
                # affectation explicite à `None` "efface" réellement la clé
                # au tour suivant. Incident réel (2026-08-30) : un
                # `selection_index` ainsi "popé" mais jamais vraiment effacé
                # survivait dans l'état persisté et se faisait ré-consommer
                # au tour suivant comme un index producteur périmé.
                payload["selection_index"] = None
                if chosen_vendor:
                    payload["product"] = chosen_vendor.get("name") or product_name
                    product_name = payload["product"]

                # Un changement RÉEL de producteur (pas la toute première
                # sélection après le menu) doit être confirmé explicitement —
                # sans quoi l'acheteur n'a AUCUN moyen de savoir que sa
                # demande ("je choisis le deuxième") a bien été prise en
                # compte, ni lequel a été retenu (bug réel 2026-08-17).
                if (
                    isinstance(previous_vendor, dict)
                    and isinstance(chosen_vendor, dict)
                    and (
                        previous_vendor.get("producer_id"),
                        previous_vendor.get("product_id"),
                        previous_vendor.get("unit"),
                    )
                    != (
                        chosen_vendor.get("producer_id"),
                        chosen_vendor.get("product_id"),
                        chosen_vendor.get("unit"),
                    )
                ):
                    vendor_switched = True

                requested_qty = vendor_ctx_payload.get("requested_quantity")
                requested_unit = vendor_ctx_payload.get("requested_unit")
                if quantity in (None, "", 0) and requested_qty not in (None, "", 0):
                    payload["quantity"] = requested_qty
                    quantity = requested_qty
                if requested_unit:
                    payload.setdefault("unit", requested_unit)
            else:
                return _with_base(
                    {
                        "status": "WAITING_INPUT",
                        **set_pending_interaction(InteractionKind.SELECTION_MENU),
                        "response_strategy": "ASK_MISSING_FIELD",
                        "final_response": "Numéro invalide. Choisissez un producteur dans la liste ci-dessus, ou tapez *annuler*.",
                        "ag_ui_component": None,
                    }
                )

        switch_ack = ""
        if vendor_switched and isinstance(chosen_vendor, dict):
            # Mandat B2c.4 : `pricing_label` (certifié) prime sur `price`/`unit` bruts — sinon un
            # lot TOTAL_LOT ou un conditionnement s'afficherait comme "X FCFA/unité".
            vendor_price_label = chosen_vendor.get("pricing_label") or (
                f"{chosen_vendor.get('price')} FCFA/{chosen_vendor.get('unit') or 'KG'}"
            )
            switch_ack = (
                f"🔁 Changement pris en compte : vous avez maintenant choisi "
                f"*{chosen_vendor.get('vendor_name') or 'ce producteur'}* "
                f"(*{vendor_price_label}*).\n\n"
            )

        # --- TIER SELECTION (2026-08-30, voir domain/pricing_tiers.py) ---
        # Le produit choisi propose plusieurs paliers de prix/conditionnement
        # → il faut savoir LEQUEL avant d'ajouter au panier. Même pattern que
        # la sélection producteur ci-dessus : un contexte dédié
        # `tier_selection_context` survit tant que le palier n'est pas
        # choisi. La `quantity` déjà donnée par l'acheteur (ex: "3") est
        # réinterprétée comme le NOMBRE DE PAQUETS du palier retenu, jamais
        # une quantité en unité de base.
        selected_tier_id = None
        vendor_tiers: List[Dict[str, Any]] = []
        if chosen_vendor is not None and isinstance(chosen_vendor, dict):
            raw_tiers = chosen_vendor.get("pricing_tiers")
            if isinstance(raw_tiers, list) and raw_tiers:
                vendor_tiers = raw_tiers

        # (2026-08-30, refonte "palier avant quantité") : un palier peut déjà
        # avoir été résolu sur un tour PRÉCÉDENT — celui où on a demandé la
        # quantité APRÈS avoir montré le menu de paliers (voir la branche
        # "Vendor chosen but no quantity" plus bas, qui persiste
        # `resolved_tier_id` sur `vendor_ctx_payload` pour ce cas exact).
        # Priorité absolue : jamais re-demander/re-résoudre le palier si on
        # l'a déjà.
        _persisted_tier_id = vendor_ctx_payload.get("resolved_tier_id")
        if _persisted_tier_id and any(
            t.get("tier_id") == _persisted_tier_id for t in vendor_tiers
        ):
            selected_tier_id = _persisted_tier_id

        # (2026-08-30) Incident réel : matcher par `product_id` re-résolu à
        # chaque tour (une recherche catalogue FRAÎCHE, voir plus bas) s'est
        # avéré fragile en prod — plusieurs produits de test "lait" au même
        # prix pouvaient faire varier l'ordre des résultats d'un appel à
        # l'autre, cassant la comparaison et laissant le menu de paliers se
        # réafficher indéfiniment. `tier_selection_context` porte déjà la
        # liste EXACTE des paliers affichés au tour précédent — on lui fait
        # confiance directement plutôt que de re-matcher sur un identifiant
        # susceptible d'avoir changé entre deux recherches.
        tier_ctx = state.get("tier_selection_context")
        tier_ctx_active = bool(tier_ctx) and not (
            isinstance(tier_ctx, dict) and tier_ctx.get("__reset__")
        )
        # (2026-08-31) Incident réel : `tier_selection_context` est un canal
        # `replace_value` — RIEN ne l'efface tant qu'aucun code ne le
        # réécrit explicitement. Un palier laissé par un achat PRÉCÉDENT et
        # déjà validé (produit totalement différent, ex: "lait") survivait
        # ainsi et se faisait réutiliser ici pour LE PRODUIT ACTUEL (ex:
        # "poulets") dès qu'un vendeur était choisi — menu de paliers avec
        # les données de l'ANCIEN produit affiché sous le nom du nouveau.
        # Comparaison par NOM (jamais `product_id`, voir le commentaire
        # "incident réel 2026-08-30" plus haut sur l'instabilité
        # d'ordonnancement entre deux recherches du MÊME produit) : ne faire
        # confiance à `tier_ctx` que s'il porte bien le nom DU PRODUIT
        # ACTUELLEMENT résolu.
        # (B8) un CHANGEMENT DE PRODUCTEUR rend les paliers affichés périmés : ils appartiennent au
        # producteur précédent (même nom de produit « lait », autre offre) — jamais réutilisés.
        if tier_ctx_active and vendor_switched:
            tier_ctx_active = False
        if tier_ctx_active and isinstance(tier_ctx, dict):
            ctx_product_name = str(tier_ctx.get("product_name") or "").strip().lower()
            chosen_product_name = (
                str(chosen_vendor.get("name") or "").strip().lower()
                if isinstance(chosen_vendor, dict)
                else ""
            )
            if not ctx_product_name or ctx_product_name != chosen_product_name:
                tier_ctx_active = False
        if tier_ctx_active and isinstance(tier_ctx, dict):
            ctx_tiers = tier_ctx.get("tiers")
            if isinstance(ctx_tiers, list) and ctx_tiers:
                vendor_tiers = ctx_tiers
        # Distingue un palier résolu CE tour-ci (via `_persisted_tier_id`
        # plus haut, un tour ANTÉRIEUR où la question "combien de X ?" a
        # déjà été posée explicitement) d'un palier qui vient tout juste
        # d'être choisi maintenant — voir le garde anti-conversion-
        # silencieuse juste après ce bloc.
        _tier_freshly_resolved = False

        # --- RE-SÉLECTION EXPLICITE D'UN PALIER (audit 2026-09-01) ---
        # "2" puis "finalement je prends le bidon de 5 L" : sans ce bloc, la
        # deuxième phrase retombait en simple quantité (repro : 5 PAQUETS du
        # palier 10 L, soit 50 L / 4500 FCFA — le contraire de ce qui est
        # demandé). Seul un `selected_value` (le LLM a reconnu un palier
        # PRÉCIS de la liste, cf. règle 4bis du prompt) peut rouvrir le choix :
        # un chiffre nu reste un nombre de paquets, jamais un index de menu, à
        # ce stade de la machine à états.
        tier_switched = False
        if tier_ctx_active and isinstance(tier_ctx, dict) and selected_tier_id:
            _reselect_val = payload.get("selected_value")
            if (
                _reselect_val
                and _reselect_val != selected_tier_id
                and any(t.get("tier_id") == _reselect_val for t in vendor_tiers)
            ):
                logger.info(
                    "cart_management: tier re-selection %s -> %s",
                    selected_tier_id,
                    _reselect_val,
                )
                selected_tier_id = _reselect_val
                _tier_freshly_resolved = True
                tier_switched = True
                payload["selected_value"] = None
                vendor_ctx_payload["resolved_tier_id"] = selected_tier_id
                _new_tier = next(
                    (t for t in vendor_tiers if t.get("tier_id") == selected_tier_id),
                    {},
                )
                switch_ack += (
                    "🔁 Changement de conditionnement pris en compte : "
                    f"*{_new_tier.get('quantity')} {_new_tier.get('unit')}*"
                    + (
                        f" ({_new_tier['packaging']})"
                        if _new_tier.get("packaging")
                        else ""
                    )
                    + f" — {_new_tier.get('price')} FCFA.\n\n"
                )

        if tier_ctx_active and isinstance(tier_ctx, dict) and not selected_tier_id:
            # `selected_value` (2026-08-30, refonte "LLM pilote la sélection
            # de palier") : le mapping sémantique texte libre → palier est
            # maintenant fait par le LLM lui-même (voir la liste des paliers
            # injectée dans son prompt, `interpreter/routing.py`) — porte
            # directement le `tier_id` retenu. On ne fait ici QUE valider
            # que cet id correspond bien à un palier de la liste ACTUELLEMENT
            # affichée avant de le faire confiance (jamais un id inventé).
            tier_sel_val = payload.get("selected_value")
            if tier_sel_val and any(
                t.get("tier_id") == tier_sel_val for t in vendor_tiers
            ):
                selected_tier_id = tier_sel_val
                _tier_freshly_resolved = True
                # `merge_dict` ne supprime pas une clé absente de `new` —
                # voir le commentaire détaillé plus haut dans ce fichier
                # (résolution vendeur). Affectation explicite à `None`.
                payload["selected_value"] = None
            tier_sel_idx = payload.get("selection_index")
            if selected_tier_id is None and tier_sel_idx is not None:
                try:
                    t_idx = int(tier_sel_idx) - 1
                except (TypeError, ValueError):
                    t_idx = -1
                if 0 <= t_idx < len(vendor_tiers):
                    selected_tier_id = vendor_tiers[t_idx].get("tier_id")
                    _tier_freshly_resolved = True
                    payload["selection_index"] = None

        # Option 1 (audit 2026-09-01, "aucune conversion silencieuse
        # quantité globale → nombre de paquets") : incident réel vérifié —
        # un acheteur ayant donné "30 litre" AVANT même de voir le menu de
        # paliers se voyait facturer 30 BIDONS de 10L (300L, 27000 FCFA) au
        # lieu des ~3 bidons attendus, la quantité globale pré-palier étant
        # silencieusement réutilisée comme nombre de paquets. Un palier tout
        # juste résolu CE tour ne peut faire confiance à `quantity` que si
        # elle a été donnée CE MÊME tour (ex: "3 bidons de 10L" en un seul
        # message) — jamais une quantité héritée d'un tour antérieur au
        # choix du palier. Sinon on l'ignore : la branche "quantité manquante"
        # juste en dessous pose alors explicitement la question du nombre de
        # paquets pour CE conditionnement précis.
        if _tier_freshly_resolved and selected_tier_id:
            _fresh_qty_this_turn = (state.get("extracted_entities") or {}).get(
                "quantity"
            )
            if not slot_has_value(_fresh_qty_this_turn):
                quantity = None
                payload["quantity"] = None
                vendor_ctx_payload["requested_quantity"] = None

        if vendor_tiers and not selected_tier_id:
            display_name = (
                chosen_vendor.get("name") if isinstance(chosen_vendor, dict) else None
            ) or product_name
            # Incident réel (2026-08-30) : `chosen_vendor` n'était résolu QUE
            # dans la copie locale `vendor_ctx_payload` (via selection_index
            # ci-dessus) — jamais renvoyé dans le patch d'état. Le tour
            # SUIVANT (réponse au menu de paliers) retrouvait donc un
            # `vendor_selection_context` SANS `chosen_vendor`, ce qui faisait
            # échouer les deux gardes "chosen_vendor is not None" plus bas
            # dans ce même bloc, tombait hors de `vendor_ctx_active` et
            # relançait une recherche vendeur fraîche — ré-affichant le menu
            # PRODUCTEUR à la place du menu de paliers, en boucle.
            _tier_menu_patch = _build_tier_menu_response(
                state,
                vendor_tiers,
                display_name,
                chosen_vendor.get("product_id")
                if isinstance(chosen_vendor, dict)
                else None,
                chosen_vendor.get("name") if isinstance(chosen_vendor, dict) else "",
                vendor_ctx_payload,
            )
            # (B8) l'index producteur (« producteur 4 ») vient d'être CONSOMMÉ : `merge_dict` ne
            # supprime pas une clé absente — sans cette mise à `None` explicite, il survit dans
            # l'état et peut être re-lu comme sélection au tour suivant.
            _tier_menu_patch["transaction_payload"] = {
                "selection_index": None,
                "selected_value": None,
            }
            return _with_base(_tier_menu_patch)

        # Vendor chosen + quantity available → add to cart directly
        if chosen_vendor is not None and product_name and quantity not in (None, "", 0):
            add_patch = await cart_service.add_to_cart_with_ref(
                phone,
                str(product_name),
                quantity,
                chosen_vendor,
                cart,
                state,
                buyer_unit=(
                    _fresh_unit_this_turn(state)
                    if selected_tier_id
                    else _declared_unit(payload, state)
                ),
                tier_id=selected_tier_id,
            )
            if switch_ack and add_patch.get("final_response"):
                add_patch["final_response"] = switch_ack + str(add_patch["final_response"])
            return _with_base(add_patch)

        # Vendor chosen but no quantity → ask for it, keep vendor context
        if chosen_vendor is not None and product_name and quantity in (None, "", 0):
            vendor_label = chosen_vendor.get("vendor_name") or "ce producteur"
            unit_hint = chosen_vendor.get("unit") or "KG"
            price_hint = chosen_vendor.get("price")
            price_info = (
                f" (prix : {chosen_vendor['pricing_label']})"
                if chosen_vendor.get("pricing_label")
                else (f" (prix : {price_hint} FCFA/{unit_hint})" if price_hint else "")
            )
            n_vendors = len(vendor_ctx_payload.get("vendors") or [])
            switch_hint = _change_producer_hint(n_vendors)
            # (2026-08-30, refonte "palier avant quantité") : si un palier
            # est déjà résolu à ce stade, la question DOIT porter sur ce
            # conditionnement précis ("combien de bidons de 10L ?"), jamais
            # une quantité générique — et le palier doit être PERSISTÉ sur
            # `vendor_ctx_payload` (`resolved_tier_id`) pour que le tour
            # suivant (la réponse quantité) le retrouve directement au lieu
            # de re-résoudre/re-afficher le menu de paliers.
            chosen_tier = None
            if selected_tier_id:
                chosen_tier = next(
                    (t for t in vendor_tiers if t.get("tier_id") == selected_tier_id),
                    None,
                )
                vendor_ctx_payload["resolved_tier_id"] = selected_tier_id
            if chosen_tier:
                tier_label = f"{chosen_tier.get('quantity')} {chosen_tier.get('unit')}" + (
                    f" ({chosen_tier['packaging']})" if chosen_tier.get("packaging") else ""
                )
                base_question = (
                    f"{switch_ack}"
                    f"👤 Vous avez choisi *{vendor_label}* — conditionnement *{tier_label}*"
                    f" ({chosen_tier.get('price')} FCFA).\n\n"
                    f"📦 Combien de *{tier_label}* souhaitez-vous ?"
                )
            else:
                base_question = (
                    f"{switch_ack}"
                    f"👤 Vous avez choisi *{vendor_label}* pour *{product_name}*{price_info}.\n\n"
                    f"📦 Quelle quantité souhaitez-vous ?\n"
                    f"💡 _Exemples : 50 {unit_hint.lower()}, 2 sacs, 100 kg..._\n"
                    f"{switch_hint}"
                )
            # Chantier résilience 2026-08 (volet acheteur) : c'est LE point
            # le plus souvent atteint en pratique (vendor_selection_context
            # persiste d'un tour à l'autre) — même correctif que le miroir
            # "Single vendor" plus bas dans ce fichier : final_response
            # précalculé court-circuite l'adaptivité générique de ask.py,
            # et ce chemin est réatteint sur une VRAIE déviation (vendeur
            # déjà choisi, quantité toujours absente).
            note = None
            if (
                event in {"UNKNOWN", "OUT_OF_SCOPE"}
                and user_text.strip()
                and not vendor_switched
                and not tier_switched
            ):
                note = await llm_deviation_reply(
                    mc_runtime, user_text, f"répondre à : quelle quantité de {product_name} souhaitez-vous ?",
                )
            return _with_base(
                {
                    "status": "WAITING_INPUT",
                    **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": f"{note}\n\n{base_question}" if note else base_question,
                    # Incident réel (2026-08-30, refonte "palier avant
                    # quantité") : `payload` a pu se faire retirer
                    # `selection_index`/`selected_value` PLUS HAUT dans ce
                    # même appel (résolution du palier, ou du vendeur) —
                    # mais cette branche ne renvoyait jamais `payload` mis à
                    # jour comme `transaction_payload`. Le canal persisté
                    # (merge_dict) gardait donc l'ANCIEN `selection_index`,
                    # qui se faisait ré-interpréter comme une sélection
                    # VENDEUR (ou re-consommer par erreur) au tour suivant —
                    # "Numéro invalide" ou un changement de vendeur fantôme.
                    "transaction_payload": payload,
                    "vendor_selection_context": vendor_ctx_payload,
                    # (B7) le menu producteur est RÉSOLU : sa liste ne doit plus survivre comme
                    # `expected_candidates` (un chiffre ne désigne plus un producteur ici).
                    "expected_candidates": [],
                    **_tier_resolved_patch(
                        state,
                        vendor_tiers,
                        chosen_vendor.get("name")
                        if isinstance(chosen_vendor, dict)
                        else product_name,
                        chosen_vendor.get("product_id")
                        if isinstance(chosen_vendor, dict)
                        else None,
                        selected_tier_id,
                    ),
                    "ag_ui_component": None,
                }
            )

    # --- Missing product ---
    # (2026-08-30, refonte "palier avant quantité") : la quantité n'est
    # PLUS un pré-requis pour lancer la recherche catalogue — sinon
    # l'acheteur ne peut jamais découvrir qu'un produit a plusieurs
    # conditionnements avant d'avoir déjà donné un chiffre à l'aveugle.
    # Seul le produit est requis ici ; la recherche ci-dessous détermine
    # elle-même si un palier doit être demandé avant la quantité.
    if not product_name:
        draft_snapshot = capture_cart_draft(state, payload)

        if quantity not in (None, "", 0):
            draft_item = {
                "product_id": "DRAFT",
                "name": "EN ATTENTE",
                "quantity": float(quantity),
                "unit": str(payload.get("unit") or "KG"),
                "price": 0.0,
                "line_total": 0.0,
                "status": "DRAFT",
            }
            cart = [
                c
                for c in cart
                if c.get("status") != "DRAFT" and c.get("product_id") != "DRAFT"
            ]
            cart.append(draft_item)

        vendor_ctx = state.get("vendor_selection_context")
        return _with_base(
            {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
                "response_strategy": "ASK_MISSING_FIELD",
                "missing_fields": ["product"],
                "last_missing_field": "product",
                "transaction_payload": payload,
                "draft_payload": draft_snapshot
                or state.get("draft_payload")
                or payload,
                "vendor_selection_context": vendor_ctx
                if vendor_ctx
                else state.get("vendor_selection_context"),
                "ag_ui_component": None,
            }
        )

    # --- MULTI-VENDOR RESOLUTION ---
    # Une panne de la recherche catalogue ne doit JAMAIS être rendue comme
    # « produit non disponible » (voir ProductLookupUnavailable) : ça affirme
    # à l'acheteur, à tort, que le produit n'existe pas et l'oriente vers un
    # appel d'offres inutile pour un produit pourtant en stock.
    #
    # `_prefetched_vendors` : `buyer_request_resolver` (flows/buyer/procurement.py)
    # a DÉJÀ appelé `resolve_product_vendors` ce tour-ci avant de nous déléguer
    # la main — on réutilise sa liste au lieu d'un second `search_products`
    # identique. Clé transitoire posée sur l'état synthétique passé à ce nœud,
    # jamais persistée.
    _prefetched = state.get("_prefetched_vendors")
    if not _prefetched:
        # (B6) menu producteur vivant + même produit répété : pas de second `search_products`.
        _prefetched = reusable_menu_vendors(state)
    if isinstance(_prefetched, list) and _prefetched:
        vendors = list(_prefetched)
        has_multiple = len(vendors) > 1
    else:
        try:
            vendors, has_multiple = await cart_service.resolve_product_vendors(
                phone, str(product_name)
            )
        except ProductLookupUnavailable:
            return _with_base(
                {
                    "status": "ERROR",
                    "response_strategy": "ERROR",
                    "final_response": (
                        f"🔌 Je n'arrive pas à consulter le catalogue pour « *{product_name}* » "
                        "en ce moment — c'est un souci technique de notre côté, pas une "
                        "absence de stock.\n\nRéessayez dans un instant."
                    ),
                    "ag_ui_component": None,
                }
            )

    # Un match qui n'est QUE trigram (pas de sous-texte réel entre le terme
    # cherché et le nom trouvé — voir _is_confident_product_match) n'est pas
    # un "peut-être" à faire confirmer : c'est du bruit. Incident réel
    # (2026-08-14) : "oeufs" fuzzy-matchait "Bœuf" alors que ni œufs ni
    # laitue n'existent en base — on écarte ces matches et on retombe sur le
    # flux "produit introuvable" existant plutôt que d'inventer une
    # suggestion. Voir [[buyer-search-fuzzy-match-safety-2026-08]].
    confident_vendors = [v for v in vendors if v.get("match_confident", True)]
    if len(confident_vendors) != len(vendors):
        logger.warning(
            "cart_management: dropped %d low-confidence match(es) for '%s' — treating as not found",
            len(vendors) - len(confident_vendors),
            product_name,
        )
    vendors = confident_vendors
    has_multiple = len(vendors) > 1

    # FLOW COMPRESSION : les contraintes dites dès le 1er message (prix max par unité, conditionnement) filtrent DÈS la première
    # recherche — rien n'est relâché en silence ; sans résultat, on le dit et on propose d'élargir.
    _constraints = constraints_from_payload(payload)
    _search_constraints = _constraints.to_dict()
    _search_pool: List[Dict[str, Any]] = []
    if vendors and not _constraints.is_empty:
        _search_pool = [dict(v) for v in vendors]  # résultats AVANT contraintes : retirer/remplacer une contrainte repart de ce pool (jamais une relaxation silencieuse)
        _report = apply_constraints(vendors, _constraints)
        logger.info(
            "flow_compression | constraints=%s | found=%s | kept=%s | excluded=%s",
            _search_constraints, len(vendors), len(_report.kept), _report.excluded,
        )
        if not _report.kept:
            return _with_base(
                {
                    "status": "WAITING_INPUT",
                    "response_strategy": "SUCCESS",
                    "final_response": no_result_message(_constraints, str(product_name), _report.excluded_count),
                    "working_memory": {
                        **dict(state.get("working_memory") or {}),
                        "last_search_constraints": _search_constraints,
                    },
                    "ag_ui_component": None,
                }
            )
        vendors = _report.kept
        has_multiple = len(vendors) > 1

    if not vendors:
        return _with_base(
            {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": (
                    f"📭 Le produit « *{product_name}* » n'est pas disponible dans notre catalogue actuellement.\n\n"
                    "💡 _Que souhaitez-vous faire ?_\n"
                    "• Tapez un autre nom de produit pour chercher\n"
                    "• Tapez *appel d'offres* pour demander aux producteurs\n"
                    "• Tapez *marketplace* pour voir les produits disponibles"
                ),
                "transaction_payload": {"__reset__": True},
                "negotiation_context": {"__reset__": True},
                "current_goal": None,
                "draft_payload": {"__reset__": True},
                "vendor_selection_context": {"__reset__": True},
                **clear_pending_interaction("no_vendor_available"),
                "ag_ui_component": None,
            }
        )

    if has_multiple:
        extra_context = {
            "requested_quantity": quantity,
            "requested_unit": payload.get("unit"),
            "search_constraints": _search_constraints,
            "search_pool": _search_pool,
        }
        state_patch, _menu = cart_service.build_product_selection_menu(
            str(product_name),
            vendors,
            extra_context=extra_context,
            phone=phone,
        )
        return _with_base(state_patch)

    # Single vendor
    ref = vendors[0]
    vendor_label = ref.get("vendor_name") or "un producteur"
    unit_hint = ref.get("unit") or "KG"
    price_hint = ref.get("price")
    price_info = (
        f" (prix : {ref['pricing_label']})"
        if ref.get("pricing_label")
        else (f" (prix : {price_hint} FCFA/{unit_hint})" if price_hint else "")
    )
    payload["product"] = product_name
    extras_hint = additional_products_hint(payload, state)

    # --- TIER RESOLUTION FIRST (2026-08-30, refonte "palier avant quantité") ---
    # Incident réel signalé par l'utilisateur : le système demandait une
    # quantité à l'aveugle AVANT même de révéler qu'il existe plusieurs
    # conditionnements — l'acheteur ne pouvait pas savoir à quoi son
    # chiffre allait s'appliquer. Le palier doit être choisi D'ABORD, la
    # quantité (nombre de paquets de CE palier) ensuite. Même mécanique de
    # résolution que la branche multi-vendeurs ci-dessus — voir
    # domain/pricing_tiers.py.
    single_selected_tier_id = None
    single_vendor_tiers = ref.get("pricing_tiers") or []
    # Même principe que la branche multi-vendeurs ci-dessus : on fait
    # confiance à `tier_selection_context` (la liste EXACTE déjà affichée),
    # pas à un re-matching par product_id sur une recherche fraîche — voir
    # le commentaire détaillé plus haut dans ce fichier.
    tier_ctx = state.get("tier_selection_context")
    tier_ctx_active = bool(tier_ctx) and not (
        isinstance(tier_ctx, dict) and tier_ctx.get("__reset__")
    )
    # (2026-08-31) Incident réel — voir le commentaire détaillé sur la
    # branche multi-vendeurs plus haut dans ce fichier : `tier_ctx` peut
    # appartenir à un produit totalement différent, déjà acheté sur un tour
    # précédent (canal `replace_value`, jamais effacé implicitement). Ne
    # faire confiance qu'au palier du produit ACTUELLEMENT résolu — par NOM
    # (jamais `product_id`, instable entre deux recherches du même produit,
    # voir le commentaire "incident réel 2026-08-30" plus haut).
    if tier_ctx_active and isinstance(tier_ctx, dict):
        ctx_product_name = str(tier_ctx.get("product_name") or "").strip().lower()
        current_product_name = str(ref.get("name") or product_name or "").strip().lower()
        if not ctx_product_name or ctx_product_name != current_product_name:
            tier_ctx_active = False
    if tier_ctx_active and isinstance(tier_ctx, dict):
        ctx_tiers = tier_ctx.get("tiers")
        if isinstance(ctx_tiers, list) and ctx_tiers:
            single_vendor_tiers = ctx_tiers
        # (2026-09-01) Un contexte estampillé `resolved_tier_id` décrit un
        # palier DÉJÀ choisi (voir `_tier_resolved_patch`) : si le tour retombe
        # ici parce que le contexte vendeur a été perdu, le menu ne doit pas se
        # réafficher — le choix tient toujours.
        _persisted = tier_ctx.get("resolved_tier_id")
        if _persisted and any(
            t.get("tier_id") == _persisted for t in single_vendor_tiers
        ):
            single_selected_tier_id = _persisted
        tier_sel_val = payload.get("selected_value")
        if tier_sel_val and any(
            t.get("tier_id") == tier_sel_val for t in single_vendor_tiers
        ):
            single_selected_tier_id = tier_sel_val
            # `merge_dict` ne supprime pas une clé absente de `new` —
            # affectation explicite à `None`, voir le commentaire détaillé
            # plus haut dans ce fichier (résolution vendeur).
            payload["selected_value"] = None
        tier_sel_idx = payload.get("selection_index")
        if single_selected_tier_id is None and tier_sel_idx is not None:
            try:
                t_idx = int(tier_sel_idx) - 1
            except (TypeError, ValueError):
                t_idx = -1
            if 0 <= t_idx < len(single_vendor_tiers):
                single_selected_tier_id = single_vendor_tiers[t_idx].get("tier_id")
                payload["selection_index"] = None

    # SAUT D'ÉTAPE : l'utilisateur a déjà dit le conditionnement voulu (« sachets de 500 ml ») et un seul palier y correspond —
    # le choix de palier n'a plus lieu d'être demandé. La quantité globale (« 20 L ») n'est PAS convertie en silence en nombre de
    # paquets (audit 2026-09-01) : on la retire et on demande le nombre de paquets, avec le calcul exact en SUGGESTION.
    _package_hint = ""
    if (
        isinstance(single_vendor_tiers, list)
        and len(single_vendor_tiers) == 1
        and not single_selected_tier_id
        and not _constraints.is_empty
        and (_constraints.package_type or _constraints.package_size is not None)
    ):
        _only_tier = single_vendor_tiers[0]
        single_selected_tier_id = _only_tier.get("tier_id")
        _per_pack = _only_tier.get("base_unit_quantity") or _only_tier.get("quantity")
        try:
            _wanted = float(quantity) if quantity not in (None, "", 0) else None
            _count = _wanted / float(_per_pack) if _wanted and _per_pack else None
        except (TypeError, ValueError, ZeroDivisionError):
            _count = None
        if _count is not None and abs(_count - round(_count)) < 1e-9 and round(_count) > 0:
            _package_hint = f"{chr(10)}💡 _Pour {_wanted:g} {str(payload.get('unit') or '').lower()}, il en faut {round(_count)}._"
        logger.info("flow_compression | step_skipped=tier_selection | tier=%s | count_hint=%s", single_selected_tier_id, bool(_package_hint))
        payload["quantity"] = None
        payload["unit"] = None
        quantity = None

    if (
        isinstance(single_vendor_tiers, list)
        and single_vendor_tiers
        and not single_selected_tier_id
    ):
        # Montre le menu de paliers immédiatement — QUE la quantité soit
        # déjà connue (donnée dans le même message) ou pas encore.
        # Seed `vendor_selection_context` avec `chosen_vendor` déjà résolu
        # (vendeur unique, pas d'ambiguïté) : le tour SUIVANT (réponse au
        # menu de paliers) route alors par le bloc `vendor_ctx_active`
        # ci-dessus, qui possède DÉJÀ toute la mécanique de résolution/
        # persistance de palier (`resolved_tier_id`, récupération de
        # `requested_quantity`) — pas de duplication de cette logique ici.
        return _with_base(
            _build_tier_menu_response(
                state,
                single_vendor_tiers,
                ref.get("name") or product_name,
                ref.get("product_id"),
                ref.get("name") or product_name,
                {
                    "product": product_name,
                    "vendors": [ref],
                    "chosen_vendor": ref,
                    "requested_quantity": quantity,
                    "requested_unit": payload.get("unit"),
                    "available_mapping_kind": "product_vendor",
                },
            )
        )

    # --- QUANTITY (produit sans palier, ou palier déjà résolu) ---
    if quantity in (None, "", 0):
        vendor_ctx_seed = {
            "product": product_name,
            "vendors": [ref],
            "chosen_vendor": ref,
            "requested_quantity": None,
            "requested_unit": None,
            "available_mapping_kind": "product_vendor",
        }
        chosen_tier = None
        if single_selected_tier_id:
            chosen_tier = next(
                (t for t in single_vendor_tiers if t.get("tier_id") == single_selected_tier_id),
                None,
            )
            vendor_ctx_seed["resolved_tier_id"] = single_selected_tier_id
        if chosen_tier:
            tier_label = f"{chosen_tier.get('quantity')} {chosen_tier.get('unit')}" + (
                f" ({chosen_tier['packaging']})" if chosen_tier.get("packaging") else ""
            )
            base_question = (
                f"✅ *{product_name}* — conditionnement *{tier_label}* chez *{vendor_label}*"
                f" ({chosen_tier.get('price')} FCFA).\n\n"
                f"📦 Combien de *{tier_label}* souhaitez-vous ?{_package_hint}"
            )
        else:
            base_question = (
                f"✅ *{product_name}* est disponible chez *{vendor_label}*{price_info}.\n\n"
                f"📦 Quelle quantité souhaitez-vous ?\n"
                f"💡 _Exemples : 50 {unit_hint.lower()}, 2 sacs, 100 kg..._"
                + extras_hint
            )
        # Chantier résilience 2026-08 (volet acheteur) : ce final_response
        # est précalculé et posé directement sur l'état — comme les tunnels
        # producteur, ça court-circuite le "reuse du final_response
        # précalculé" de render_ask_missing_field (ask.py), qui ne peut donc
        # jamais atteindre SA propre logique d'adaptivité. Ce même code
        # chemin est réatteint sur une VRAIE déviation (vendeur déjà résolu,
        # quantité toujours absente parce que l'utilisateur a dit autre
        # chose) — pas seulement à la première résolution du vendeur — d'où
        # le besoin du même accusé de réception LLM, gardé sur UNKNOWN/
        # OUT_OF_SCOPE pour ne pas payer un appel inutile sur l'entrée
        # fraîche.
        note = None
        if event in {"UNKNOWN", "OUT_OF_SCOPE"} and user_text.strip():
            note = await llm_deviation_reply(
                mc_runtime, user_text, f"répondre à : quelle quantité de {product_name} souhaitez-vous ?",
            )
        return _with_base(
            {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": f"{note}\n\n{base_question}" if note else base_question,
                "transaction_payload": payload,
                "vendor_selection_context": vendor_ctx_seed,
                **_tier_resolved_patch(
                    state,
                    single_vendor_tiers,
                    ref.get("name") or product_name,
                    ref.get("product_id"),
                    single_selected_tier_id,
                ),
                "ag_ui_component": None,
            }
        )

    return _with_base(
        await cart_service.add_to_cart_with_ref(
            phone, str(product_name), quantity, ref, cart, state,
            buyer_unit=(
                _fresh_unit_this_turn(state)
                if single_selected_tier_id
                else _declared_unit(payload, state)
            ),
            tier_id=single_selected_tier_id,
        )
    )


__all__ = ["cart_management"]
