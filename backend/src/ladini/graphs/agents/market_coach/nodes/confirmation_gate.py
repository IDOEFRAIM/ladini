"""
Point UNIQUE de confirmation humaine (HITL) avant toute action à risque.
Aucun garde-fou équivalent n'existe au niveau protocolaire MCP (infrastructure/mcp/runtime.py
applique uniquement scope + préflight SQL). Tout nouveau chemin d'appel qui BYPASSE ce nœud
s'exécute sans validation humaine.
"""

import time
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.core.base import (
    _READ_GOALS,
    get_node_logger,
)
from ladini.graphs.agents.market_coach.core.goals import (
    DRAFT_BASED_CONFIRMATION_GOALS as _DRAFT_BASED_CONFIRMATION_GOALS,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    get_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary as _build_confirmation_summary,
)
from ladini.graphs.agents.market_coach.utils import (
    llm_deviation_reply as _llm_deviation_reply_impl,
)

logger = get_node_logger("ConfirmationGateNode")

# (2026-09-03/04, refonte transactionnelle) : goals qui possèdent leur
# PROPRE cycle de confirmation versionné (draft canonique + target de
# confirmation lié à une version précise — voir domain/procurement_draft.py,
# domain/sales_publish_draft.py) plutôt que le mécanisme générique
# confirmation_summary/transaction_payload de ce nœud. `confirmation_gate`
# DÉLÈGUE entièrement pour ces goals — il ne doit JAMAIS ré-interpréter un
# état déjà tranché par une décision métier canonique (mandat §15 : le
# routing doit être piloté par InterpreterResult + PendingInteraction +
# DomainContext, pas par des restes d'ancien état).
#
# SALES_PUBLISH_PRODUCT ajouté 2026-09-04 (migration SALES, hardening
# transverse) — audité et confirmé utiliser AVANT ce chantier exactement le
# mécanisme générique `transaction_payload`/`confirmation_summary` que
# cette liste existe pour remplacer (voir rapport final, section C).
#
# (2026-09-30, "interruption d'une confirmation active") : déplacé vers
# `core/goals.py::DRAFT_BASED_CONFIRMATION_GOALS` — `nodes/cognitive.py` a
# désormais besoin de la même information (détection d'une nouvelle
# intention pendant WAITING_CONFIRMATION) ; ré-exporté ici sous l'ancien nom
# pour ne pas toucher le reste de ce fichier.


async def _llm_deviation_reply(
    mc_runtime: Any, user_text: str, summary: str
) -> Optional[str]:
    """Réponse COURTE générée par le LLM quand l'utilisateur dit autre chose
    qu'un oui/non pendant une confirmation en attente — voir
    `utils.py::llm_deviation_reply` (implémentation partagée avec
    `flows/buyer/gps_delivery_gate.py`, voir
    [[precommande-architecture-consolidation-2026-08]])."""
    return await _llm_deviation_reply_impl(
        mc_runtime,
        user_text,
        f"un récapitulatif à confirmer :\n{summary}",
        extra_instructions=(
            "Le récapitulatif ci-dessus reflète DÉJÀ les valeurs les plus "
            "récentes fournies par l'utilisateur (une correction éventuelle a "
            "déjà été appliquée) — accuse juste réception brièvement (ex: "
            '"Noté, c\'est corrigé."), ne demande PAS de reformuler et ne '
            "redemande PAS oui/non toi-même."
        ),
    )


# Goals où un REJECT pendant la confirmation ne doit PAS effacer le brouillon
# (produit/quantité/prix déjà saisis) — l'utilisateur doit pouvoir corriger un
# champ (ex: "non" puis "plafond 400") sans tout reprendre depuis zéro. Bug
# vécu en prod : un producteur voulait juste corriger le prix plafond d'un
# appel d'offres, a répondu "non" au récap, s'est retrouvé traité comme un
# inconnu (goal effacé, plus aucun contexte) dès le message suivant.
#
# Contrairement aux tunnels `producer_update` (flows/producer/flow.py), qui
# contournent ENTIÈREMENT ce nœud pour gérer leur propre cycle CONFIRM/REJECT/
# correction — y compris l'écriture en base, appelée directement par le flow —
# cette liste ne touche QUE le REJECT : le chemin CONFIRM (et donc l'écriture
# réelle, ex. `create_auction`) continue de passer par ce nœud puis l'exécuteur
# générique, inchangé. Sûr par construction : aucune logique d'écriture n'est
# dupliquée ici.
_DRAFT_PRESERVING_REJECT_GOALS = frozenset({"PROCUREMENT_CREATE_REQUEST"})

