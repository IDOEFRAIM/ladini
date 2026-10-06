from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.services.onboarding import (
    resolve_onboarding_state,
)
from ladini.graphs.agents.market_coach.services.profile_loader import (
    _mask_phone,
    load_user_profile,
)
from ladini.graphs.agents.market_coach.utils import MarketRuntime
from ladini.graphs.roles import normalize_role

logger = get_node_logger("SessionBootstrap")

# =====================================================================
# Nœud technique d'amorçage de session — PAS un nœud "philosophie" du
# périmètre audité (2026-09-08, refonte responsabilités des nœuds
# d'entrée). Question UNIQUE : le contexte utilisateur minimum nécessaire
# pour continuer la conversation est-il disponible ? Regroupe DEUX
# responsabilités qui n'avaient plus leur place dans `input_normalizer`
# une fois celui-ci purifié à la SEULE question « quelle est l'entrée
# canonique de ce tour ? » :
#
#   1. Rôle par défaut (ex-`role_guard`) — SANS présumer PRODUCER pour une
#      valeur absente/inconnue (voir `graphs/roles.py::normalize_role`,
#      corrigé dans le même chantier : retourne "UNKNOWN", jamais un
#      défaut silencieux).
#   2. Chargement du profil utilisateur / bascule onboarding — nécessite
#      un accès MCP/DB, donc reste un appel réseau qui n'a pas sa place
#      dans un normaliseur de texte déterministe.
#
# (2026-09-08, revue de validation du bloc refondu) : DEUX écarts
# corrigés suite à cette revue, tous deux confirmés SANS risque de
# régression avant correctif —
#   - Le préchargement des fermes (`preload_farms`) a été RETIRÉ : ce
#     n'est PAS un contexte "minimum universel" (un acheteur n'en a
#     jamais besoin) — c'est une donnée métier producteur. Son unique
#     consommateur, `flows/producer/farm_logic.py::ensure_farm_node`, a
#     DÉJÀ son propre repli complet vers `FarmGateway.list_farms()`
#     quand `user_farms_cache` est absent (vérifié en lisant son code),
#     et persiste le résultat dans le même champ DURABLE — donc aucune
#     perte de fonctionnalité, juste un appel réseau désormais paresseux
#     et scindé au bon endroit (producteur uniquement, à la demande).
#   - Le bookkeeping `working_memory.active_tunnel_label` a été RETIRÉ :
#     recherche exhaustive dans `src/ladini` — AUCUN lecteur nulle
#     part. C'était une écriture pure, jamais consommée (même classe de
#     bug que `working_memory.turn_count`, déjà purgé lors du chantier
#     précédent) — pas seulement mal placée, mais morte.
#
# Pourquoi un nœud séparé plutôt qu'une intégration dans `input_normalizer`
# ou dans `core/graph_builder.py`/`orchestrator.py` : `orchestrator.py`
# (couche appelante, hors périmètre audité) précharge DÉJÀ le profil pour
# une partie des tours (voir `orchestrator.py::_run_turn`, commentaire
# "Forward profile result so input_normalizer skips the redundant MCP
# call") — mais UNIQUEMENT quand le rôle résolu initialement est PRODUCER ;
# pour un utilisateur BUYER (ou tout appel direct au graphe compilé qui ne
# passe pas par cet orchestrateur, ex. tests), ce chargement est la SEULE
# source réelle du profil. Le déplacer entièrement dans `orchestrator.py`
# exigerait de restructurer une couche non auditée dans ce chantier
# (interdit par le mandat, §19) ; l'encapsuler comme un nœud technique
# minimal, nommé pour ce qu'il FAIT et non pour une "philosophie" de
# dialogue, est le choix le plus proche du mandat §12/§14 qui reste
# honnête sur le fait qu'aucun autre point d'ancrage sûr n'existe
# aujourd'hui dans le périmètre inspecté. Dette explicitement assumée —
# voir le rapport de refonte, section "Dette volontairement laissée".
# =====================================================================


def _profile_unavailable_patch() -> Dict[str, Any]:
    """Réponse claire quand le profil ne peut pas être résolu (échec technique).

    On NE simule jamais un utilisateur fantôme et on NE propose pas de
    transaction : on informe l'utilisateur au lieu de le laisser perdu.
    """
    return {
        "status": "BLOCKED",
        "security_status": "PROFILE_UNAVAILABLE",
        "response_strategy": "ERROR",
        "final_response": (
            "😕 Je n'arrive pas à accéder à votre profil pour le moment.\n\n"
            "Merci de *réessayer dans quelques instants*. "
            "Si le problème persiste, contactez le *service client* au +22601479800."
        ),
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "StatusComponent"],
            "kwargs": {"type": "error", "reason": "Profil indisponible"},
        },
    }


