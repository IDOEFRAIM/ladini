"""Market — Response handlers (final_response).

Ce module isole la génération de la réponse finale + injection de composants
AG-UI afin d'alléger `shared_core.py`.

Important:
- Conserver le bloc SUCCESS optimisé tel quel (rendu groupé fermes/stocks).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.state import MarketAgentState

logger = logging.getLogger("AgriConnect.Market.ResponseHandlers")


_DEFAULT_LABEL_MAP = {
    "product": "produit",
    "quantity": "quantité",
    "quantity_mentioned": "quantité",
    "price": "prix",
    "price_mentioned": "prix",
    "unit": "unité",
    "unit_mentioned": "unité",
    "zone": "zone de production",
    "zone_name": "zone de production",
}


def _label_for_field(goal: Optional[str], field: Optional[str]) -> str:
    if not field:
        return "cette information"
    clean_field = field.replace("_mentioned", "").replace("_for_sale", "")
    goal_config = (INTENT_CONFIG.get(goal or "") or {})
    label_map = goal_config.get("label_map") or {}
    return label_map.get(field, label_map.get(clean_field, _DEFAULT_LABEL_MAP.get(field, _DEFAULT_LABEL_MAP.get(clean_field, clean_field))))


# PEDAGOGICAL FIELD REASONS — WHY each field matters (business value)
_FIELD_BUSINESS_REASON: Dict[str, str] = {
    "product": "pour cibler les bons acheteurs et bien catégoriser votre offre",
    "quantity_mentioned": "pour que les acheteurs sachent exactement ce qui est disponible",
    "price_mentioned": "pour positionner votre offre de manière compétitive sur le marché",
    "unit_mentioned": "pour éviter toute confusion lors de la livraison",
    "zone_name": "pour connecter avec les acheteurs de votre région",
    "zone": "pour cibler le bon marché local",
    "stock_id": "pour identifier précisément le lot concerné",
    "auction_id": "pour répondre à la bonne demande d'achat",
    "bid_id": "pour valider la bonne transaction",
    "movement_type": "pour que votre inventaire reste exact (Entrée, Sortie ou Perte)",
    "farm_id": "pour rattacher l'opération à la bonne exploitation",
    "deadline": "pour que les producteurs sachent quand répondre",
    "description": "pour donner envie aux acheteurs (qualité, variété, fraîcheur...)",
}


async def _generate_llm_question(
    mc_runtime: Any, goal: str, field: str, label: str,
    payload: Dict[str, Any], state: Optional[Dict[str, Any]] = None,
) -> str:
    """Génère une question pédagogique coaching-style via LLM.

    Principes :
    - Explique POURQUOI l'info est nécessaire (business value)
    - Encourage et rassure
    - Donne un exemple concret si possible
    - Mentionne la progression si c'est la dernière info
    """
    llm = getattr(mc_runtime, "llm", None)
    business_reason = _FIELD_BUSINESS_REASON.get(field, "pour finaliser votre opération")
    fallback = f"J'ai besoin de connaître {label} {business_reason}. Indiquez-le moi."
    if llm is None:
        return fallback

    goal_label = (INTENT_CONFIG.get(goal) or {}).get("label", goal.replace("_", " ").lower())
    already_known = ", ".join(
        f"{k}={v}" for k, v in (payload or {}).items()
        if v not in (None, "", [], {}) and not k.endswith("_id") and k != "phone"
    ) or "aucune donnée encore"

    # Build rich context from state
    progress_ctx = ""
    is_last_field = False
    if state:
        progress = state.get("conversation_progress") or {}
        if progress:
            remaining = len(progress.get("remaining") or [])
            is_last_field = remaining <= 1
            progress_ctx = f"\nProgression : étape {progress.get('filled', 0) + 1}/{progress.get('total', '?')}."
        user_name = state.get("user_name")
        if user_name:
            progress_ctx += f"\nL'utilisateur s'appelle {user_name}."

    last_field_instruction = ""
    if is_last_field:
        last_field_instruction = (
            "\nC'est la DERNIÈRE info nécessaire ! Encourage l'utilisateur en disant qu'on y est presque "
            "et que dès qu'il répond, l'opération sera lancée."
        )

    prompt = (
        f"Tu es un assistant commercial agricole WhatsApp au Burkina Faso.\n"
        f"Ton style : coach amical, direct, encourageant. Tu tutoies l'utilisateur.\n"
        f"L'utilisateur est en train de : {goal_label}.\n"
        f"Données déjà collectées : {already_known}.{progress_ctx}\n\n"
        f"Tu dois demander : '{label}'.\n"
        f"Raison métier : {business_reason}.\n"
        f"IMPORTANT : Explique brièvement POURQUOI tu demandes cette info (1 raison métier).\n"
        f"Donne un EXEMPLE concret si pertinent (ex: '250 FCFA/kg', '5 sacs', 'Ouagadougou').\n"
        f"{last_field_instruction}\n"
        f"Réponds en 1-2 phrases maximum, en français simple et direct."
    )
    try:
        completion = await asyncio.to_thread(
            lambda: llm.chat.completions.create(
                model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=120,
            )
        )
        result = (completion.choices[0].message.content or "").strip()
        return result if result else fallback
    except Exception as exc:
        logger.warning("LLM question generation failed: %s", exc)
        return fallback


_RECOVERY_MAX_RETRIES = 2


def _resolve_goal_for_ui(state: MarketAgentState) -> str | None:
    """Resolve a stable goal string for AG-UI metadata.

    Contract: never returns 'UNKNOWN'. If no goal can be resolved, returns None.
    """
    working = state.get("working_memory") or {}
    candidates = [
        state.get("current_goal"),
        working.get("active_goal"),
        working.get("locked_intent"),
        state.get("suspended_goal"),
    ]
    for cand in candidates:
        if cand in (None, ""):
            continue
        goal = str(cand).upper().strip()
        if goal and goal != "UNKNOWN":
            return goal
    return None


async def final_response(state: MarketAgentState, mc_runtime: Any) -> Dict[str, Any]:
    """Compose le message final via LLM et injecte le composant AG-UI requis."""
    strategy = str(state.get("response_strategy") or "CLARIFICATION").upper().strip()
    status = str(state.get("status") or "").upper().strip()
    goal = _resolve_goal_for_ui(state)
    user_name = state.get("user_name") or ""
    salutation = f"{user_name}, " if user_name else ""
    payload = state.get("transaction_payload") or {}

    # Never reuse precomputed `final_response` here: it can get out of sync with
    # `response_strategy` within the same graph run, causing text/component mismatch.
    # This node is the single source of truth for the final text + AG-UI.

    # -----------------------------------------------------------------
    # STRATÉGIE 1 : ASK_MISSING_FIELD — Question LLM + FormInputComponent
    # -----------------------------------------------------------------
    if strategy == "ASK_MISSING_FIELD":
        if not goal:
            return {
                "final_response": f"{salutation}Que souhaitez-vous faire exactement (vendre, acheter, stock, enchères) ?",
                "ag_ui_component": None,
            }
        missing = state.get("missing_fields") or []
        field = state.get("last_missing_field") or (missing[0] if missing else None)
        label = _label_for_field(goal, field)
        candidates = state.get("expected_candidates") or []

        question = await _generate_llm_question(
            mc_runtime, goal, field or "", label, payload, state=state,
        )
        # Prefix with progress indicator when available
        progress = state.get("conversation_progress") or {}
        if progress:
            total = progress.get("total", 0)
            filled = progress.get("filled", 0)
            if total > 1:
                question = f"[{filled + 1}/{total}] {question}"

        if candidates:
            ag_component = {
                "lc_type": "constructor",
                "id": ["ag_ui", "ListMenu"],
                "kwargs": {
                    "title": f"Choisissez {label}",
                    "options": [
                        {"index": str(i), "label": c}
                        for i, c in enumerate(candidates, start=1)
                    ],
                    "metadata": {"field": field, "goal": goal},
                },
            }
        else:
            ag_component = {
                "lc_type": "constructor",
                "id": ["ag_ui", "FormInputComponent"],
                "kwargs": {
                    "title": f"Saisie : {label}",
                    "field": field,
                    "label": label,
                    "placeholder": f"Entrez {label}...",
                    "metadata": {"goal": goal},
                },
            }

        return {"final_response": question, "ag_ui_component": ag_component}

    # -----------------------------------------------------------------
    # STRATÉGIE 2 : CONFIRMATION — Récapitulatif + FormConfirmation
    # -----------------------------------------------------------------
    if strategy == "CONFIRMATION":
        if not goal:
            return {
                "final_response": f"{salutation}Que souhaitez-vous confirmer exactement ?",
                "ag_ui_component": None,
            }
        summary = state.get("confirmation_summary")

        if not summary and payload:
            prod = payload.get("product") or payload.get("product_name")
            qty = payload.get("quantity_mentioned") or payload.get("quantity")
            unit = payload.get("unit_mentioned") or payload.get("unit", "KG")
            price = payload.get("price_mentioned") or payload.get("price")
            budget = payload.get("max_budget") or payload.get("target_price")
            zone = payload.get("zone_name") or state.get("zone_name")
            parts: List[str] = []

            if "PUBLISH" in goal or "SELL" in goal:
                parts.append(f"Vente de {prod or '—'}")
                if qty:
                    parts.append(f"Quantité : {qty} {unit}")
                if price:
                    parts.append(f"Prix : {price} FCFA")
            elif "AUCTION" in goal or "SEARCH" in goal or "BUY" in goal:
                parts.append(f"Achat de {prod or '—'}")
                if qty:
                    parts.append(f"Quantité : {qty} {unit}")
                if budget or price:
                    parts.append(f"Prix plafond : {budget or price} FCFA")
            elif "BID" in goal:
                parts.append("Soumission d'enchère")
                if price:
                    parts.append(f"Proposition : {price} FCFA")
            else:
                for k, v in payload.items():
                    if v and not k.endswith("_id") and k != "phone":
                        parts.append(f"{k.replace('_', ' ').title()} : {v}")

            if zone:
                parts.append(f"Zone : {zone}")
            summary = "\n".join(parts) if parts else f"Opération : {goal}"

        text_output = f"{salutation}Voici le récapitulatif :\n{summary}\n\nConfirmez-vous ?"

        return {
            "final_response": text_output,
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "FormConfirmation"],
                "kwargs": {
                    "title": "Confirmation requise",
                    "summary": summary,
                    "submit_label": "Confirmer",
                    "cancel_label": "Annuler",
                    "metadata": {"goal": goal},
                },
            },
        }

    # -----------------------------------------------------------------
    # STRATÉGIE 3 : SELECTION_MENU — ListMenu AG-UI
    # -----------------------------------------------------------------
    if strategy == "SELECTION_MENU":
        if not goal:
            return {
                "final_response": f"{salutation}Que souhaitez-vous choisir ?",
                "ag_ui_component": None,
            }
        wm = state.get("working_memory") or {}
        preformatted = wm.get("auction_menu") or wm.get("bids_menu") or wm.get("stocks_menu") or wm.get("generic_menu")
        candidates: List[str] = state.get("expected_candidates") or []

        text_output = (
            str(preformatted) if preformatted else (
                "Veuillez choisir une option :\n"
                + "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
            )
        )

        return {
            "final_response": text_output,
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "ListMenu"],
                "kwargs": {
                    "title": "Sélection",
                    "options": [
                        {"index": str(i), "label": c}
                        for i, c in enumerate(candidates, start=1)
                    ],
                    "metadata": {"goal": goal},
                },
            },
        }

    # -----------------------------------------------------------------
    # STRATÉGIE 4 : SUCCESS / COMPLETED
    # -----------------------------------------------------------------
    if strategy == "SUCCESS" or status == "COMPLETED":
        # Keep UI metadata consistent even when goal is missing; SUCCESS can render without goal.
        exec_result = state.get("execution_result") or {}
        tool_msg = exec_result.get("message") or ""
        tool_data = exec_result.get("data")
        
        # Initialisation par défaut de la réponse
        text_output = str(tool_msg) or "🌾 Opération réussie."
        ag_component = None
        options_ui = []

        # Fonction utilitaire pour formater proprement les nombres (ex: 50.0 -> "50", 12.5 -> "12.5")
        def _fmt_num(val) -> str:
            try:
                f_val = float(val)
                return f"{f_val:g}"
            except (ValueError, TypeError):
                return str(val) if val else "0"

        # 🔍 CAS 1 : Structure en Dictionnaire imbriqué (ex: stocks groupés par exploitation)
        # Guard: only enter this branch if values look like farm objects (dict with stocks/name).
        is_grouped_farm_dict = False
        if isinstance(tool_data, dict) and tool_data:
            sample_vals = [v for v in tool_data.values() if v is not None]
            if sample_vals and isinstance(sample_vals[0], dict):
                probe = sample_vals[0]
                if any(k in probe for k in ("stocks", "farm_name", "name", "location")):
                    is_grouped_farm_dict = True

        if is_grouped_farm_dict:
            lines = ["📋 *Voici l'état actuel de vos stocks par exploitation :*\n"]
            index_counter = 1
            
            for farm_id, farm_info in tool_data.items():
                if not isinstance(farm_info, dict):
                    continue

                farm_name = farm_info.get("farm_name") or farm_info.get("name") or "Exploitation sans nom"
                location = farm_info.get("location") or "Zone non spécifiée"
                stocks = farm_info.get("stocks") or []
                
                # En-tête de la ferme
                lines.append(f"🏡 *{farm_name}* ({location})")
                
                if not stocks:
                    lines.append("  _Aucun produit stocké actuellement dans cette exploitation._\n")
                    continue
                    
                for s in stocks:
                    item_name = s.get("item_name") or s.get("product_name") or "Produit inconnu"
                    qty = _fmt_num(s.get("quantity", 0))
                    unit = str(s.get("unit") or "KG").upper()
                    stock_id = s.get("stock_id") or s.get("id") or farm_id
                    
                    label_item = f"{item_name} : {qty} {unit}"
                    lines.append(f"  {index_counter}️⃣ {label_item}")
                    
                    # Remplissage du composant d'interface avec l'identifiant technique masqué
                    options_ui.append({
                        "index": str(index_counter),
                        "label": f"{farm_name} - {label_item}",
                        "value": str(stock_id)
                    })
                    index_counter += 1
                lines.append("")  # Espacement visuel entre les blocs d'exploitations

            if options_ui:
                lines.append("❓ *Que souhaitez-vous faire ?* Indiquez le numéro d'un lot pour le mettre en vente ou le modifier.")
                text_output = "\n".join(lines).strip()
                
                ag_component = {
                    "lc_type": "constructor",
                    "id": ["ag_ui", "ListMenu"],
                    "kwargs": {
                        "title": "Gestion des stocks",
                        "options": options_ui,
                        "metadata": ({"goal": goal, "mode": "grouped_dict", "count": len(options_ui)} if goal else {"mode": "grouped_dict", "count": len(options_ui)}),
                    },
                }

        # 🔍 CAS 2 : Structure en Liste plate (ex: liste d'offres globales, d'acheteurs)
        elif isinstance(tool_data, list) and tool_data:
            lines = ["📋 *Voici les éléments trouvés correspondant à votre demande :*\n"]
            
            for i, item in enumerate(tool_data, start=1):
                name = item.get("name") or item.get("item_name") or item.get("product_name") or f"Élément {i}"
                details_parts = []
                entity_id = item.get("id")
                
                # Détection contextuelle de l'entité pour enrichir intelligemment la ligne
                if "size" in item:  # C'est une exploitation
                    entity_id = item.get("farm_id") or entity_id
                    loc = item.get("location")
                    details_parts.append(f"{_fmt_num(item['size'])} ha")
                    if loc: details_parts.append(str(loc))
                    
                elif "quantity" in item:  # C'est un lot ou un stock plat
                    entity_id = item.get("stock_id") or entity_id
                    qty = _fmt_num(item["quantity"])
                    unit = str(item.get("unit") or "KG").upper()
                    details_parts.append(f"{qty} {unit}")
                    
                elif "price" in item or "target_price" in item:  # C'est un aspect financier (enchère, prix)
                    entity_id = item.get("auction_id") or item.get("bid_id") or entity_id
                    price = _fmt_num(item.get("price") or item.get("target_price"))
                    details_parts.append(f"{price} FCFA")

                entity_id = str(entity_id or i)
                details_str = f" ({', '.join(details_parts)})" if details_parts else ""
                label_complet = f"{name}{details_str}"
                
                lines.append(f"{i}️⃣ {label_complet}")
                options_ui.append({
                    "index": str(i),
                    "label": label_complet,
                    "value": entity_id
                })

            lines.append("\n👉 *Faites votre choix en tapant le numéro correspondant.*")
            text_output = "\n".join(lines)
            
            ag_component = {
                "lc_type": "constructor",
                "id": ["ag_ui", "ListMenu"],
                "kwargs": {
                    "title": "Faites votre choix",
                    "options": options_ui,
                    "metadata": ({"goal": goal, "mode": "flat_list", "count": len(tool_data)} if goal else {"mode": "flat_list", "count": len(tool_data)}),
                },
            }

        # 🔍 CAS 3 : Fallback transactionnel unitaire (si l'outil ne renvoie pas de collection)
        if not ag_component:
            if not tool_msg:
                prod_name = payload.get("product") or payload.get("product_name") or "votre demande"
                qty = _fmt_num(payload.get("quantity_mentioned") or payload.get("quantity") or "")
                unit = str(payload.get("unit_mentioned") or payload.get("unit") or "").upper()
                q_info = f" pour {qty} {unit}" if qty else ""

                g = goal or ""
                if "PUBLISH" in g or "SELL" in g:
                    text_output = f"✅ {salutation}Votre offre de vente{q_info} de *{prod_name}* a bien été publiée sur le marché."
                elif "AUCTION" in g or "BUY" in g:
                    text_output = f"✅ {salutation}Votre appel d'offres{q_info} de *{prod_name}* a été enregistré avec succès."
                elif "BID" in g:
                    price_bid = _fmt_num(payload.get("price"))
                    p_info = f" à {price_bid} FCFA" if price_bid else ""
                    text_output = f"✅ {salutation}Votre proposition de prix{p_info} pour *{prod_name}* a bien été transmise."
                else:
                    text_output = f"✅ {salutation}L'opération concernant *{prod_name}* a été validée avec succès."

            ag_component = {
                "lc_type": "constructor",
                "id": ["ag_ui", "StatusComponent"],
                "kwargs": {"type": "success", "message": text_output},
            }

        # Envoi d'une note proactive d'engagement utilisateur
        proactive = state.get("proactive_hint")
        if proactive:
            text_output = f"{text_output}\n\n💡 *Conseil :* {proactive}"

        return {
            "final_response": text_output,
            "ag_ui_component": ag_component,
        }

    # -----------------------------------------------------------------
    # STRATÉGIE 5 : ERROR
    # -----------------------------------------------------------------
    if strategy == "ERROR":
        reason_list = state.get("validation_errors") or []
        reason = reason_list[0] if reason_list else state.get("security_reason") or ""

        if state.get("security_status") == "SCAM_DETECTED":
            text_output = "⚠️ Alerte sécurité : ce message ne peut pas être traité."
            ui_reason = "Sécurité renforcée"
        elif reason:
            text_output = f"❌ Opération impossible : {reason}"
            ui_reason = str(reason)
        else:
            text_output = "❌ Une erreur technique est survenue. Réessayez dans un instant."
            ui_reason = "Erreur technique"

        # Coaching on error: suggest what to do next
        goal_label = (INTENT_CONFIG.get(goal or "") or {}).get("label", "")
        if goal_label:
            text_output += f"\n\nVous pouvez réessayer en reformulant ou dire \"annuler\"."

        return {
            "final_response": text_output,
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "StatusComponent"],
                "kwargs": {"type": "error", "reason": ui_reason},
            },
        }

    # -----------------------------------------------------------------
    # STRATÉGIE 5B : RECOVERY (Coaching pédagogique)
    # -----------------------------------------------------------------
    if strategy == "RECOVERY":
        retry_count = int(state.get("retry_count") or 0)
        retry_next = min(retry_count + 1, _RECOVERY_MAX_RETRIES)
        field = state.get("last_missing_field") or ((state.get("missing_fields") or [None])[0])
        label = _label_for_field(goal or "", field)
        goal_label = (INTENT_CONFIG.get(goal or "") or {}).get("label", (goal or "").replace("_", " ").lower()) if goal else "votre opération"
        business_reason = _FIELD_BUSINESS_REASON.get(field or "", "")

        if retry_count >= _RECOVERY_MAX_RETRIES:
            text_output = (
                f"{salutation}Pas de souci ! L'opération \"{goal_label}\" est mise en pause. "
                "Vous pourrez la reprendre à tout moment. Que puis-je faire d'autre pour vous ?"
            )
        else:
            # Pedagogical recovery: explain what we need and why
            reason_part = f" ({business_reason})" if business_reason else ""
            text_output = (
                f"🔄 {salutation}On continue : {goal_label}.\n"
                f"J'ai juste besoin de {label}{reason_part}.\n"
                f"Exemple : tapez simplement la valeur, ou dites « annuler » si vous changez d'avis."
            )
        return {
            "final_response": text_output,
            "retry_count": retry_next,
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "StatusComponent"],
                "kwargs": {"type": "warning", "message": text_output},
            },
        }

    # -----------------------------------------------------------------
    # STRATÉGIE 6 : INTERRUPTION_HANDLER
    # -----------------------------------------------------------------
    if strategy == "INTERRUPTION_HANDLER":
        return {
            "final_response": "Je mets en pause la saisie en cours pour traiter votre nouvelle demande.",
            "ag_ui_component": None,
        }

    # -----------------------------------------------------------------
    # FALLBACK : CLARIFICATION (Coach proactif role-aware)
    # -----------------------------------------------------------------
    user_role = str(state.get("user_role") or "PRODUCER").upper()
    turn = int(state.get("turn_count") or 0)

    if turn <= 1:
        if user_role == "BUYER":
            fallback_text = (
                f"👋 {salutation}Bienvenue ! Je suis votre assistant d'achat AgriConnect.\n"
                "Je peux vous aider à :\n"
                "• Trouver des produits agricoles\n"
                "• Lancer un appel d'offres\n"
                "• Suivre vos commandes\n\n"
                "Dites-moi ce que vous cherchez !"
            )
        else:
            fallback_text = (
                f"👋 {salutation}Bienvenue ! Je suis votre coach commercial AgriConnect.\n"
                "Je peux vous aider à :\n"
                "• Mettre vos produits en vente\n"
                "• Gérer votre stock\n"
                "• Répondre aux demandes d'acheteurs\n\n"
                "Que souhaitez-vous faire ?"
            )
    else:
        # Tour > 1 : reformulation douce
        if user_role == "BUYER":
            examples = "chercher un produit, lancer un appel d'offres, ou voir vos commandes"
        else:
            examples = "vendre un produit, gérer votre stock, ou répondre à une enchère"
        fallback_text = (
            f"{salutation}Je n'ai pas bien saisi. Vous pouvez par exemple {examples}. "
            "Dites-moi en quelques mots ce dont vous avez besoin."
        )

    return {
        "final_response": fallback_text,
        "ag_ui_component": None,
    }


__all__ = [
    "final_response",
    "_label_for_field",
]