# Au-delà de ce délai sans réponse claire (oui/non), une confirmation en
# attente est considérée abandonnée plutôt que ré-affichée indéfiniment sur
# un message sans rapport arrivé bien plus tard (ex: un partage de position
# GPS reçu 30 min après une confirmation jamais tranchée). Purement temporel
# — aucune heuristique sur le CONTENU du message.
_CONFIRMATION_TTL_SECONDS = 600.0

_ABANDON_PATCH: Dict[str, Any] = {
    "is_certified": False,
    "execution_authorized": False,
    "confirmation_summary": None,
    "confirmation_raised_at": None,
    "current_goal": None,
    "transaction_payload": {"__reset__": True},
    "missing_fields": [],
    "completed_fields": [],
    "goal_status": "IDLE",
    "status": "COMPLETED",
    "response_strategy": None,
    "ag_ui_component": None,
    "pending_interaction": None,
}


async def _resolve_draft_based_confirmation(
    state: Dict[str, Any], mc_runtime: Any, goal: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Point d'entrée pour les goals à draft canonique versionné (voir
    `_DRAFT_BASED_CONFIRMATION_GOALS`). Deux cas :

    1. Aucun `procurement_draft` encore posé — la collecte initiale des
       champs (mécanisme générique existant : validator/ASK_MISSING_FIELD,
       `transaction_payload`) est TOUJOURS en cours. Ce nœud ne fait rien
       tant que `transaction_payload` ne porte pas tous les champs requis —
       il redevient un no-op (`{}`), laissant le chemin générique gérer ce
       tour EXACTEMENT comme avant pour cette phase (pas encore de contrat
       transactionnel à protéger).
    2. Dès que les champs requis sont réunis, LA PREMIÈRE version du draft
       est créée ICI, une fois — c'est la bascule : à partir de ce tour,
       `transaction_payload` n'est plus lu comme source de vérité pour ce
       goal (voir `nodes/memory.py`, garde symétrique), seul le draft
       compte. Tout tour suivant délègue entièrement à
       `flows/buyer/procurement_confirmation.py::resolve_procurement_confirmation`
       — LA seule autorité de mutation du draft à partir de là."""
    from ladini.graphs.agents.market_coach.domain.procurement_draft import (
        ProcurementDraft,
    )
    from ladini.graphs.agents.market_coach.flows.buyer.procurement_confirmation import (
        resolve_procurement_confirmation,
    )
    from ladini.services.database import procurement_draft_store

    if state.get("procurement_draft") is not None:
        return await resolve_procurement_confirmation(state, mc_runtime)

    import uuid

    candidate = ProcurementDraft.new(draft_id=uuid.uuid4().hex[:12], **payload)
    if not candidate.is_complete():
        # Collecte encore en cours (validateur/slot-filling générique, en
        # amont, gère la relance) — AUCUN draft n'existe encore, donc rien
        # à protéger transactionnellement. Mais I1/I2 (chaos suite,
        # `tests/chaos/test_state_machine_invariants.py`) exigent qu'un
        # `execution_authorized=True` FORGÉ en amont ne survive JAMAIS à ce
        # nœud, même pendant cette phase — défense explicite, pas un simple
        # no-op silencieux.
        return {"execution_authorized": False, "is_certified": False}

    # (2026-09-03, persistance transactionnelle) : la ligne PostgreSQL
    # CANONIQUE est créée ICI, AVANT de poser le cache LangGraph — jamais
    # l'inverse (voir procurement_confirmation.py, qui refuse de faire
    # confiance au cache seul). Best-effort : un échec d'insertion ne
    # bloque pas le tour (repli dégradé déjà géré côté lecture), mais est
    # journalisé bruyamment — ce n'est PAS le chemin normal.
    conversation_id = str(state.get("user_phone") or state.get("session_id") or "")
    inserted = await procurement_draft_store.insert(candidate, conversation_id=conversation_id)
    if not inserted:
        logger.warning(
            "[ConfirmationGate] %s : échec insertion PostgreSQL du draft v1 "
            "(draft_id=%s) — poursuite en mode dégradé (cache LangGraph seul)",
            goal,
            candidate.draft_id,
        )

    logger.info(
        "[ConfirmationGate] %s : draft v1 créé (champs réunis) — draft_id=%s",
        goal,
        candidate.draft_id,
    )
    from ladini.core.telemetry import record_procurement_transaction_event

    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=conversation_id or None,
        message_id=state.get("message_sid"),
        draft_id=candidate.draft_id,
        draft_version_before=None,  # aucune version antérieure — c'est la création
        draft_version_after=candidate.version,
        pending_kind="CONFIRM_ACTION",
        confirmation_target={"draft_id": candidate.draft_id, "draft_version": candidate.version},
        interpreter_event=str(state.get("interpreted_event") or "") or None,
        domain_action="bootstrap_draft_v1",
        state_before=None,
        state_after=candidate.status.value,
        outcome="DRAFT_CREATED" if inserted else "DRAFT_CREATED_DEGRADED_CACHE_ONLY",
    )
    return {
        "procurement_draft": candidate.to_dict(),
        "status": "WAITING_INPUT",
        "response_strategy": "CONFIRMATION",
        "final_response": f"{candidate.render_summary()}\n\nConfirmez-vous ?",
        "ag_ui_component": None,
        # Défense identique à la branche "collecte en cours" ci-dessus : un
        # `execution_authorized`/`is_certified` forgé en amont ne doit
        # JAMAIS survivre à ce nœud tant que le draft n'est pas passé par
        # `resolve_procurement_confirmation` (seul point qui les met à True,
        # et uniquement sur CONFIRM_ACTION résolu — voir apply_response_plan).
        "execution_authorized": False,
        "is_certified": False,
        **set_pending_interaction(
            InteractionKind.CONFIRM_ACTION,
            context_ref="confirmation",
            target={"draft_id": candidate.draft_id, "draft_version": candidate.version},
        ),
    }


async def _resolve_sales_draft_based_confirmation(
    state: Dict[str, Any], mc_runtime: Any, goal: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Équivalent SALES de `_resolve_draft_based_confirmation` ci-dessus —
    même structure en 2 cas (docstring identique en esprit), draft
    `SalesPublishDraft` au lieu de `ProcurementDraft` (2026-09-04, migration
    SALES). Fonction SÉPARÉE plutôt qu'un paramètre de classe générique sur
    `_resolve_draft_based_confirmation` (mandat hardening : pas d'abstraction
    prématurée pour 2 domaines — voir rapport final, section A)."""
    from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
        SalesPublishDraft,
    )
    from ladini.graphs.agents.market_coach.flows.producer.sales_confirmation import (
        resolve_sales_confirmation,
    )
    from ladini.services.database import sales_publish_draft_store

    if state.get("sales_publish_draft") is not None:
        return await resolve_sales_confirmation(state, mc_runtime)

    import uuid

    candidate = SalesPublishDraft.new(draft_id=uuid.uuid4().hex[:12], **payload)
    if not candidate.is_complete():
        # Un état tarifaire COMPRIS ne doit jamais finir sur le fallback générique (incident prod
        # « 60 l de miel ») : on redemande le champ qui manque, dans le tunnel SALES_PUBLISH_PRODUCT.
        # Le `validator` pose déjà la question ciblée ; ceci est le filet si un chemin l'a laissée passer.
        missing = candidate.missing_fields()
        if not missing:
            return {"execution_authorized": False, "is_certified": False}
        logger.warning(
            "[ConfirmationGate] %s : draft candidat incomplet (manque=%s) — clarification, pas de fallback",
            goal,
            ",".join(missing),
        )
        from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
            _missing_field_prompt,
        )

        return {
            "status": "WAITING_INPUT",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": _missing_field_prompt(missing),
            "missing_fields": missing,
            "last_missing_field": missing[0],
            "execution_authorized": False,
            "is_certified": False,
            **set_pending_interaction(
                InteractionKind.ENTER_FIELD, goal=goal, field_name=missing[0]
            ),
        }

    conversation_id = str(state.get("user_phone") or state.get("session_id") or "")
    inserted = await sales_publish_draft_store.insert(candidate, conversation_id=conversation_id)
    if not inserted:
        logger.warning(
            "[ConfirmationGate] %s : échec insertion PostgreSQL du draft v1 "
            "(draft_id=%s) — poursuite en mode dégradé (cache LangGraph seul)",
            goal,
            candidate.draft_id,
        )

    logger.info(
        "[ConfirmationGate] %s : draft v1 créé (champs réunis) — draft_id=%s",
        goal,
        candidate.draft_id,
    )
    from ladini.core.telemetry import record_procurement_transaction_event

    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=conversation_id or None,
        message_id=state.get("message_sid"),
        draft_id=candidate.draft_id,
        draft_version_before=None,
        draft_version_after=candidate.version,
        pending_kind="CONFIRM_ACTION",
        confirmation_target={"draft_id": candidate.draft_id, "draft_version": candidate.version},
        interpreter_event=str(state.get("interpreted_event") or "") or None,
        domain_action="bootstrap_draft_v1",
        state_before=None,
        state_after=candidate.status.value,
        outcome="DRAFT_CREATED" if inserted else "DRAFT_CREATED_DEGRADED_CACHE_ONLY",
    )
    if candidate.offer is not None:
        # CERTIFICATION (Phase B1) : draft_id + version + offre + représentation normalisée figés
        # au passage en attente de confirmation ; la clé d'idempotence de l'écriture dérive de la
        # version qui suivra le CONFIRM (voir `sales_publish_draft.execution_key`).
        from ladini.graphs.agents.market_coach.services.domain.commercial_gate import (
            log_offer_lifecycle,
        )

        log_offer_lifecycle(
            "COMMERCIAL_OFFER_CERTIFIED",
            state,
            candidate.offer,
            draft_id=candidate.draft_id,
            version=candidate.version,
            idempotency_key=f"sales_publish:{candidate.draft_id}:{candidate.version + 1}",
        )
    return {
        "sales_publish_draft": candidate.to_dict(),
        "status": "WAITING_INPUT",
        "response_strategy": "CONFIRMATION",
        "final_response": f"{candidate.render_summary()}\n\nConfirmez-vous ?",
        "ag_ui_component": None,
        "execution_authorized": False,
        "is_certified": False,
        **set_pending_interaction(
            InteractionKind.CONFIRM_ACTION,
            context_ref="confirmation",
            target={"draft_id": candidate.draft_id, "draft_version": candidate.version},
        ),
    }