async def session_bootstrap(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Amorce technique de session — voir docstring de module."""
    updates: Dict[str, Any] = {}

    # ── 1. Rôle par défaut (ex-role_guard) ─────────────────────────────
    # Valeur d'affichage/préférence initiale UNIQUEMENT — jamais un
    # contrôle d'accès (voir core/router.py::_goal_domain, qui résout le
    # domaine PAR GOAL). "UNKNOWN" plutôt qu'un défaut PRODUCER silencieux
    # — un profil qui n'a jamais précisé son rôle ne doit pas être présumé
    # producteur (mandat §3).
    if not (state.get("user_role") or "").strip():
        updates["user_role"] = normalize_role(None)

    # ── 2. Profil / bascule onboarding ─────────────────────────────────
    phone = state.get("phone") or state.get("user_phone") or state.get("phone_number")
    if not phone and isinstance(state.get("state_updates"), dict):
        phone = state.get("state_updates").get("phone")
    logger.info(
        "[SessionBootstrap] Téléphone extrait pour validation MCP : %s",
        _mask_phone(phone),
    )

    if state.get("user_context_loaded") and phone:
        # Collecte juste-à-temps en cours (`profile_gate`) : le profil EST chargé, mais on reste en « mode gate » pour
        # recueillir la réponse — jamais remis à False ici.
        updates.setdefault("is_onboarding", bool(state.get("profile_gate")))
        updates.setdefault("onboarding_step", "COMPLETED")
        updates.setdefault("onboarding_internal_step", "__NONE__")
        logger.info("[SessionBootstrap] Profil déjà chargé — onboarding désactivé")

    elif state.get("is_onboarding") and phone:
        # Orchestrator already detected a new user — skip redundant MCP call.
        tx_payload = dict(state.get("transaction_payload") or {})
        # COMPATIBILITY_SHIM (2026-09-08, clôture Bloc 1, mandat §8) :
        # recherche exhaustive faite — `flows/producer/farm_logic.py::
        # ensure_farm_node` et `nodes/executor.py` (`mcp_tool_executor`,
        # `_build_task_payload`) lisent `transaction_payload.get("phone")`
        # en REPLI, après `state.get("user_phone")` (jamais en primaire).
        # Ces deux nœuds sont hors périmètre audité (Bloc transactionnel) —
        # on ne peut pas prouver que `user_phone` suffit seul à CES sites,
        # sur TOUS les tours ultérieurs (transaction_payload SURVIT aux
        # tours, contrairement à `user_phone`, réécrit à chaque tour).
        # Retirer cette duplication casserait leur filet sans pouvoir le
        # vérifier depuis ce chantier — conservé tel quel, verrouillé par
        # `tests/nodes/test_session_bootstrap.py::TestTransactionPayloadPhoneShim`.
        tx_payload["phone"] = str(phone).strip()
        current_step = (
            state.get("onboarding_internal_step")
            or state.get("onboarding_step")
            or "COLLECT_ROLE"
        )
        updates.update(
            {
                "user_context_loaded": False,
                "is_onboarding": True,
                "onboarding_step": current_step,
                "onboarding_internal_step": current_step,
                "transaction_payload": tx_payload,
                "user_phone": str(phone).strip(),
            }
        )
        logger.info(
            "[SessionBootstrap] Onboarding déjà activé par l'orchestrateur — skip MCP"
        )

    elif not state.get("user_context_loaded") and phone:
        try:
            profile_updates = await load_user_profile(str(phone), mc_runtime)

            if profile_updates.get("user_context_loaded"):
                updates.update(profile_updates)
                updates["user_role"] = (
                    profile_updates.get("user_role")
                    or state.get("user_role")
                    or updates.get("user_role")
                    or normalize_role(None)
                )

            elif profile_updates.get("_new_user"):
                tx_payload = dict(state.get("transaction_payload") or {})
                if phone:
                    # COMPATIBILITY_SHIM — voir le commentaire complet plus
                    # haut dans cette fonction (branche `is_onboarding`) :
                    # même filet, mêmes lecteurs (`farm_logic.py::
                    # ensure_farm_node`, `nodes/executor.py`).
                    tx_payload["phone"] = str(phone).strip()

                current_step = (
                    state.get("onboarding_internal_step")
                    or state.get("onboarding_step")
                    or "COLLECT_ROLE"
                )
                updates.update(
                    {
                        "user_context_loaded": False,
                        "is_onboarding": True,
                        "onboarding_step": current_step,
                        "onboarding_internal_step": current_step,
                        "transaction_payload": tx_payload,
                        "user_phone": str(phone).strip()
                        if phone
                        else state.get("user_phone"),
                    }
                )
            else:
                # Profil non résolu (échec technique) : message clair, pas de fantôme.
                updates["user_context_loaded"] = False
                if profile_updates.get("_profile_unavailable"):
                    updates["user_phone"] = str(phone).strip()
                    updates.update(_profile_unavailable_patch())
                    return updates

        except Exception as db_err:
            logger.error(
                "[SessionBootstrap] Erreur de communication critique avec le serveur MCP DB: %s",
                db_err,
                exc_info=True,
            )
            updates["user_context_loaded"] = False
            updates["user_phone"] = str(phone).strip()
            updates.update(_profile_unavailable_patch())
            return updates

    # Onboarding resolution
    resolve_onboarding_state(state, updates)

    return updates


__all__ = ["session_bootstrap"]
