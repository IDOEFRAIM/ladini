"""UI Engine — matérialise un ``MenuRequest`` déjà décidé en interaction de
sélection sûre.

Responsabilité (2026-09-09, audit de clôture) : un flow/resolver a DÉJÀ
choisi le menu métier (``MenuRequest``, voir ``flows/common/menu_contracts.py``).
Ce nœud ne décide RIEN de métier — ni quel menu, ni quel goal, ni quel rôle,
ni si une transaction est valide — il transforme ce contrat déjà tranché en :

  - ``ag_ui_component``, ``available_mapping``, ``expected_candidates``
  - ``PendingInteraction.SELECTION_MENU`` (voir §"MenuRequest ⇒ sélection"
    plus bas)
  - un identifiant de snapshot stable (``menu_snapshot_id``)
  - ``final_response`` fusionné avec ``menu.preformatted_text``

Contrat MenuRequest ⇒ sélection : ce nœud est le SEUL consommateur de
``state["pending_menu"]`` dans le graphe compilé — tout ``MenuRequest`` qui
l'atteint doit donc être répondu par une sélection numérique/texte, jamais
une simple liste informative. C'est une propriété du contrat
``MenuRequest`` lui-même (validé à la construction, voir
``menu_contracts.py::MenuRequest.__post_init__``), pas une décision prise
ici.

Legacy : les flows qui construisent encore ``ag_ui_component`` directement
(hors ``pending_menu``) continuent de fonctionner — ce nœud est un
pass-through silencieux si ``pending_menu`` n'est pas un ``MenuRequest``.

COMPATIBILITY_SHIM : ``working_memory["menu_snapshot_id"]`` reste écrit en
plus de ``state["menu_snapshot_id"]`` (désormais la source CANONIQUE
déclarée, voir ``core/state.py``) parce que ``nodes/memory.py`` — hors
périmètre de cet audit — lit encore exclusivement la copie
``working_memory``. Ne pas retirer sans auditer ``memory.py``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)

logger = logging.getLogger("AgriConnect.Market.UIEngine")


# =====================================================================
# IDENTITÉ DE SESSION DU SNAPSHOT
# =====================================================================


def _resolve_menu_session_key(state: Dict[str, Any]) -> Optional[str]:
    """Résout l'identité de session utilisée pour namespacer le snapshot.

    Doit rester BYTE-COMPATIBLE avec le calcul indépendant fait par
    ``nodes/memory.py`` (``str(state.get("session_id") or
    state.get("user_phone") or "")``, hors périmètre de cet audit) : les
    deux calculs DOIVENT produire la même clé pour que
    ``menu_snapshot_store.resolve()`` retrouve le snapshot posé ici. Pas de
    nouvel identifiant inventé (thread_id/UUID) — ``user_phone`` est déjà
    l'identité canonique utilisée par tout le reste du graphe
    (``session_bootstrap``, `security_moderation``, l'executor...).

    Retourne ``None`` (jamais ``""``) quand aucune identité fiable
    n'existe — c'est exactement le cas où ``memory.py`` calculerait aussi
    une chaîne vide, que ``MenuSnapshotStore.get()`` refuse déjà
    (``if not session_id: return None``) : ne PAS sauvegarder un snapshot
    dans ce cas ne perd donc aucune capacité de résolution réelle.
    """
    raw = state.get("session_id") or state.get("user_phone")
    if not raw:
        return None
    key = str(raw)
    return key if key.strip() else None


# =====================================================================
# IDEMPOTENCE DU SNAPSHOT
# =====================================================================


def _compute_menu_idempotency_key(menu: MenuRequest, turn_count: Any) -> str:
    """Clé stable pour ``menu_snapshot_store.save(idempotency_key=...)``.

    Un replay LangGraph (retry, reprise de checkpoint, double invocation
    accidentelle) doit retrouver le MÊME snapshot pour le même menu
    logique dans le même tour — pas en fabriquer un second, incompatible.
    Dérivée de : identité de tour (``turn_count``) + ``menu.kind`` +
    empreinte du contenu (titre + options triées par position, jamais par
    valeur — l'ORDRE fait partie du contenu affiché). Le store scope déjà
    cette clé par session (voir ``MenuSnapshotStore.save``), donc elle n'a
    pas besoin d'inclure l'identité de session elle-même.
    """
    fingerprint_source = "|".join(
        [
            menu.title,
            menu.kind,
            *(
                f"{opt.index}:{opt.label}:{opt.effective_value()}"
                for opt in menu.options
            ),
        ]
    )
    content_hash = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    return f"{turn_count}:{menu.kind}:{content_hash}"


# =====================================================================
# TEXTE — fusion base_text / preformatted_text (fonction pure)
# =====================================================================


def _merge_menu_text(base_text: str, preformatted_text: Optional[str]) -> Optional[str]:
    """Fusionne le texte déjà présent (``state.final_response``) avec le
    texte préformaté du menu — sans dupliquer si les deux sont
    équivalents à la casse/l'espacement près. Fonction pure : aucun accès
    LLM, aucun état global. Retourne ``None`` si rien à produire (laisse
    l'appelant décider du repli)."""
    instructions = (preformatted_text or "").strip()
    if not instructions:
        return base_text or None

    if not base_text:
        return instructions

    normalized_base = " ".join(base_text.split())
    normalized_instructions = " ".join(instructions.split())
    if normalized_base == normalized_instructions:
        return base_text
    return f"{base_text}\n\n{instructions}".strip()


# =====================================================================
# PUBLIC NODE
# =====================================================================


async def ui_engine(
    state: Dict[str, Any], _mc_runtime: Any | None = None, **_kwargs: Any
) -> Dict[str, Any]:
    """Nœud LangGraph : transforme un ``MenuRequest`` en composant AG-UI.

    Paramètres ignorés via ``**_kwargs`` pour compatibilité avec
    ``_safe_node`` qui injecte ``mc_runtime``. 0 LLM, 0 décision métier,
    0 filtrage par rôle — voir docstring de module.
    """
    menu = state.get("pending_menu")

    # Pas de menu en attente → pass-through silencieux. Aucun snapshot,
    # aucune mutation de pending_interaction.
    if not isinstance(menu, MenuRequest):
        return {}

    logger.info(
        "ui_engine: rendering menu kind=%s options=%d title=%s",
        menu.kind,
        len(menu.options),
        menu.title[:40],
    )

    ag_ui = _build_ag_ui_component(menu)
    mapping = menu.to_mapping()
    candidates = menu.to_candidates()
    wm_patch = menu.to_working_memory_patch()

    session_key = _resolve_menu_session_key(state)
    snapshot_id: Optional[str] = None
    if session_key is None:
        # (mandat §5-7) : jamais de fallback "" -> namespace partagé. Le
        # menu reste affichable en toute sécurité SANS snapshot : la
        # résolution de la sélection au tour suivant lit `available_mapping`
        # (état DURABLE) en PREMIER — le snapshot n'est qu'un filet de
        # secours si ce champ a été perdu. On dégrade donc explicitement,
        # jamais silencieusement.
        logger.error(
            "[UIEngine] CONTRACT VIOLATION — aucune identité de session "
            "fiable (ni session_id ni user_phone) : menu kind=%s rendu SANS "
            "snapshot (filet de secours indisponible pour ce tour).",
            menu.kind,
        )
    else:
        turn_count = state.get("turn_count")
        idempotency_key = _compute_menu_idempotency_key(menu, turn_count)
        try:
            snapshot = menu_snapshot_store.save(
                session_key,
                mapping,
                kind=menu.kind,
                metadata=menu.metadata,
                idempotency_key=idempotency_key,
            )
            snapshot_id = snapshot.menu_id
        except Exception as exc:
            # (mandat §30-31) : le store est indisponible — dégrader vers un
            # menu SANS snapshot (comme le cas "pas d'identité" ci-dessus)
            # plutôt que de faire échouer tout le tour. Sûr pour la MÊME
            # raison : `available_mapping` reste la source primaire de
            # résolution, le snapshot n'est qu'un filet de secours.
            logger.error(
                "[UIEngine] menu_snapshot_store.save a échoué (%s) — menu "
                "kind=%s rendu sans snapshot (filet de secours indisponible "
                "pour ce tour).",
                exc,
                menu.kind,
            )

    if snapshot_id is not None:
        wm_patch["menu_snapshot_id"] = snapshot_id
        ag_ui.setdefault("kwargs", {}).setdefault("metadata", {})["menu_snapshot_id"] = (
            snapshot_id
        )

    result: Dict[str, Any] = {
        "ag_ui_component": ag_ui,
        "available_mapping": mapping,
        "expected_candidates": candidates,
        # (mandat §12-14) : PAS de `goal=` — recherche exhaustive faite,
        # `PendingInteraction.goal` n'a AUCUN lecteur réel dans tout
        # `src/agriconnect` (ni pour SELECTION_MENU ni pour les autres
        # kinds qui le posent). L'identification du propriétaire du menu
        # passe déjà par `context_ref`/`menu_snapshot_id`/
        # `available_mapping_kind` — ajouter `goal` inventerait une
        # dépendance sans lecteur, pas un contrat existant.
        **set_pending_interaction(
            InteractionKind.SELECTION_MENU, context_ref="ui_menu"
        ),
        "working_memory": {
            **(state.get("working_memory") or {}),
            **wm_patch,
        },
        # Consommer le menu pour qu'il ne soit pas re-traité — uniquement
        # atteint après matérialisation réussie du contrat de sélection
        # (mapping/candidates/pending_interaction déjà dans `result` à ce
        # point) : un échec AVANT ce point (ex. `_build_ag_ui_component`
        # qui lèverait) ne consommerait jamais le menu.
        "pending_menu": None,
    }
    if snapshot_id is not None:
        result["menu_snapshot_id"] = snapshot_id

    merged_text = _merge_menu_text(
        (state.get("final_response") or "").strip(), menu.preformatted_text
    )
    if merged_text is not None:
        result["final_response"] = merged_text

    return result


# =====================================================================
# PRIVATE — construction du dict AG-UI (fonction pure)
# =====================================================================


def _build_ag_ui_component(menu: MenuRequest) -> Dict[str, Any]:
    """Construit le dictionnaire ``ag_ui_component`` normalisé.

    Fonction PURE (mandat §23) : aucun IO, aucune lecture de state global,
    aucune sauvegarde de snapshot. ``options_list``/``mapping``/
    ``candidates`` sont tous les trois dérivés de ``menu.options`` dans le
    MÊME ordre — l'alignement index/position est garanti par construction,
    pas par une synchronisation manuelle (voir test de contrat dédié)."""
    options_list = [{"index": opt.index, "label": opt.label} for opt in menu.options]
    metadata = dict(menu.metadata)
    metadata.setdefault("kind", menu.kind)

    return {
        "lc_type": "constructor",
        "id": ["ag_ui", "ListMenu"],
        "kwargs": {
            "title": menu.title,
            "options": options_list,
            "metadata": metadata,
        },
    }


__all__ = ["ui_engine"]
