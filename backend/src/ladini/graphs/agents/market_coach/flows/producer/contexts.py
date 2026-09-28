"""Producer domain workflow contracts (P2-4, audit architectural 2026-09-08).

Ces deux mini machines à états (``bid_phase``, ``update_phase``) vivent
EXCLUSIVEMENT dans ``working_memory`` (dict générique `merge_dict`,
`core/state.py`) — jamais promues en canaux `MarketAgentState` dédiés, car
elles sont strictement internes à un seul flow producteur chacune (pas de
lecteur cross-domaine comme `preorder_workflow`/`negotiation_context` côté
acheteur, qui ELLES sont déjà typées dans `flows/buyer/contexts.py`).

Mandat P2-4 : ne pas convertir en sous-graphe (pas de bénéfice démontré —
chaque machine est un simple cycle demande/collecte/confirme porté par 1-2
fonctions), mais leur donner une IDENTITÉ TYPÉE — un ensemble de clés
`working_memory` déclaré une seule fois — pour que `nodes/cognitive.py`
(abandon de tunnel) n'ait plus besoin de connaître une liste littérale
arbitraire de clés internes à chaque domaine : il importe désormais
``BidWorkflowState.KEYS`` / ``ProducerUpdateWorkflowState.KEYS`` au lieu de
les recopier à la main (source de dérive si un flow ajoute une clé sans
penser à mettre à jour le nettoyeur de tunnel).

Les sites d'écriture réels (`flows/producer/auctions.py`,
`flows/producer/flow.py`) continuent d'écrire directement les clés
`working_memory` — comportement inchangé, aucun risque de régression
introduit par ce fichier, qui est un contrat de LECTURE (déclaration des
clés + phases), pas une réécriture des écrivains existants.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet


class BidWorkflowState:
    """Contrat typé de la mini machine à états `bid_phase`
    (`flows/producer/auctions.py`).

    Phases (non strictement ordonnées — deux cycles indépendants) :
        ASK_PRICE         → nouvelle offre, prix demandé.
        CONFIRM            → prix reçu, confirmation demandée.
        ASK_PRICE_MODIFY   → modification d'offre existante, nouveau prix demandé.
        CONFIRM_MODIFY      → nouveau prix reçu, confirmation demandée.
        ASK_BASIS[_MODIFY]  → (Phase B2b) montant reçu SANS base fiable : « par tonne ou pour l'ensemble ? ».
        ASK_PACKAGE[_MODIFY]→ (Phase B2b) prix au conditionnement sans contenu : « quelle quantité ? ».
    """

    PHASES = (
        "ASK_PRICE", "CONFIRM", "ASK_PRICE_MODIFY", "CONFIRM_MODIFY",
        "ASK_BASIS", "ASK_PACKAGE", "ASK_BASIS_MODIFY", "ASK_PACKAGE_MODIFY",
    )

    KEYS: FrozenSet[str] = frozenset(
        {
            "bid_phase", "pending_bid_auction", "pending_bid_price", "pending_modify_bid",
            "pending_bid_pricing",
        }
    )

    def __init__(self, working_memory: Dict[str, Any]) -> None:
        self._wm = working_memory or {}

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "BidWorkflowState":
        return cls(state.get("working_memory") or {})

    @property
    def phase(self) -> str:
        return str(self._wm.get("bid_phase") or "").upper().strip()

    @property
    def is_active(self) -> bool:
        return self.phase in self.PHASES

    def reset_patch(self) -> Dict[str, Any]:
        """Clés à écraser à `None` pour clore proprement ce cycle (voir
        `nodes/cognitive.py`, abandon de tunnel)."""
        return {key: None for key in self.KEYS}


class ProducerUpdateWorkflowState:
    """Contrat typé de la mini machine à états `update_phase`
    (`flows/producer/flow.py` — mise à jour prix/stock/production).

    Phases :
        SELECT   → sélection de l'item à mettre à jour (si ambigu).
        COLLECT  → collecte de la nouvelle valeur.
        CONFIRM  → confirmation demandée avant application.
    """

    PHASES = ("SELECT", "COLLECT", "CONFIRM")

    KEYS: FrozenSet[str] = frozenset(
        {"update_phase", "update_cycle_id", "update_product_id", "update_pending"}
    )

    def __init__(self, working_memory: Dict[str, Any]) -> None:
        self._wm = working_memory or {}

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "ProducerUpdateWorkflowState":
        return cls(state.get("working_memory") or {})

    @property
    def phase(self) -> str:
        return str(self._wm.get("update_phase") or "").upper().strip()

    @property
    def is_active(self) -> bool:
        return self.phase in self.PHASES

    def reset_patch(self) -> Dict[str, Any]:
        return {key: None for key in self.KEYS}


__all__ = ["BidWorkflowState", "ProducerUpdateWorkflowState"]
