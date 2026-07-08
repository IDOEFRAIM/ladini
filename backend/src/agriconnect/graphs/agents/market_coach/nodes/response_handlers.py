"""Market — Response handlers (final_response).

Ce module isole la génération de la réponse finale + injection de composants
AG-UI afin d'alléger `shared_core.py`.

Important:
- Conserver le bloc SUCCESS optimisé tel quel (rendu groupé fermes/stocks).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import normalize_slot_keys, slot_has_value


def _append_corrections(state: MarketAgentState, response: Dict[str, Any]) -> Dict[str, Any]:
    status = str(state.get("status") or "").upper().strip()
    strategy = str(state.get("response_strategy") or "").upper().strip()
    if status == "COMPLETED" or strategy == "SUCCESS":
        working = state.get("working_memory") or {}
        if working.get("recent_corrections"):
            working = dict(working)
            working.pop("recent_corrections", None)
            response["working_memory"] = working
        return response
    working = state.get("working_memory") or {}
    corrections_raw = working.get("recent_corrections") or {}

    parts: List[str] = []
    if isinstance(corrections_raw, dict):
        for field, delta in corrections_raw.items():
            label = _label_for_field(state.get("current_goal"), field)
            parts.append(f"*{label.title()}* mis à jour : {delta}")
    else:
        corrections = list(corrections_raw or [])
        for change in corrections:
            if isinstance(change, dict):
                for field, delta in change.items():
                    label = _label_for_field(state.get("current_goal"), field)
                    parts.append(f"*{label.title()}* mis à jour : {delta}")
            else:
                parts.append(str(change))

    if parts:
        acknowledgement = "\n\n" + "\n".join(parts)
        response["final_response"] = f"{response.get('final_response', '')}{acknowledgement}"
        working = dict(working)
        working.pop("recent_corrections", None)
        response["working_memory"] = working
    return response

logger = logging.getLogger("AgriConnect.Market.ResponseHandlers")


_DEFAULT_LABEL_MAP = {
    "product": "produit",
    "quantity": "quantité",
    "price": "prix",
    "unit": "unité",
    "zone": "zone de production",
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
    "quantity": "pour que les acheteurs sachent exactement ce qui est disponible",
    "price": "pour positionner votre offre de manière compétitive sur le marché",
    "unit": "pour éviter toute confusion lors de la livraison",
    "zone": "pour connecter avec les acheteurs de votre région",
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


def _unwrap_execution_result(exec_result: Dict[str, Any]) -> Dict[str, Any]:
    """Déplie défensivement les réponses MCP imbriquées sous `data`.

    Certains clients retournent un payload enveloppé de la forme:
    {"status": "success", "data": {"status": "success", "message": ..., "data": [...]}}
    On normalise ici pour que le renderer SUCCESS lise toujours le bon niveau.
    """
    current = exec_result if isinstance(exec_result, dict) else {}
    for _ in range(3):
        nested = current.get("data")
        if not isinstance(nested, dict):
            break
        has_wrapper_shape = any(k in nested for k in ("status", "data", "message", "error"))
        if not has_wrapper_shape:
            break
        outer_message = current.get("message")
        current = dict(nested)
        if outer_message and not current.get("message"):
            current["message"] = outer_message
    return current


async def final_response(state: MarketAgentState, mc_runtime: Any) -> Dict[str, Any]:
    """Compose le message final via LLM et injecte le composant AG-UI requis."""
    strategy = str(state.get("response_strategy") or "CLARIFICATION").upper().strip()
    status = str(state.get("status") or "").upper().strip()
    goal = _resolve_goal_for_ui(state)
    user_name = state.get("user_name") or ""
    salutation = f"{user_name}, " if user_name else ""
    payload = normalize_slot_keys(state.get("transaction_payload") or {})

    # Never reuse precomputed `final_response` here: it can get out of sync with
    # `response_strategy` within the same graph run, causing text/component mismatch.
    # This node is the single source of truth for the final text + AG-UI.

    # -----------------------------------------------------------------
    # STRATÉGIE 0 : ONBOARDING — prompts déterministes
    # -----------------------------------------------------------------
    if strategy == "ONBOARDING":
        prompt = state.get("onboarding_prompt") or (
            f"{salutation}Bienvenue sur AgriConnect ! Quel est votre nom complet ?"
        )
        return {
            "final_response": prompt,
            "ag_ui_component": state.get("ag_ui_component"),
        }
        return _append_corrections(state, response)

    # -----------------------------------------------------------------
    # STRATÉGIE 1 : ASK_MISSING_FIELD — Question LLM + FormInputComponent
    # -----------------------------------------------------------------
    if strategy == "ASK_MISSING_FIELD":
        # Reuse pre-computed final_response from upstream nodes if available
        precomputed_ask = state.get("final_response")
        if precomputed_ask:
            response = {
                "final_response": precomputed_ask,
                "ag_ui_component": state.get("ag_ui_component"),
            }
            return _append_corrections(state, response)

        if not goal:
            response = {
                "final_response": f"{salutation}Que souhaitez-vous faire exactement (vendre, acheter, stock, enchères) ?",
                "ag_ui_component": None,
            }
            return _append_corrections(state, response)
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

        response = {"final_response": question, "ag_ui_component": ag_component}
        return _append_corrections(state, response)

    # -----------------------------------------------------------------
    # STRATÉGIE 2 : CONFIRMATION — Récapitulatif + FormConfirmation
    # -----------------------------------------------------------------
    if strategy == "CONFIRMATION":
        if not goal:
            response = {
                "final_response": f"{salutation}Que souhaitez-vous confirmer exactement ?",
                "ag_ui_component": None,
            }
            return _append_corrections(state, response)
        summary = state.get("confirmation_summary")

        if not summary and payload:
            prod = payload.get("product")
            qty = payload.get("quantity")
            unit = payload.get("unit", "KG")
            price = payload.get("price")
            budget = payload.get("max_budget") or payload.get("target_price")
            zone = payload.get("zone") or state.get("zone")
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

        response = {
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
        return _append_corrections(state, response)

    # -----------------------------------------------------------------
    # STRATÉGIE 3 : SELECTION_MENU — ListMenu AG-UI
    # -----------------------------------------------------------------
    if strategy == "SELECTION_MENU":
        # Reuse pre-computed final_response from upstream nodes (cart, negotiation, etc.)
        if state.get("final_response"):
            response = {
                "final_response": state.get("final_response"),
                "ag_ui_component": state.get("ag_ui_component"),
            }
            return _append_corrections(state, response)
        if not goal:
            response = {
                "final_response": f"{salutation}Que souhaitez-vous choisir ?",
                "ag_ui_component": None,
            }
            return _append_corrections(state, response)
        wm = state.get("working_memory") or {}
        preformatted = wm.get("auction_menu") or wm.get("bids_menu") or wm.get("stocks_menu") or wm.get("generic_menu")
        candidates: List[str] = state.get("expected_candidates") or []

        if preformatted:
            text_output = str(preformatted)
        else:
            base_menu = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
            text_output = "Veuillez choisir une option :\n" + base_menu
            if "répondez" not in text_output.lower():
                text_output = (
                    text_output
                    + "\n\n👉 Répondez uniquement par le numéro de votre choix (ex: '2')."
                )

        response = {
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
        return _append_corrections(state, response)

    # -----------------------------------------------------------------
    # STRATÉGIE 4 : SUCCESS / COMPLETED
    # -----------------------------------------------------------------
    if strategy == "SUCCESS" or (
        status == "COMPLETED"
        and strategy not in {
            "SELECTION_MENU",
            "ASK_MISSING_FIELD",
            "CONFIRMATION",
            "CLARIFICATION",
            "ERROR",
            "ONBOARDING",
        }
    ):
        # Toujours recalculer la réponse finale pour refléter les dernières données MCP.
        # (Les noeuds amont ne doivent plus injecter un texte qui masquerait les résultats frais.)

        # Keep UI metadata consistent even when goal is missing; SUCCESS can render without goal.
        raw_exec_result = state.get("execution_result") or {}
        exec_result = _unwrap_execution_result(raw_exec_result)
        tool_msg = exec_result.get("message") or ""
        tool_data = exec_result.get("data")
        if tool_data in (None, {}):
            results_payload = exec_result.get("results")
            if isinstance(results_payload, (list, dict)):
                tool_data = results_payload
        
        # Initialisation par défaut de la réponse
        text_output = str(tool_msg) or "🌾 Opération réussie."
        ag_component = None
        options_ui = []
        formatted_menu_raw = exec_result.get("formatted_menu")
        formatted_menu_text = (
            formatted_menu_raw.strip()
            if isinstance(formatted_menu_raw, str)
            else ""
        )
        has_formatted_menu = bool(formatted_menu_text)
        if has_formatted_menu:
            text_output = formatted_menu_text

        # Fonction utilitaire pour formater proprement les nombres (ex: 50.0 -> "50", 12.5 -> "12.5")
        def _fmt_num(val) -> str:
            try:
                f_val = float(val)
                return f"{f_val:g}"
            except (ValueError, TypeError):
                return str(val) if val else "0"

        def _fmt_date(date_val) -> Optional[str]:
            if not date_val:
                return None
            if isinstance(date_val, datetime):
                return date_val.strftime("%d/%m/%Y")
            text = str(date_val)
            try:
                cleaned = text.replace("Z", "+00:00") if text.endswith("Z") else text
                dt_val = datetime.fromisoformat(cleaned)
                return dt_val.strftime("%d/%m/%Y")
            except Exception:
                return text

        def _build_list_menu_component(title: str, options: List[Dict[str, str]], mode: str, count: int) -> Dict[str, Any]:
            metadata = {"mode": mode, "count": count}
            if goal:
                metadata["goal"] = goal
            return {
                "lc_type": "constructor",
                "id": ["ag_ui", "ListMenu"],
                "kwargs": {
                    "title": title,
                    "options": options,
                    "metadata": metadata,
                },
            }

        def _render_farm_sections(farms_dict: Dict[str, Any]) -> tuple[str, List[Dict[str, str]]]:
            if not farms_dict:
                return "", []
            lines = ["📋 *Voici l'état de vos stocks par exploitation :*\n"]
            options: List[Dict[str, str]] = []
            index_counter = 1
            for farm_id, farm_info in farms_dict.items():
                if not isinstance(farm_info, dict):
                    continue
                farm_name = farm_info.get("farm_name") or farm_info.get("name") or "Exploitation sans nom"
                location = farm_info.get("location") or "Zone non spécifiée"
                lines.append(f"🏡 *{farm_name}* ({location})")
                stocks = farm_info.get("stocks") or []
                if not stocks:
                    lines.append("  _Aucun produit stocké actuellement dans cette exploitation._")
                for stock in stocks:
                    item_name = stock.get("item_name") or stock.get("product_name") or "Produit"
                    qty = _fmt_num(stock.get("quantity", 0))
                    unit = str(stock.get("unit") or "KG").upper()
                    stock_id = stock.get("stock_id") or stock.get("id") or farm_id
                    label_item = f"{item_name} : {qty} {unit}"
                    lines.append(f"  {index_counter}️⃣ {label_item}")
                    options.append({
                        "index": str(index_counter),
                        "label": f"{farm_name} - {label_item}",
                        "value": str(stock_id),
                    })
                    index_counter += 1

                cycles = farm_info.get("upcoming_cycles") or []
                for cycle in cycles[:2]:
                    crop_type = cycle.get("crop_type") or "Culture"
                    harvest = _fmt_date(cycle.get("expected_harvest_date"))
                    status = (cycle.get("status") or "EN PRÉPARATION").upper()
                    harvest_text = f" récolte prévue le {harvest}" if harvest else ""
                    lines.append(f"    🌱 {crop_type} — statut {status}{harvest_text}")
                if len(cycles) > 2:
                    lines.append(f"    … +{len(cycles) - 2} autre(s) cycle(s) en préparation")

                lines.append("")

            if options:
                lines.append("❓ *Que souhaitez-vous faire ?* Indiquez le numéro d'un lot pour le mettre en vente ou le modifier.")
            return "\n".join(lines).strip(), options

        def _render_catalog_section(catalog: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, str]]]:
            if not catalog:
                return "", []
            lines = ["📦 *Produits listés dans votre catalogue :*\n"]
            options: List[Dict[str, str]] = []
            for i, product in enumerate(catalog, start=1):
                name = product.get("name") or product.get("product_name") or f"Produit {i}"
                code = product.get("short_code")
                price = product.get("price")
                unit = str(product.get("unit") or "KG").upper()
                qty = product.get("quantity") or product.get("quantity_for_sale")
                status = (product.get("status") or "DISPONIBLE").upper()
                line_header = f"{i}️⃣ *{name}*"
                if code:
                    line_header += f" (Réf: #{code})"
                lines.append(line_header)
                details = []
                if price is not None:
                    details.append(f"💰 { _fmt_num(price) } FCFA/{unit}")
                if qty not in (None, ""):
                    details.append(f"⚖️ { _fmt_num(qty) } {unit} dispo")
                details.append(f"Statut : {status}")
                lines.append("  " + " | ".join(details))
                options.append({
                    "index": str(i),
                    "label": f"{name} ({_fmt_num(qty or 0)} {unit})",
                    "value": str(product.get("product_id") or product.get("id") or product.get("short_code") or i),
                })
            lines.append("👉 *Mentionnez un numéro pour ouvrir ce produit ou ajuster prix/quantité.*")
            return "\n".join(lines).strip(), options

        def _render_cycles_section(cycles: List[Dict[str, Any]]) -> str:
            if not cycles:
                return ""
            deduped: Dict[str, Dict[str, Any]] = {}
            for raw_cycle in cycles:
                if not isinstance(raw_cycle, dict):
                    continue
                cycle_id = str(raw_cycle.get("cycle_id") or raw_cycle.get("id") or len(deduped))
                if cycle_id in deduped:
                    continue
                deduped[cycle_id] = raw_cycle
            if not deduped:
                return ""
            lines = ["🌱 *Cultures en cours / futures récoltes :*"]
            for cycle in deduped.values():
                crop_type = cycle.get("crop_type") or "Culture"
                farm_name = cycle.get("farm_name") or "ferme"
                status = (cycle.get("status") or "EN PRÉPARATION").upper()
                harvest = _fmt_date(cycle.get("expected_harvest_date"))
                harvest_text = f" — récolte prévue le {harvest}" if harvest else ""
                lines.append(f"• {crop_type} ({farm_name}) — statut {status}{harvest_text}")
            return "\n".join(lines)

        def _render_flat_list(items: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, str]]]:
            lines = ["📋 *Voici les éléments trouvés correspondant à votre demande :*\n"]
            options_ui: List[Dict[str, str]] = []

            for i, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    item = {}
                name = (
                    item.get("name")
                    or item.get("item_name")
                    or item.get("product_name")
                    or f"Élément {i}"
                )
                details_parts: List[str] = []
                entity_id = item.get("id")

                if "size" in item:
                    entity_id = item.get("farm_id") or entity_id
                    loc = item.get("location")
                    details_parts.append(f"{_fmt_num(item['size'])} ha")
                    if loc:
                        details_parts.append(str(loc))
                    stocks = item.get("stocks") or []
                    if isinstance(stocks, list) and stocks:
                        stock_tokens: List[str] = []
                        for s in stocks[:3]:
                            s_name = s.get("item_name") or s.get("product_name") or "Produit"
                            s_qty = _fmt_num(s.get("quantity") or 0)
                            s_unit = str(s.get("unit") or "KG").upper()
                            stock_tokens.append(f"{s_name} {s_qty} {s_unit}")
                        if len(stocks) > 3:
                            stock_tokens.append(f"+{len(stocks) - 3} autre(s)")
                        details_parts.append("Stocks: " + ", ".join(stock_tokens))
                elif "quantity" in item:
                    entity_id = item.get("stock_id") or item.get("product_id") or entity_id
                    qty = _fmt_num(item.get("quantity"))
                    unit = str(item.get("unit") or "KG").upper()
                    details_parts.append(f"{qty} {unit}")
                    if item.get("price") is not None:
                        details_parts.append(f"{_fmt_num(item.get('price'))} FCFA")
                elif "price" in item or "target_price" in item:
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
                    "value": entity_id,
                })

            lines.append("\n👉 *Faites votre choix en tapant le numéro correspondant.*")
            return "\n".join(lines), options_ui

        def _render_buyer_catalog_sections(items: List[Dict[str, Any]]) -> str:
            if not items:
                return ""

            direct_items: List[Dict[str, Any]] = []
            future_items: List[Dict[str, Any]] = []

            for item in items:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source_type") or item.get("availability_kind") or "").upper()
                if not source and item.get("estimated_available_at"):
                    source = "FUTURE"
                bucket = future_items if source == "FUTURE" else direct_items
                bucket.append(item)

            sections: List[str] = []

            if direct_items:
                lines = ["🌐 *Produits disponibles immédiatement :*"]
                for idx, product in enumerate(direct_items, start=1):
                    name = product.get("name") or product.get("product_name") or f"Produit {idx}"
                    price = product.get("price")
                    unit = str(product.get("unit") or "KG").upper()
                    vendor = product.get("vendor") or product.get("producer_name") or "Producteur"
                    zone = product.get("zone_name") or product.get("zone")
                    price_label = f"{_fmt_num(price)} FCFA/{unit}" if price not in (None, "") else "Prix communiqué par le producteur"
                    line = f"{idx}️⃣ *{name}* — {price_label} — {vendor}"
                    if zone:
                        line += f" ({zone})"
                    available_qty = product.get("available_quantity")
                    if available_qty not in (None, ""):
                        line += f" | ⚖️ {_fmt_num(available_qty)} {unit}"
                    lines.append(line)
                sections.append("\n".join(lines))

            if future_items:
                lines = ["⏳ *Productions futures (précommandes ouvertes) :*"]
                for product in future_items:
                    name = product.get("name") or product.get("product_name") or "Production future"
                    price = product.get("price") or product.get("price_per_unit")
                    unit = str(product.get("unit") or "KG").upper()
                    eta = _fmt_date(product.get("estimated_available_at")) or product.get("estimated_available_at") or "date à confirmer"
                    vendor = product.get("vendor") or product.get("producer_name") or "Producteur"
                    price_label = f"{_fmt_num(price)} FCFA/{unit}" if price not in (None, "") else "Prix communiqué lors de la confirmation"
                    lines.append(f"• *{name}* — {price_label} — livré vers {eta} ({vendor})")
                sections.append("\n".join(lines))

            return "\n\n".join([section for section in sections if section]).strip()

        farms_payload: Optional[Dict[str, Any]] = None
        catalog_payload: Optional[List[Dict[str, Any]]] = None
        cycles_payload: Optional[List[Dict[str, Any]]] = None

        if isinstance(tool_data, dict):
            farms_candidate = tool_data.get("farms")
            if isinstance(farms_candidate, dict):
                farms_payload = farms_candidate
            catalog_candidate = tool_data.get("catalog")
            if isinstance(catalog_candidate, list):
                catalog_payload = catalog_candidate
            cycles_candidate = tool_data.get("upcoming_cycles")
            if isinstance(cycles_candidate, list):
                cycles_payload = cycles_candidate

        if farms_payload is None and isinstance(tool_data, dict) and tool_data:
            sample_vals = [v for v in tool_data.values() if v is not None]
            if sample_vals and isinstance(sample_vals[0], dict):
                probe = sample_vals[0]
                if any(k in probe for k in ("stocks", "farm_name", "name", "location")):
                    farms_payload = tool_data  # backward compat

        structured_sections_handled = False
        allow_structured_render = not has_formatted_menu

        if allow_structured_render and any([farms_payload, catalog_payload, cycles_payload]):
            sections: List[str] = []
            if farms_payload:
                farm_text, farm_options = _render_farm_sections(farms_payload)
                if farm_text:
                    sections.append(farm_text)
                if farm_options and ag_component is None:
                    ag_component = _build_list_menu_component(
                        "Gestion des stocks",
                        farm_options,
                        mode="stocks",
                        count=len(farm_options),
                    )

            if catalog_payload:
                catalog_text, catalog_options = _render_catalog_section(catalog_payload)
                if catalog_text:
                    sections.append(catalog_text)
                if catalog_options and ag_component is None:
                    ag_component = _build_list_menu_component(
                        "Catalogue produits",
                        catalog_options,
                        mode="catalog",
                        count=len(catalog_options),
                    )

            if cycles_payload:
                cycles_text = _render_cycles_section(cycles_payload)
                if cycles_text:
                    sections.append(cycles_text)

            if sections:
                text_output = "\n\n".join([section for section in sections if section]).strip()
                structured_sections_handled = True

        selected_tool_name = str(state.get("selected_tool") or "").lower().strip()
        if (
            allow_structured_render
            and not structured_sections_handled
            and selected_tool_name == "search_products"
        ):
            results_payload: List[Dict[str, Any]] = []
            if isinstance(tool_data, list):
                results_payload = tool_data
            elif isinstance(exec_result.get("results"), list):
                results_payload = exec_result["results"]  # type: ignore[index]

            buyer_sections = _render_buyer_catalog_sections(results_payload)
            if buyer_sections:
                text_output = buyer_sections
                structured_sections_handled = True

        # 🔍 CAS 2 : Structure en Liste plate (ex: liste d'offres globales, d'acheteurs)
        if isinstance(tool_data, list) and tool_data:
            flat_text, options_ui = _render_flat_list(tool_data)
            if allow_structured_render and not structured_sections_handled:
                text_output = flat_text

            if options_ui:
                # Reuse existing AG-UI component or provide one if absent
                if ag_component is None:
                    ag_component = _build_list_menu_component(
                        "Faites votre choix",
                        options_ui,
                        mode="flat_list",
                        count=len(options_ui),
                    )
                else:
                    # Enrich existing component metadata when possible
                    kwargs = ag_component.get("kwargs") if isinstance(ag_component, dict) else None
                    if isinstance(kwargs, dict) and "metadata" in kwargs:
                        kwargs["metadata"].setdefault("count", len(options_ui))

        # 🔍 CAS 2B : Dictionnaire simple {farm_name, stocks}
        elif not structured_sections_handled and isinstance(tool_data, dict) and "stocks" in tool_data:
            stocks_list = tool_data.get("stocks") or []
            farm_label = tool_data.get("farm_name") or tool_data.get("name") or "cette exploitation"

            if not stocks_list:
                text_output = (
                    f"Aucun produit n'est actuellement enregistré en stock pour {farm_label}. "
                    "Souhaitez-vous ajouter une nouvelle récolte ?"
                )
                ag_component = {
                    "lc_type": "constructor",
                    "id": ["ag_ui", "StatusComponent"],
                    "kwargs": {"type": "info", "message": text_output},
                }

        # 🔍 CAS 3 : Fallback transactionnel unitaire (si l'outil ne renvoie pas de collection)
        if not ag_component:
            if not tool_msg:
                prod_name = payload.get("product") or payload.get("product_name") or "votre demande"
                qty = _fmt_num(
                    payload.get("quantity")
                    or payload.get("quantity_mentioned")
                    or payload.get("quantity_desired")
                    or ""
                )
                unit = str(
                    payload.get("unit")
                    or payload.get("unit_mentioned")
                    or payload.get("unit_desired")
                    or ""
                ).upper()
                q_info = f" pour {qty} {unit}" if qty else ""

                g = (goal or "").upper()
                if "BUYER_ADD_TO_CART" == g:
                    text_output = (
                        f"🛒 {salutation}*{prod_name}*{q_info} a été ajouté à votre panier. "
                        "Tapez *précommander* pour valider ou ajoutez un autre produit."
                    )
                elif g.startswith("BUYER_PREORDER"):
                    text_output = (
                        f"✅ {salutation}Votre précommande{q_info} pour *{prod_name}* est enregistrée. "
                        "Vous recevrez le récapitulatif complet dans un instant."
                    )
                elif g.startswith("PROCUREMENT_") or "AUCTION" in g:
                    text_output = f"✅ {salutation}Votre appel d'offres{q_info} de *{prod_name}* a été enregistré avec succès."
                elif "PUBLISH" in g or "SELL" in g:
                    text_output = f"✅ {salutation}Votre offre de vente{q_info} de *{prod_name}* a bien été publiée sur le marché."
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

        auto_notice = state.get("auto_farm_notice")
        if auto_notice:
            text_output = f"{auto_notice}\n\n{text_output}"

        if state.get("error_creating_farm"):
            text_output = (
                "⚠️ J'ai rencontré un problème en essayant de configurer automatiquement votre ferme."
                " Vous pourrez tout de même poursuivre, mais la mise à jour des stocks peut échouer tant que la ferme n'est pas ajoutée manuellement.\n\n"
                f"{text_output}"
            )

        # Envoi d'une note proactive d'engagement utilisateur
        proactive = state.get("proactive_hint")
        if proactive:
            text_output = f"{text_output}\n\n💡 *Conseil :* {proactive}"

        response = {
            "final_response": text_output,
            "ag_ui_component": ag_component,
        }
        return _append_corrections(state, response)

    # -----------------------------------------------------------------
    # STRATÉGIE 5 : ERROR
    # -----------------------------------------------------------------
    if strategy == "ERROR":
        reason_list = state.get("validation_errors") or []
        reason = reason_list[0] if reason_list else state.get("security_reason") or ""

        if state.get("security_status") == "SCAM_DETECTED":
            text_output = "⚠️ Alerte sécurité : ce message ne peut pas être traité."
            ui_reason = "Sécurité renforcée"
        elif state.get("security_status") == "WARNING":
            text_output = (
                "⚠️ Je ne peux pas finaliser cette action sans unité claire (KG, SAC, TONNE). "
                "Merci de préciser pour continuer."
            )
            ui_reason = "Unité manquante"
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
        if not field:
            form_step = state.get("form_step")
            if form_step not in (None, "", "CONFIRMING", "COMPLETE"):
                field = form_step
        if not field:
            expected_input = str(state.get("expected_input") or "").strip().lower()
            if expected_input and expected_input not in {"none", "confirmation", "selection"}:
                field = expected_input
        label = _label_for_field(goal or "", field)
        goal_label = (INTENT_CONFIG.get(goal or "") or {}).get("label", (goal or "").replace("_", " ").lower()) if goal else "votre opération"
        clean_field = ""
        if field:
            clean_field = str(field).replace("_mentioned", "").replace("_for_sale", "")
        business_reason = _FIELD_BUSINESS_REASON.get(field or "", "") or _FIELD_BUSINESS_REASON.get(clean_field, "")

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
        detected_intent = str(state.get("detected_intent") or "").upper().strip()
        if detected_intent == "BUYER_PREORDER_INIT":
            return {
                "final_response": "Je bascule vers la confirmation de votre précommande. Le récapitulatif arrive.",
                "ag_ui_component": None,
            }
        suspended = str(state.get("suspended_goal") or "").upper().strip()
        current_goal = str(state.get("current_goal") or "").upper().strip()
        expected = str(state.get("expected_input") or "").upper().strip()

        suspended_label = (
            (INTENT_CONFIG.get(suspended) or {}).get("label")
            if suspended
            else ""
        )
        current_label = (
            (INTENT_CONFIG.get(current_goal) or {}).get("label")
            if current_goal
            else ""
        )

        expected_hint = ""
        if expected == "SELECTION":
            expected_hint = "un numéro du menu (ex: 1)"
        elif expected == "CONFIRMATION":
            expected_hint = "oui / non"
        elif expected and expected not in {"NONE", ""}:
            expected_hint = expected.lower()

        head = "Je traite votre nouvelle demande." 
        if current_label:
            head = f"Je passe à : *{current_label}*."

        tail = ""
        if suspended_label and expected_hint:
            tail = (
                f"\n\nPour reprendre ensuite *{suspended_label}*, j'attendais {expected_hint}. "
                "Vous pouvez aussi dire *annuler* si vous ne souhaitez plus continuer."
            )
        elif suspended_label:
            tail = (
                f"\n\nL'étape *{suspended_label}* est mise de côté. "
                "Dites *reprendre* pour revenir dessus, ou *annuler*."
            )

        return {"final_response": head + tail, "ag_ui_component": None}

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
