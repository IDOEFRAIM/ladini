"""Menu Contracts — Contrats d'interface UI standardisés.

Définit les structures de données intermédiaires que les flows métier
(buyer, producer) retournent au lieu de construire manuellement des
dictionnaires ``ag_ui_component``. Le nœud ``nodes/ui_engine.py``
consomme ces objets et génère le composant AG-UI final + le
``available_mapping`` de manière uniforme.

Règle d'or :
  Les fichiers ``flows/buyer/flow.py`` et ``flows/producer/flow.py``
  ne doivent JAMAIS construire de ``ag_ui_component`` eux-mêmes.
  Ils retournent un ``MenuRequest`` (ou ``None`` si pas de menu).

Dépendances : AUCUNE import vers flows/ ou core/ — ce module est une
feuille pure du graphe de dépendances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class MenuOption:
    """Une option unique dans un menu de sélection AG-UI.

    Attributes:
        index: Clé numérique ou identifiant court (affiché à l'utilisateur).
        label: Texte descriptif de l'option.
        value: Identifiant métier associé (UUID, slug…). Si ``None``,
               ``index`` est utilisé comme valeur par défaut.
    """

    index: str
    label: str
    value: Optional[str] = None

    def effective_value(self) -> str:
        """Retourne ``value`` ou ``index`` en fallback."""
        return self.value if self.value is not None else self.index


@dataclass(frozen=True, slots=True)
class MenuRequest:
    """Requête de menu standardisée émise par un flow métier.

    Le ``ui_engine`` la transforme en :
      - ``ag_ui_component`` (dict AG-UI ``ListMenu``)
      - ``available_mapping`` (dict index → valeur métier)
      - ``expected_candidates`` (list[str])

    Attributes:
        title: Titre affiché en tête du menu.
        options: Liste ordonnée des options.
        kind: Catégorie sémantique du menu (``"cart"``, ``"auction"``,
              ``"bid"``, ``"stock"``, ``"preorder_action"``,
              ``"negotiation"``, etc.). Propagé dans le metadata AG-UI
              et dans ``working_memory.available_mapping_kind``. Contrat
              (2026-09-09, audit ui_engine) : ce champ identifie le TYPE du
              mapping actuellement actif — il ne doit jamais survivre à un
              menu DIFFÉRENT avec une ancienne valeur. ``ui_engine`` le
              réécrit intégralement à CHAQUE menu rendu (jamais de fusion
              partielle) ; un flow qui abandonne un menu sans en montrer un
              nouveau reste responsable de nettoyer explicitement toute
              valeur stale (voir ``flows/buyer/cart.py`` pour l'exemple
              existant — hors périmètre de cet audit).
        metadata: Paires clé-valeur supplémentaires injectées dans le
                  metadata du composant AG-UI (ex: ``preorder_id``).
        preformatted_text: Texte WhatsApp déjà formaté à utiliser comme
                          ``final_response`` au lieu de la génération auto.
                          Si ``None``, le ``ui_engine`` génère le texte.
    """

    title: str
    options: List[MenuOption]
    kind: str = "generic"
    metadata: Dict[str, Any] = field(default_factory=dict)
    preformatted_text: Optional[str] = None

    def __post_init__(self) -> None:
        """Validation à la construction (2026-09-09, audit ui_engine, mandat
        §25) : un `MenuRequest` implique TOUJOURS une sélection valide — la
        validation appartient ICI, pas dispersée dans `ui_engine`. Rejette
        UNIQUEMENT ce qui produirait un mapping structurellement AMBIGU
        (index dupliqué/vide, valeur métier vide) — ne rejette PAS les
        labels dupliqués (légitimes : ex. le même nom de produit chez deux
        vendeurs différents, distingués par leur seul index)."""
        if not self.options:
            raise ValueError(
                "MenuRequest sans options : rien à sélectionner (0 option)"
            )
        seen_indices: set[str] = set()
        for opt in self.options:
            if not str(opt.index or "").strip():
                raise ValueError(
                    f"MenuRequest option avec un index vide (label={opt.label!r})"
                )
            if opt.index in seen_indices:
                raise ValueError(
                    f"MenuRequest avec un index dupliqué {opt.index!r} — "
                    "mapping ambigu (deux options se disputeraient la même "
                    "sélection numérique)"
                )
            seen_indices.add(opt.index)
            if opt.value is not None and not str(opt.value).strip():
                raise ValueError(
                    f"MenuRequest option index={opt.index!r} a une valeur "
                    "métier explicitement vide"
                )

    # ------------------------------------------------------------------
    # Helpers de construction rapide
    # ------------------------------------------------------------------

    @classmethod
    def from_pairs(
        cls,
        title: str,
        pairs: List[tuple[str, str]],
        *,
        kind: str = "generic",
        metadata: Optional[Dict[str, Any]] = None,
        preformatted_text: Optional[str] = None,
    ) -> "MenuRequest":
        """Construit un ``MenuRequest`` à partir de paires ``(index, label)``."""
        options = [MenuOption(index=idx, label=lbl) for idx, lbl in pairs]
        return cls(
            title=title,
            options=options,
            kind=kind,
            metadata=metadata or {},
            preformatted_text=preformatted_text,
        )

    @classmethod
    def from_mapping(
        cls,
        title: str,
        mapping: Dict[str, str],
        labels: List[str],
        *,
        kind: str = "generic",
        metadata: Optional[Dict[str, Any]] = None,
        preformatted_text: Optional[str] = None,
    ) -> "MenuRequest":
        """Construit un ``MenuRequest`` à partir d'un mapping index→valeur
        et d'une liste de labels correspondants.
        """
        items = list(mapping.items())
        options = [
            MenuOption(
                index=idx, label=labels[i] if i < len(labels) else idx, value=val
            )
            for i, (idx, val) in enumerate(items)
        ]
        return cls(
            title=title,
            options=options,
            kind=kind,
            metadata=metadata or {},
            preformatted_text=preformatted_text,
        )

    # ------------------------------------------------------------------
    # Sérialisation vers les structures State attendues
    # ------------------------------------------------------------------

    def to_mapping(self) -> Dict[str, str]:
        """Retourne le ``available_mapping`` (index → valeur métier)."""
        return {opt.index: opt.effective_value() for opt in self.options}

    def to_candidates(self) -> List[str]:
        """Retourne la liste ``expected_candidates``."""
        return [opt.label for opt in self.options]

    def to_working_memory_patch(self) -> Dict[str, Any]:
        """Retourne le fragment de ``working_memory`` à merger."""
        return {"available_mapping_kind": self.kind}


@dataclass(slots=True)
class DomainResult:
    """Résultat unifié retourné par un flow de domaine au DomainRouter.

    Attributes:
        state_patch: Deltas à fusionner dans ``MarketAgentState``.
        pending_menu: Menu optionnel ; le ``ui_engine`` le transformera
                      en ``ag_ui_component`` + ``available_mapping``.
    """

    state_patch: Dict[str, Any] = field(default_factory=dict)
    pending_menu: Optional[MenuRequest] = None


__all__ = [
    "MenuOption",
    "MenuRequest",
    "DomainResult",
]
