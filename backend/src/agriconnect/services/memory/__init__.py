"""
Memory Module — Architecture mémoire 3 niveaux pour AgriBot-AI.

Niveaux :
  1. UserFarmProfile  : Profil structuré (JSON) → mémoire long-terme (SQL)
  2. EpisodicMemory   : Résumés épisodiques → mémoire moyen-terme (Vector Store)
  3. ContextOptimizer : Assemblage temps-réel → prompt chirurgical (~1500 tokens)

Gains attendus : -50% tokens/message, qualité supérieure (data structurée).
"""

from .context_optimizer import ContextOptimizer
from .episodic_memory import EpisodicMemory
from .profile_extractor import ProfileExtractor
from .user_profile import UserFarmProfile

__all__ = [
    "UserFarmProfile",
    "ProfileExtractor",
    "EpisodicMemory",
    "ContextOptimizer",
]