_DRAFT_BASED_RESOLVER_BY_GOAL = {
    "PROCUREMENT_CREATE_REQUEST": _resolve_draft_based_confirmation,
    "SALES_PUBLISH_PRODUCT": _resolve_sales_draft_based_confirmation,
}


async def _cancel_old_draft_for_switch(
    old_goal: str, old_draft_dict: Dict[str, Any], old_pending_interaction: Optional[Dict[str, Any]],
    state: Dict[str, Any], mc_runtime: Any,
) -> Dict[str, Any]:
    """Annule (persistance CAS comprise) le draft canonique du goal ABANDONNÉ, en
    réutilisant le résolveur CANCEL déjà testé de ce goal (`resolve_sales_confirmation`/
    `resolve_procurement_confirmation`) plutôt qu'en dupliquant sa logique — mandat
    Étape 5 : « une transaction terminée doit être définitivement morte dans le state
    actif, tout en restant disponible dans l'historique/persistence » s'applique ICI
    exactement comme à un CANCEL ordinaire, ce draft n'ayant jamais été confirmé."""
    cancel_state = {
        **state,
        "current_goal": old_goal,
        "sales_publish_draft": old_draft_dict if old_goal == "SALES_PUBLISH_PRODUCT" else state.get("sales_publish_draft"),
        "procurement_draft": old_draft_dict if old_goal == "PROCUREMENT_CREATE_REQUEST" else state.get("procurement_draft"),
        "interpreted_event": "CANCEL",
        "extracted_entities": {},
        "pending_interaction": old_pending_interaction,
    }
    if old_goal == "SALES_PUBLISH_PRODUCT":
        from ladini.graphs.agents.market_coach.flows.producer.sales_confirmation import (
            resolve_sales_confirmation,
        )

        return dict(await resolve_sales_confirmation(cancel_state, mc_runtime))
    if old_goal == "PROCUREMENT_CREATE_REQUEST":
        from ladini.graphs.agents.market_coach.flows.buyer.procurement_confirmation import (
            resolve_procurement_confirmation,
        )

        return dict(await resolve_procurement_confirmation(cancel_state, mc_runtime))
    return {}


