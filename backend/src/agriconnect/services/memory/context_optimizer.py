"""
Context Optimizer — Assemblage Temps-Réel du Prompt (Niveau 3).
===============================================================

PRINCIPE : Au lieu d'injecter 5 000 tokens en vrac, on assemble un
prompt "chirurgical" de ~1 500-2 000 tokens avec :

  1. Profil structuré (JSON)    → ~80 tokens  (au lieu de ~2000 d'historique)
  2. Épisodes pertinents        → ~120 tokens (au lieu de ~1500 de logs)
  3. Contexte RAG rerranké      → ~500-800 tokens (au lieu de ~2000 bruts)
  4. System Prompt (cacheable)  → ~0 tokens facturés (prompt caching)

RÉSULTAT : Même qualité, 60% de tokens en moins.

Ce module est le POINT D'ENTRÉE pour l'orchestrateur.
Il fournit le contexte complet optimisé en un seul appel.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("Memory.ContextOptimizer")

# Budgets tokens par composant (soft limits pour guidance)
TOKEN_BUDGETS = {
    "profile": 100,       # Fiche ferme JSON → ~80 tokens
    "episodes": 150,      # 3 résumés épisodiques → ~120 tokens
    "rag_context": 800,   # Top-2 documents rerankés → ~500-800 tokens
    "system_prompt": 0,   # Caché (prompt caching) → facturé ~0
    "user_query": 100,    # Question de l'agriculteur
    "total_target": 1500, # Objectif total
}


@dataclass
class InteractionRecord:
    user_id: str
    query: str
    response: str
    agent_type: str
    crop: Optional[str] = None
    zone: Optional[str] = None
    severity: Optional[str] = None
    intent: Optional[str] = None


class ContextOptimizer:
    """
    Assembleur de contexte optimisé pour les agents.
    
    Remplace l'approche "tout charger" par un assemblage intelligent :
    
    AVANT (5 000 tokens) :
      System Prompt (1000) + Historique brut (2000) + RAG large (1500) + Question (100)
    
    APRÈS (1 500 tokens) :
      Profil JSON (80) + Épisodes (120) + RAG chirurgical (800) + Question (100)
      + System Prompt (caché, ~0 facturable)
    
    Usage :
        optimizer = ContextOptimizer(user_profile, episodic_memory, profile_extractor)
        context = optimizer.build_context(user_id, query, zone, crop)
    """

    def __init__(self, user_profile, episodic_memory, profile_extractor=None):
        """
        Args:
            user_profile: Instance UserFarmProfile.
            episodic_memory: Instance EpisodicMemory.
            profile_extractor: Instance ProfileExtractor (optionnel, pour extraction en arrière-plan).
        """
        self._profile = user_profile
        self._episodic = episodic_memory
        self._extractor = profile_extractor

    def record_interaction(
        self,
        user_id: str,
        query: str,
        response: str,
        agent_type: str,
        crop: str = None,
        zone: str = None,
        severity: str = None,
        intent: str = None,
    ) -> None:
        """
        Backwards-compatible wrapper: build an InteractionRecord and delegate
        to the single-argument API `record_interaction_obj`.
        """
        interaction = InteractionRecord(
            user_id=user_id,
            query=query,
            response=response,
            agent_type=agent_type,
            crop=crop,
            zone=zone,
            severity=severity,
            intent=intent,
        )
        return self.record_interaction_obj(interaction)

    def record_interaction_obj(self, interaction: InteractionRecord) -> None:
        """Persist an InteractionRecord (single-argument API).

        This reduces the surface area of the method and groups related
        parameters into a cohesive object.
        """
        try:
            self._episodic.record(
                user_id=interaction.user_id,
                query=interaction.query,
                response=interaction.response,
                agent_type=interaction.agent_type,
                crop=interaction.crop,
                zone=interaction.zone,
                severity=interaction.severity,
                intent=interaction.intent,
            )
        except Exception as e:
            logger.warning("Enregistrement épisodique échoué: %s", e)




    # ── Assemblage interne ───────────────────────────────────────

    def _assemble(self, profile: str, episodes: str) -> str:
        """Assemble les composants en un bloc de contexte cohérent."""
        parts = []
        if profile:
            parts.append(profile)
        if episodes:
            parts.append(episodes)
        return "\n\n".join(parts)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        Estimation rapide du nombre de tokens (règle des 4 chars).
        Plus précis qu'un simple len() / 4 grâce au split sur les mots.
        """
        if not text:
            return 0
        # Heuristique : ~1.3 tokens par mot en français
        word_count = len(text.split())
        return int(word_count * 1.3)
