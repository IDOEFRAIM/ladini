"""
Episodic Memory — Mémoire Moyen-Terme par Résumés (Niveau 2).
==============================================================

PRINCIPE : Après chaque interaction significative, un "Scribe" génère
un résumé de 2-3 lignes stocké en DB + Vector Store.

Quand l'agriculteur revient 5 mois plus tard, le système recherche
dans les résumés (~30 tokens chacun) au lieu de relire 500 messages
(~50 000 tokens). Gain : 99.9%.

STOCKAGE :
  - PostgreSQL : table `episodic_memories` (métadonnées + texte)
  - Vector Store : embedding du résumé pour recherche sémantique

DÉCLENCHEUR : Appelé par l'orchestrateur à la fin de persist(),
uniquement si l'interaction est "significative" (pas les salutations).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, String, Text, Float, JSON, Integer
from sqlalchemy.sql import func

from agriconnect.services.models_legacy import Base

logger = logging.getLogger("Memory.Episodic")


# ═══════════════════════════════════════════════════════════════
# MODÈLE ORM
# ═══════════════════════════════════════════════════════════════

class EpisodicMemoryModel(Base):
    """
    Un épisode = un résumé d'interaction significative.
    
    Exemples de résumés stockés :
      - "12/03: Rouille jaune sur 10ha blé Meknès. Traitement: Fongicide Opus. Suivi 10j."
      - "05/04: Vendu 5 sacs maïs 225 FCFA/kg marché Bobo. Acheteur: SONAGESS."
      - "20/06: Alerte sécheresse zone Nord. Conseillé zaï + paillage."
    """
    __tablename__ = "episodic_memories"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)

    # Résumé court (~30 tokens, optimisé pour injection dans le prompt)
    summary = Column(Text, nullable=False)

    # Catégorie pour filtrage rapide (SQL WHERE, pas besoin de vector search)
    category = Column(String, nullable=False)  # diagnosis | market | weather | formation | soil

    # Entités clés (pour recherche SQL rapide sans vector store)
    crop = Column(String)
    zone = Column(String)
    severity = Column(String)  # Pour prioriser les épisodes critiques

    # Métadonnées pour le tri et la pertinence
    relevance_score = Column(Float, default=1.0)  # Décroît avec le temps
    access_count = Column(Integer, default=0)      # Combien de fois rappelé

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "summary": self.summary,
            "category": self.category,
            "crop": self.crop,
            "zone": self.zone,
            "severity": self.severity,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# PROMPT DE SYNTHÈSE (~100 tokens)
# ═══════════════════════════════════════════════════════════════

SUMMARY_PROMPT = """Résume cette interaction agricole en UNE SEULE PHRASE (max 40 mots).
Format : "Date: Action/Diagnostic. Détails clés. Résultat/Suivi."

Interaction :
Question: {query}
Réponse: {response}
Agent: {agent}