def _render_summary_for_goal(old_goal: str, old_draft_dict: Dict[str, Any]) -> str:
    if not old_draft_dict:
        return ""
    if old_goal == "SALES_PUBLISH_PRODUCT":
        from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
            SalesPublishDraft,
        )

        draft = SalesPublishDraft.from_dict(old_draft_dict)
        return draft.render_summary() if draft else ""
    if old_goal == "PROCUREMENT_CREATE_REQUEST":
        from ladini.graphs.agents.market_coach.domain.procurement_draft import (
            ProcurementDraft,
        )

        draft = ProcurementDraft.from_dict(old_draft_dict)
        return draft.render_summary() if draft else ""
    return ""


async def _resolve_confirmation_switch(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    """Résout la réponse oui/non à la question posée par `nodes/cognitive.py`
    (`ASK_SWITCH_CONFIRMATION`, `pending_interaction.context_ref ==
    "confirmation_switch"`) quand une nouvelle intention explicite est arrivée pendant
    une CONFIRM_ACTION sur un draft canonique versionné déjà actif (mandat §A2/§A3).

    Interceptée en tête de `confirmation_gate` (avant le dispatch `_DRAFT_BASED_
    CONFIRMATION_GOALS` normal) : `current_goal` porte encore l'ANCIEN goal à ce
    stade — le dispatch générique interpréterait `pending_interaction.target` (la
    forme dédiée posée par `cognitive_guard`, PAS `{"draft_id":..., "draft_version":
    ...}`) comme une cible de confirmation ordinaire et échouerait silencieusement.

    - CONFIRM (« oui ») : annule l'ancien draft (persistance CAS comprise, voir
      `_cancel_old_draft_for_switch`) PUIS démarre la nouvelle intention à partir des
      entités déjà dites (« je veux vendre mon miel » -> product=miel).
      (2026-09-30, corrigé — incident trouvé en non-régression : "finalement je vends
      20 boeufs à 450000 la tête" créait directement un draft "complet" sans jamais
      passer par `validator`/`commercial_gate`, donc sans jamais détecter une base de
      prix ambiguë ("par tête ou pour l'ensemble ?") qu'un tour NORMAL aurait posée
      AVANT toute confirmation — voir `tests/integration/
      test_commercial_pricing_vertical_slice.py::TestInvalidation`.) Les entités
      déjà dites sont donc d'abord rejouées à travers le VALIDATEUR normal (même
      nœud qu'un tour ordinaire) ; s'il lève sa propre question (ENTER_FIELD), CE
      tour s'arrête là, comme n'importe quel démarrage de goal ordinaire — seulement
      s'il résout proprement, le bootstrap standard (`_DRAFT_BASED_RESOLVER_BY_GOAL`)
      pose la question du premier champ manquant. Jamais de re-saisie demandée à
      l'utilisateur pour ce qu'il a déjà dit.
    - Tout le reste (REJECT, ou toute déviation) : reste CONSERVATEUR (mandat §A3) —
      restaure la confirmation d'origine telle quelle, l'ancien draft n'est JAMAIS
      touché."""
    pending = get_pending_interaction(state)
    target = pending.target or {}
    old_goal = str(target.get("old_goal") or "").upper()
    old_draft_dict = dict(target.get("old_draft") or {})
    old_pending_interaction = target.get("old_pending_interaction")
    incoming_goal = str(target.get("incoming_goal") or "").upper()
    incoming_entities = dict(target.get("incoming_entities") or {})
    event = str(state.get("interpreted_event") or "").upper()

    if event != "CONFIRM":
        # (§A3) Non/déviation : l'ancien draft n'a JAMAIS été touché par la question
        # elle-même (cognitive_guard ne fait que poser `pending_interaction` — voir sa
        # docstring) — il suffit de restaurer la confirmation qui attendait déjà.
        summary = _render_summary_for_goal(old_goal, old_draft_dict)
        return {
            "pending_interaction": old_pending_interaction,
            "pending_goal": None,
            "status": "WAITING_INPUT",
            "response_strategy": "SUCCESS",
            "final_response": (
                f"D'accord, on reste sur la confirmation en cours.\n{summary}\n\nConfirmez-vous ?"
                if summary
                else "D'accord, on reste sur la confirmation en cours."
            ),
            "ag_ui_component": None,
        }

    logger.info(
        "CONFIRMATION_INTERRUPTION_DETECTED | current_goal=%s | incoming_goal=%s | "
        "current_status=%s | action=CONFIRMED_SWITCH",
        old_goal,
        incoming_goal,
        state.get("status"),
    )
    cancel_patch = await _cancel_old_draft_for_switch(
        old_goal, old_draft_dict, old_pending_interaction, state, mc_runtime
    )

    bootstrap_state = {
        **state,
        "current_goal": incoming_goal,
        "interpreted_event": "NEW_TASK",
        "extracted_entities": incoming_entities,
        "transaction_payload": incoming_entities,
        "sales_publish_draft": None,
        "procurement_draft": None,
        "pending_interaction": None,
    }
    from ladini.graphs.agents.market_coach.nodes.validation import validator

    validator_patch = await validator(bootstrap_state, mc_runtime)
    bootstrap_state = {**bootstrap_state, **validator_patch}
    validator_pending = validator_patch.get("pending_interaction")
    # Étroit DÉLIBÉRÉMENT : `validator` pose aussi `pending_interaction=ENTER_FIELD` pour un
    # champ manquant ORDINAIRE (ex: "quantity") — ce cas-là, `_DRAFT_BASED_RESOLVER_BY_GOAL`
    # (ci-dessous) le gère déjà très bien tout seul (même `missing_fields()`/prompt). Seule une
    # VRAIE ambiguïté COMMERCIALE (base de prix/contenu de conditionnement — voir
    # `services/domain/commercial_gate.py::CommercialQuestion`) doit court-circuiter le
    # bootstrap : c'est la question qu'un tour ORDINAIRE aurait posée AVANT toute confirmation
    # et que le bootstrap, lui, ne sait pas reproduire (il construit directement un draft
    # "complet" dès que product+quantity+price sont là, sans trancher la base du prix).
    validator_asks_something = bool(
        isinstance(validator_pending, dict)
        and str(validator_pending.get("kind") or "NONE") != "NONE"
        and isinstance(validator_pending.get("target"), dict)
        and validator_pending["target"].get("kind") == "COMMERCIAL_QUESTION"
    )

    new_goal_patch: Dict[str, Any] = {}
    if not validator_asks_something:
        bootstrap_resolver = _DRAFT_BASED_RESOLVER_BY_GOAL.get(incoming_goal)
        if bootstrap_resolver is not None:
            new_goal_patch = await bootstrap_resolver(
                bootstrap_state, mc_runtime, incoming_goal,
                bootstrap_state.get("transaction_payload") or incoming_entities,
            )
    else:
        new_goal_patch = dict(validator_patch)

    # (2026-09-30, corrigé — incident trouvé en non-régression) : QUELLE QUE SOIT la
    # branche ci-dessus, `transaction_payload` DOIT porter le sentinel `__reset__` —
    # `transaction_payload` est un canal `merge_dict` (voir `agents/reducers.py`) : un
    # patch de `new_goal_patch`/`validator_patch` SANS ce sentinel (le cas normal pour
    # CE champ, puisqu'un tour ORDINAIRE part d'un état déjà propre) se fusionnerait
    # avec l'ANCIEN `transaction_payload` — encore celui du goal abandonné à ce
    # stade — au lieu de le remplacer. D'où le calcul explicite ici plutôt que de
    # laisser `**new_goal_patch` fournir sa propre clé `transaction_payload` telle
    # quelle.
    final_payload = new_goal_patch.get("transaction_payload") or bootstrap_state.get(
        "transaction_payload"
    ) or incoming_entities
    new_goal_patch = {k: v for k, v in new_goal_patch.items() if k != "transaction_payload"}

    return {
        **cancel_patch,
        "current_goal": incoming_goal,
        "goal_status": "ACTIVE",
        "pending_goal": None,
        "sales_publish_draft": None,
        "procurement_draft": None,
        "transaction_payload": {"__reset__": True, **final_payload},
        "stable_entities": {
            "__reset__": True,
            **{k: v for k, v in incoming_entities.items() if k in ("product", "unit")},
        },
        **new_goal_patch,
    }


async def confirmation_gate(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Gère l'état d'approbation explicite avant l'écriture en base de données."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    event = str(state.get("interpreted_event") or "").upper()

    if get_pending_interaction(state).context_ref == "confirmation_switch":
        return await _resolve_confirmation_switch(state, mc_runtime)

    if goal in _DRAFT_BASED_CONFIRMATION_GOALS:
        resolver = _DRAFT_BASED_RESOLVER_BY_GOAL[goal]
        return await resolver(state, mc_runtime, goal, payload)

    if goal in _READ_GOALS:
        return {
            "is_certified": True,
            "execution_authorized": True,
            "status": "EXECUTING",
            "ag_ui_component": None,
            **clear_pending_interaction("read_goal_auto_authorized"),
        }

    deviation_note: Optional[str] = None

    # (2026-09-02, "no legacy shim") : source UNIQUE — `waiting_for_confirmation`
    # et `expected_input` ne sont plus lus ici. `get_pending_interaction` gère
    # elle-même, dans SON seul module, le pont de transition pour les
    # conversations déjà persistées avant ce déploiement (voir
    # core/pending_interaction.py) — ce node n'a plus à connaître ces
    # anciens champs du tout.
    awaiting_confirmation = (
        get_pending_interaction(state).kind == InteractionKind.CONFIRM_ACTION
    )
    if awaiting_confirmation:
        if event == "CONFIRM":
            # (2026-09-28, hardening P1-A — invariant "ce que l'utilisateur a
            # confirmé == ce que le métier exécute") : `confirmation_summary_goal`/
            # `confirmation_summary_payload` sont le seul snapshot GELÉ de ce qui a
            # RÉELLEMENT été montré à l'utilisateur (posés une fois, ci-dessous, au
            # moment où CETTE confirmation a été levée — jamais retouchés depuis,
            # `state.py` les documente déjà comme le garde-fou anti-péremption de
            # `render_confirmation`). Sans cette vérification, un `transaction_payload`
            # devenu périmé ENTRE la levée de la confirmation et la réception de
            # CONFIRM (contamination cross-flow, correction non voulue d'un autre
            # tunnel, état orphelin) certifiait quand même l'action — l'exécuteur
            # (`mcp_tool_executor`) lit `transaction_payload`, pas le récap montré.
            # Un but courant différent de celui pour lequel la confirmation a été
            # levée (ex: A8 — une nouvelle action a démarré sans que cette
            # confirmation-ci ait été explicitement tranchée) est traité EXACTEMENT
            # comme une confirmation périmée/orpheline : abandon, jamais certification
            # d'une action que l'utilisateur n'a jamais vue sous cette forme.
            certified_goal = state.get("confirmation_summary_goal")
            certified_payload = state.get("confirmation_summary_payload")
            if certified_goal != goal or not certified_payload:
                logger.warning(
                    "[ConfirmationGate] CONFIRM reçu mais confirmation_summary_goal=%r "
                    "!= goal courant=%r (ou confirmation_summary_payload manquant) — "
                    "abandon au lieu de certifier une action jamais réellement montrée",
                    certified_goal,
                    goal,
                )
                return dict(_ABANDON_PATCH)
            # (2026-09-28, observability P1-A) : `flow_certified` — corrélation
            # (goal, message_sid) seulement, jamais le contenu du payload
            # (prix/quantité/texte libre).
            logger.info(
                "flow_certified | goal=%s | message_sid=%s",
                goal,
                state.get("message_sid"),
            )
            return {
                "is_certified": True,
                "execution_authorized": True,
                "confirmation_raised_at": None,
                "status": "EXECUTING",
                "ag_ui_component": None,
                # (2026-09-28, hardening P1-A) : `mcp_tool_executor` lit
                # `transaction_payload`, PAS `confirmation_summary_payload` — cette
                # bascule fait consommer à l'exécution EXACTEMENT le snapshot gelé
                # déjà vérifié ci-dessus, jamais une valeur live qui aurait pu
                # diverger entre-temps. `{"__reset__": True, **data}` (`merge_dict`,
                # voir `agents/reducers.py`) REMPLACE entièrement le payload courant
                # — une simple fusion laisserait survivre des clés périmées absentes
                # du snapshot certifié.
                "transaction_payload": {"__reset__": True, **dict(certified_payload)},
                **resolve_pending_interaction(),
            }
        if event == "REJECT":
            if goal in _DRAFT_PRESERVING_REJECT_GOALS:
                # Repli DOUX : n'efface QUE l'état de confirmation (résumé,
                # drapeau d'attente) — `current_goal`/`transaction_payload`/
                # `active_form`/`form_data` survivent intacts. Le tour suivant
                # retombe naturellement sur le flow dédié (ex:
                # `buyer_request_resolver`, ré-entrée déjà existante sur
                # `active_form == "AUCTION_CREATE"`), qui ré-affichera un récap
                # à jour si l'utilisateur fournit une correction — ou, si
                # l'utilisateur répond "non"/"annuler" une SECONDE fois sans
                # rien d'autre, `goal_planner` applique déjà son repli
                # générique complet (RÈGLE 1, hors confirmation) : rien à
                # dupliquer ici pour ce cas.
                logger.info(
                    "[ConfirmationGate] REJECT sur %s — brouillon préservé (correction possible au tour suivant)",
                    goal,
                )
                return {
                    "is_certified": False,
                    "execution_authorized": False,
                    "confirmation_raised_at": None,
                    "confirmation_summary": None,
                    "goal_status": "ACTIVE",
                    "status": "PLANNING",
                    "response_strategy": "SUCCESS",
                    "final_response": (
                        "D'accord, ce n'est pas encore confirmé. Dites-moi ce que "
                        "vous voulez modifier (prix, quantité, date), ou répondez "
                        "*annuler* pour abandonner."
                    ),
                    "ag_ui_component": None,
                    **clear_pending_interaction("reject_draft_preserved"),
                }
            return {
                "is_certified": False,
                "execution_authorized": False,
                "confirmation_raised_at": None,
                "current_goal": None,
                "transaction_payload": {"__reset__": True},
                "missing_fields": [],
                "completed_fields": [],
                "goal_status": "COMPLETED",
                "status": "COMPLETED",
                "response_strategy": "CLARIFICATION",
                "final_response": "Opération annulée. Que souhaitez-vous faire ?",
                "ag_ui_component": None,
                **clear_pending_interaction("reject_cancelled"),
            }

        # Ni CONFIRM ni REJECT : soit une correction (gérée en amont par les
        # flows dédiés avant d'atteindre ce nœud), soit un message sans
        # rapport (ex: partage GPS, changement de sujet). Résilience : si la
        # confirmation traîne trop longtemps (péremption) OU si le goal
        # associé est introuvable (état orphelin — ne devrait plus arriver
        # depuis la préservation de `current_goal` par post_response_cleanup,
        # mais on se protège quand même), on abandonne silencieusement au
        # lieu de ré-afficher un récap confus voire vide.
        raised_at = state.get("confirmation_raised_at")
        is_stale = (
            bool(raised_at)
            and (time.time() - float(raised_at)) > _CONFIRMATION_TTL_SECONDS
        )
        is_orphaned = not goal or not payload
        if is_stale or is_orphaned:
            logger.info(
                "[ConfirmationGate] Confirmation abandonnée (stale=%s, orphaned=%s, goal=%r)",
                is_stale,
                is_orphaned,
                goal,
            )
            return dict(_ABANDON_PATCH)

        # Ni stale ni orphelin : un vrai écart (question, correction, remarque)
        # pendant une confirmation en attente. Sans ceci, le récap se
        # répétait mot pour mot en boucle, quoi que dise l'utilisateur — bug
        # réel (2026-08-14), même défaut que l'onboarding avant
        # [[onboarding-adaptive-questions-2026-08]].
        #
        # Bug réel confirmé (2026-08-16) : quand la correction ("non non c'est
        # 35 unité") est déjà appliquée par les nœuds amont AVANT ce nœud
        # (`transaction_payload` mis à jour), le récap final (`summary`
        # ci-dessous, calculé APRÈS ce bloc) reflétait bien la correction —
        # mais la note LLM, elle, était générée à partir de l'ANCIEN résumé
        # (`state.get("confirmation_summary")`, celui du tour PRÉCÉDENT), donc
        # le LLM ne "voyait" pas la correction déjà appliquée et répondait
        # "pouvez-vous reformuler ?" juste avant d'afficher un récap DÉJÀ
        # corrigé — contradictoire et déroutant. Fix : construire le résumé
        # FRAIS une seule fois ici (à partir du `payload` courant) et le
        # réutiliser pour la note LLM ET le récap final, au lieu de deux
        # sources désynchronisées. Voir
        # [[precommande-architecture-consolidation-2026-08]].
        user_text = str(
            state.get("normalized_text") or state.get("user_query") or ""
        ).strip()
        fresh_summary = _build_confirmation_summary(goal, payload)
        deviation_note = await _llm_deviation_reply(
            mc_runtime, user_text, fresh_summary
        )

    # Garde-fou symétrique à celui du ré-affichage ci-dessus : ne JAMAIS lever
    # une confirmation sans goal ni payload — ça ne peut produire qu'un récap
    # vide/incompréhensible ("Validation de l'opération : " sans rien après).
    # Un routage a mal résolu le goal courant AVANT d'arriver ici (bug amont) ;
    # mieux vaut abandonner proprement que d'exposer l'état interne cassé.
    if not goal or not payload:
        logger.warning(
            "[ConfirmationGate] Appel avec goal/payload vide — abandon au lieu d'un récap creux (goal=%r, payload_keys=%s)",
            goal,
            list(payload.keys()),
        )
        return dict(_ABANDON_PATCH)

    summary = _build_confirmation_summary(goal, payload)
    return {
        "is_certified": False,
        "execution_authorized": False,
        "confirmation_summary": summary,
        "confirmation_summary_goal": goal,
        "confirmation_summary_payload": dict(payload),
        "confirmation_deviation_note": deviation_note,
        "confirmation_raised_at": state.get("confirmation_raised_at") or time.time(),
        "last_agent_question": summary,
        "status": "WAITING_CONFIRMATION",
        "goal_status": "WAITING_CONFIRMATION",
        "response_strategy": "CONFIRMATION",
        # (2026-09-02) confirmation_gate.py est le SEUL écrivain de
        # CONFIRM_ACTION — voir core/pending_interaction.py. context_ref
        # pointe vers confirmation_summary/transaction_payload (déjà posés
        # ci-dessus), pas de duplication du contenu détaillé.
        **set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, goal=goal, context_ref="confirmation"
        ),
        # QuickReplies (audit UX interactive 2026-08-27) : remplace
        # FormConfirmation, un composant que rien ne lisait jamais —
        # orchestrator.py::_interactive_hint ne reconnaissait que ListMenu,
        # donc la confirmation retombait sur les flags status/
        # response_strategy ci-dessus, déconnectés de ce composant. Les
        # id "CONFIRM"/"REJECT" correspondent exactement aux valeurs déjà
        # matchées par le bypass zéro-token (interpreter/routing.py).
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "QuickReplies"],
            "kwargs": {
                "body": summary,
                "buttons": [
                    {"id": "CONFIRM", "title": "✅ Confirmer"},
                    {"id": "REJECT", "title": "❌ Annuler"},
                ],
                "metadata": {"goal": goal},
            },
        },
    }
