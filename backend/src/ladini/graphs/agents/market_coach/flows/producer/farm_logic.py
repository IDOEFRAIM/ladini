from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from ladini.core.logger import get_logger
from ladini.domain.profile_requirements import display_name_or_none
from ladini.graphs.agents.market_coach.core.base import (
    _AUTO_FARM_NOTICE,
    FARM_CRITICAL_GOALS,
    FARM_ID_REQUIRED_GOALS,
    MARKET_VALIDATION_CONFIG,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import FarmGateway
from ladini.graphs.agents.market_coach.utils import MarketRuntime

logger = get_logger("Ladini.MarketCoach.AutoFarm")

FARM_RULES = MARKET_VALIDATION_CONFIG.farm


def _extract_farm_id(candidates: Iterable[Dict[str, Any]]) -> Optional[str]:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        farm_id = candidate.get("farm_id") or candidate.get("id")
        if farm_id:
            return str(farm_id)
    return None


async def _farm_belongs_to_caller(
    claimed_farm_id: str,
    phone: str,
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> bool:
    """Le ``farm_id`` présent dans le payload appartient-il bien à l'appelant ?

    Garde d'autorisation (audit sécurité agent 2026-09-10) : un `farm_id` du
    payload est une ENTRÉE UTILISATEUR (slot déclaré, donc remplissable par
    extraction LLM ou `form_data`), pas une preuve d'accès.

    Fail-closed sur l'incertitude : si la liste des fermes de l'appelant est
    introuvable (panne réseau/gateway), on renvoie ``False`` — l'identifiant
    revendiqué est écarté et la résolution normale reprend la main. Ne jamais
    accorder le bénéfice du doute à une valeur venue du payload.
    """
    claimed = str(claimed_farm_id or "").strip()
    if not claimed:
        return False

    cached = state.get("user_farms_cache")
    if isinstance(cached, list) and cached:
        farms = cached
    else:
        try:
            farms = await FarmGateway(mc_runtime).list_farms(str(phone))
        except Exception as exc:  # pragma: no cover - log only
            logger.warning(
                "[AutoFarm] Vérification de propriété impossible (%s) — "
                "farm_id revendiqué écarté par précaution.",
                exc,
            )
            return False

    if not isinstance(farms, list):
        return False

    owned = {
        str(f.get("id") or f.get("farm_id") or "")
        for f in farms
        if isinstance(f, dict)
    }
    owned.discard("")
    return claimed in owned


async def ensure_farm_node(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Garantit la présence d'un farm_id avant les appels critiques.

    Asymmetric behavior (Mission 3):
      - WRITE_REQUIRES_FARM: auto-provision allowed (create farm if none exists).
      - READ_OPTIONAL_FARM: NO auto-provisioning — emit explicit selection prompt.
      - Multi-farm: NEVER auto-select farms[0] — always prompt for user choice.
    """

    goal = (state.get("current_goal") or "").upper()
    if goal not in FARM_CRITICAL_GOALS:
        return {}

    payload = dict(state.get("transaction_payload") or {})

    # ⚠️ AUDIT SÉCURITÉ AGENT 2026-09-10 — l'identité du tour ne doit JAMAIS
    # retomber sur le payload. `state["user_phone"]` vient du webhook signé ;
    # `payload["phone"]` vient de l'extraction LLM / `form_data`, donc du texte
    # de l'utilisateur. L'ancien `state.get("user_phone") or payload.get("phone")`
    # faisait du numéro de téléphone une valeur usurpable dès que `user_phone`
    # manquait pour une raison quelconque.
    phone = state.get("user_phone")
    if not phone:
        logger.debug(
            "[AutoFarm] Impossible de déterminer le téléphone utilisateur — abandon."
        )
        return {}

    # ⚠️ AUDIT SÉCURITÉ AGENT 2026-09-10 — ce nœud court-circuitait dès qu'un
    # `farm_id` était PRÉSENT dans le payload, sans jamais vérifier à qui cette
    # ferme appartient. Comme `farm_id` est un slot déclaré (`core/slots.py`),
    # la valeur peut arriver par extraction LLM ou `form_data` — donc depuis le
    # texte utilisateur. Un `farm_id` fourni n'était ni validé ici, ni écrasé,
    # ni contrôlé côté outil : il devenait la cible d'écriture de `add_stock`,
    # `remove_stock`, `add_expense`, `update_farm`.
    #
    # Un `farm_id` du payload n'est désormais accepté que s'il figure PARMI LES
    # FERMES DE L'APPELANT ; sinon il est écarté et la résolution normale
    # (ci-dessous) redonne la vraie ferme. La garde côté base
    # (`BaseMixin._assert_farm_owned_by`) reste la barrière autoritaire — cette
    # vérification-ci évite en plus d'agir sur une valeur empoisonnée et de
    # présenter un message d'erreur à un utilisateur légitime.
    claimed_farm_id = payload.get("farm_id")
    if claimed_farm_id:
        if await _farm_belongs_to_caller(str(claimed_farm_id), phone, state, mc_runtime):
            return {}
        logger.warning(
            "[AutoFarm] FARM_ID_REJETE | farm_id=%s non détenu par l'appelant — "
            "résolution normale forcée.",
            claimed_farm_id,
        )
        payload["farm_id"] = None

    if state.get("farm_creation_attempted"):
        logger.info(
            "[AutoFarm] Tentative déjà effectuée pour cette transaction — aucune action."
        )
        return {}

    is_read_intent = goal in FARM_RULES.read_optional_farm
    is_write_intent = (
        goal in FARM_RULES.write_requires_farm or goal in FARM_ID_REQUIRED_GOALS
    )
    updates: Dict[str, Any] = {"farm_creation_attempted": True}

    # Short-circuit: check in-memory cache before network call
    cached = state.get("user_farms_cache")
    if isinstance(cached, list) and cached:
        farms_cache = cached
        logger.info("[AutoFarm] Using cached farms (%d entries)", len(farms_cache))
    else:
        farms_cache = []
        try:
            farm_gw = FarmGateway(mc_runtime)
            farms_cache = await farm_gw.list_farms(str(phone))
        except Exception as exc:  # pragma: no cover - log only
            logger.warning(
                "[AutoFarm] Impossible de récupérer les fermes existantes: %s", exc
            )

    if farms_cache:
        updates["user_farms_cache"] = farms_cache

    farms_list = farms_cache if isinstance(farms_cache, list) else []
    no_farms = len(farms_list) == 0
    auto_provision_allowed = is_write_intent or no_farms

    # --- MULTI-FARM AMBIGUITY: always prompt user for explicit choice ---
    if len(farms_list) > 1:
        logger.info(
            "[AutoFarm] %d farms detected — prompting user for selection",
            len(farms_list),
        )
        lines = ["🌾 *Sur quelle exploitation souhaitez-vous travailler ?*"]
        mapping: Dict[str, str] = {}
        for i, f in enumerate(farms_list, start=1):
            f_id = str(f.get("id") or f.get("farm_id") or "")
            name = f.get("name") or f"Ferme {i}"
            lines.append(f"{i}. *{name}*")
            mapping[str(i)] = f_id
        updates["status"] = "WAITING_INPUT"
        updates.update(set_pending_interaction(InteractionKind.SELECTION_MENU))
        updates["response_strategy"] = "SELECTION_MENU"
        updates["final_response"] = "\n".join(lines)
        updates["available_mapping"] = mapping
        updates["working_memory"] = {"available_mapping_kind": "farm"}
        return updates

    farm_id = _extract_farm_id(farms_list)

    # --- READ intents: block auto-provisioning ---
    if not farm_id and is_read_intent and not no_farms:
        logger.info(
            "[AutoFarm] No farm for READ intent '%s' — prompting creation", goal
        )
        updates["status"] = "WAITING_INPUT"
        updates["response_strategy"] = "ASK_CLARIFICATION"
        updates["final_response"] = (
            "Vous n'avez pas encore d'exploitation enregistrée. "
            "Pour consulter vos données, créez d'abord une ferme en disant "
            'par exemple : *"Créer une ferme Ferme principale à Dakar"*.'
        )
        return updates

    # --- Auto-provision is allowed for WRITE intents or when no farm exists yet ---
    if not farm_id and not auto_provision_allowed:
        logger.warning(
            "[AutoFarm] Goal '%s' not authorized for auto-provisioning", goal
        )
        updates["status"] = "WAITING_INPUT"
        updates["response_strategy"] = "ASK_CLARIFICATION"
        updates["final_response"] = (
            "Je ne peux pas créer automatiquement une exploitation pour cette opération. "
            "Merci de préciser l'exploitation à utiliser."
        )
        return updates

    if not farm_id:
        zone_name = state.get("zone_name") or payload.get("zone_name")
        zone_id = state.get("zone_id") or payload.get("zone_id")
        default_name = payload.get("farm_name") or (
            f"Ferme de {display_name_or_none(state.get('user_name'))}"
            if display_name_or_none(state.get("user_name"))
            else "Ferme principale"
        )

        try:
            farm_gw = FarmGateway(mc_runtime)
            farm_data = await farm_gw.get_or_create_farm(
                phone=str(phone).strip(),
                farm_name=default_name,
                zone_id=zone_id,
            )
            farm_id = str(farm_data.get("id") or farm_data.get("farm_id") or "")
            if farm_id:
                updates["auto_farm_notice"] = _AUTO_FARM_NOTICE
            if zone_name and not farm_data.get("location"):
                updates.setdefault("working_memory", {})
                wm = dict(updates["working_memory"])
                wm.setdefault("recent_corrections", {})
                updates["working_memory"] = wm
        except Exception as exc:  # pragma: no cover - log only
            logger.error("[AutoFarm] get_or_create_farm a échoué: %s", exc)
            updates["error_creating_farm"] = True
            updates["status"] = "WAITING_INPUT"
            updates["response_strategy"] = "CLARIFICATION"
            updates["final_response"] = (
                "Je n'ai pas pu configurer votre exploitation pour le moment. "
                "Réessayez dans un instant, ou précisez le nom d'une exploitation "
                "existante."
            )
            return updates

    if not farm_id:
        # (P0-2, audit architectural 2026-09-08) : ce cas ne posait jusqu'ici
        # NI `status` NI `final_response` — l'edge fixe vers `confirmation_gate`
        # laissait alors passer un tour sans farm_id résolu, sans jamais
        # informer l'utilisateur. Symétrique avec la branche d'exception
        # ci-dessus : même échec (aucun id obtenu), même traitement.
        updates["error_creating_farm"] = True
        updates["status"] = "WAITING_INPUT"
        updates["response_strategy"] = "CLARIFICATION"
        updates["final_response"] = (
            "Je n'ai pas pu configurer votre exploitation pour le moment. "
            "Réessayez dans un instant, ou précisez le nom d'une exploitation "
            "existante."
        )
        return updates

    payload["farm_id"] = farm_id
    stable_entities = dict(state.get("stable_entities") or {})
    stable_entities["farm_id"] = farm_id

    updates["transaction_payload"] = payload
    updates["stable_entities"] = stable_entities
    return updates


__all__ = ["ensure_farm_node"]