Résumé (1 phrase) :"""

# Interactions à ignorer (pas de valeur épisodique)
SKIP_INTENTS = {"CHAT", "REJECT", "UNKNOWN"}


class EpisodicMemory:
    """
    Service de mémoire épisodique.
    
    Opérations :
      - record()     : Enregistre un nouvel épisode (après interaction)
      - recall()     : Rappelle les épisodes pertinents pour un contexte
      - to_context() : Génère le snippet injectable (~100-150 tokens)
      - decay()      : Réduit la relevance des vieux épisodes (maintenance)
    """

    def __init__(self, session_factory, llm_client=None):
        self._session_factory = session_factory
        self._llm = llm_client
        self._model = "llama-3.1-8b-instant"  # Rapide pour résumé

    # ── Enregistrement d'un épisode ──────────────────────────────

    def record(
        self,
        user_id: str,
        query: str,
        response: str,
        agent_type: str,
        crop: str = None,
        zone: str = None,
        severity: str = None,
        intent: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Enregistre un épisode si l'interaction est significative.
        
        Args:
            user_id: Identifiant agriculteur.
            query: Question de l'utilisateur.
            response: Réponse de l'agent.
            agent_type: Type d'agent (sentinelle, formation, market, etc.)
            
        Returns:
            L'épisode créé ou None si interaction non significative.
        """
        # Filtre : pas d'épisode pour les salutations
        if intent and intent.upper() in SKIP_INTENTS:
            return None

        # Filtre : réponses trop courtes = pas significatif
        if not response or len(response) < 50:
            return None

        # Générer le résumé
        summary = self._generate_summary(query, response, agent_type)
        if not summary:
            return None

        # Déterminer la catégorie
        category = self._classify_category(agent_type)

        # Persister
        with self._get_session() as session:
            episode = EpisodicMemoryModel(
                id=str(uuid.uuid4()),
                user_id=user_id,
                summary=summary,
                category=category,
                crop=crop,
                zone=zone,
                severity=severity,
            )
            session.add(episode)
            session.flush()
            logger.info("📝 Épisode enregistré pour %s: %s", user_id, summary[:60])
            return episode.to_dict()

    # ── Rappel d'épisodes pertinents ─────────────────────────────

    def recall(
        self,
        user_id: str,
        crop: str = None,
        zone: str = None,
        category: str = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Rappelle les épisodes les plus pertinents pour le contexte actuel.
        
        Stratégie de recall (SQL pur, pas de vector search pour l'instant) :
          1. Filtre par user_id (obligatoire)
          2. Filtre par crop ET/OU zone si disponible
          3. Tri par relevance_score DESC, created_at DESC
          4. Limite à N épisodes
        """
        with self._get_session() as session:
            query = session.query(EpisodicMemoryModel).filter(
                EpisodicMemoryModel.user_id == user_id
            )

            # Filtres contextuels (si on sait ce qu'on cherche)
            if crop:
                query = query.filter(EpisodicMemoryModel.crop.ilike(f"%{crop}%"))
            if zone:
                query = query.filter(EpisodicMemoryModel.zone.ilike(f"%{zone}%"))
            if category:
                query = query.filter(EpisodicMemoryModel.category == category)

            episodes = query.order_by(
                EpisodicMemoryModel.relevance_score.desc(),
                EpisodicMemoryModel.created_at.desc(),
            ).limit(limit).all()

            # Incrémenter le compteur d'accès
            for ep in episodes:
                ep.access_count = (ep.access_count or 0) + 1
            session.flush()

            return [ep.to_dict() for ep in episodes]

    # ── Génération du contexte injectable (~100-150 tokens) ──────

    def to_context(
        self,
        user_id: str,
        crop: str = None,
        zone: str = None,
        limit: int = 3,
    ) -> str:
        """
        Génère le snippet d'historique injectable dans le prompt.
        
        Exemple de sortie :
            "HISTORIQUE: 
             - 12/03: Rouille jaune 10ha blé. Traitement fongicide.
             - 05/04: Vendu 5 sacs maïs 225 FCFA/kg Bobo.
             - 20/06: Alerte sécheresse zone Nord."
        """
        episodes = self.recall(user_id, crop=crop, zone=zone, limit=limit)
        if not episodes:
            return ""

        lines = []
        for ep in episodes:
            summary = ep.get("summary", "")
            if summary:
                lines.append(f"- {summary}")

        if not lines:
            return ""

        return "HISTORIQUE RÉCENT:\n" + "\n".join(lines)

    # ── Maintenance : décroissance de pertinence ─────────────────

    def decay(self, user_id: str, decay_factor: float = 0.95) -> int:
        """
        Réduit la pertinence des vieux épisodes (appelé périodiquement).
        
        Les épisodes sévères (CRITIQUE/HAUT) décroissent plus lentement.
        
        Returns:
            Nombre d'épisodes mis à jour.
        """
        with self._get_session() as session:
            episodes = session.query(EpisodicMemoryModel).filter(
                EpisodicMemoryModel.user_id == user_id,
                EpisodicMemoryModel.relevance_score > 0.1,
            ).all()

            count = 0
            for ep in episodes:
                if ep.severity in ("CRITIQUE", "HAUT"):
                    ep.relevance_score *= (decay_factor ** 0.5)  # Décroît 2x plus lentement
                else:
                    ep.relevance_score *= decay_factor
                count += 1

            session.flush()
            logger.debug("🕐 Decay appliqué à %d épisodes pour %s", count, user_id)
            return count

    # ── Génération du résumé via LLM ─────────────────────────────

    def _generate_summary(self, query: str, response: str, agent_type: str) -> Optional[str]:
        """Génère un résumé via LLM 8B ou fallback déterministe."""
        # Essai LLM
        if self._llm:
            try:
                # Tronquer la réponse pour garder le coût bas
                response_short = response[:500] if len(response) > 500 else response
                prompt = SUMMARY_PROMPT.format(
                    query=query,
                    response=response_short,
                    agent=agent_type,
                )
                completion = self._llm.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=80,
                )
                summary = completion.choices[0].message.content.strip()
                # Nettoyage
                summary = summary.strip('"').strip("'")
                if len(summary) > 200:
                    summary = summary[:200] + "..."
                return summary
            except Exception as e:
                logger.warning("Résumé LLM échoué: %s, fallback", e)

        # Fallback déterministe (0 tokens LLM)
        date_str = datetime.now(timezone.utc).strftime("%d/%m")
        q_short = query[:80] if len(query) > 80 else query
        return f"{date_str}: [{agent_type}] {q_short}"

    def _classify_category(self, agent_type: str) -> str:
        """Mappe le type d'agent vers une catégorie épisodique."""
        mapping = {
            "sentinelle": "weather",
            "formation": "formation",
            "market": "market",
            "marketplace": "market",
            "soil": "soil",
            "plant_doctor": "diagnosis",
            "doctor": "diagnosis",
        }
        return mapping.get(agent_type, "general")

    # ── Helper session ───────────────────────────────────────────

    def _get_session(self):
        from contextlib import contextmanager

        @contextmanager
        def session_scope():
            session = self._session_factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        return session_scope()
