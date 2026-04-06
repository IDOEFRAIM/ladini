"""
User Farm Profile — Mémoire Long-Terme Structurée (Niveau 1).
=============================================================

PRINCIPE : Chaque agriculteur a une "Fiche Ferme" JSON en DB.
Au lieu de relire 500 messages pour savoir qu'il a 10 ha de maïs,
on injecte 50-100 tokens de JSON structuré dans le prompt.

STOCKAGE : Table PostgreSQL `user_farm_profiles` (colonne JSONB).
COÛT : 0 tokens LLM pour la lecture (c'est du SQL pur).
INJECTION : ~80 tokens dans le System Prompt au lieu de ~2000 d'historique.

Le profil est INCRÉMENTAL : chaque interaction peut l'enrichir
sans jamais le réécrire entièrement (MERGE, pas REPLACE).
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, String, Text, Float, JSON
from sqlalchemy.sql import func

from agriconnect.services.models_legacy import Base

logger = logging.getLogger("Memory.UserProfile")


# ═══════════════════════════════════════════════════════════════
# MODÈLE ORM
# ═══════════════════════════════════════════════════════════════

class UserFarmProfileModel(Base):
    """
    Fiche Ferme persistée en PostgreSQL.
    
    Structure JSONB `profile_data` :
    {
        "location": {"zone": "...", "commune": "...", "gps": [lat, lon]},
        "plots": [
            {
                "id": "p1",
                "crop": "Maïs",
                "area_ha": 10,
                "soil_type": "Ferrugineux",
                "planting_date": "2025-06-15",
                "status": "ACTIVE"
            }
        ],
        "livestock": [{"type": "Bovins", "count": 5}],
        "equipment": ["Charrue", "Motopompe"],
        "preferences": {
            "language": "fr",
            "level": "intermediaire",
            "notify_weather": true,
            "notify_market": true
        },
        "constraints": {
            "water_access": "puits",
            "budget_fcfa": 150000,
            "labor_hands": 3
        }
    }
    """
    __tablename__ = "user_farm_profiles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, unique=True, nullable=False, index=True)
    profile_data = Column(JSON, default=dict)
    version = Column(String, default="1")  # Versioning pour migrations futures
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "profile_data": self.profile_data or {},
            "version": self.version,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# SERVICE PROFIL
# ═══════════════════════════════════════════════════════════════

# Template vide pour un nouveau profil (coût 0, scalable)
EMPTY_PROFILE: Dict[str, Any] = {
    "location": {},
    "plots": [],
    "livestock": [],
    "equipment": [],
    "preferences": {
        "language": "fr",
        "level": "debutant",
        "notify_weather": True,
        "notify_market": True,
    },
    "constraints": {},
}


class UserFarmProfile:
    """
    Service de gestion du profil ferme structuré.
    
    Opérations :
      - get(user_id)          → Charge ou crée le profil
      - merge(user_id, patch) → Fusionne des nouvelles infos (incrémental)
      - to_context(user_id)   → Génère le snippet d'injection (~80 tokens)
      - add_plot / update_plot / remove_plot
      - set_location / set_constraints
    """

    def __init__(self, session_factory):
        """
        Args:
            session_factory: SQLAlchemy sessionmaker (partagé avec le reste).
        """
        self._session_factory = session_factory

    # ── CRUD de base ─────────────────────────────────────────────

    def get(self, user_id: str) -> Dict[str, Any]:
        """Charge le profil. Le crée s'il n'existe pas (lazy init)."""
        with self._get_session() as session:
            row = session.query(UserFarmProfileModel).filter_by(user_id=user_id).first()
            if row:
                return row.profile_data or {}
            # Première interaction → profil vide
            new_profile = UserFarmProfileModel(
                id=str(uuid.uuid4()),
                user_id=user_id,
                profile_data=dict(EMPTY_PROFILE),
            )
            session.add(new_profile)
            session.flush()
            logger.info("📋 Nouveau profil créé pour %s", user_id)
            return dict(EMPTY_PROFILE)

    def merge(self, user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fusionne un patch dans le profil existant (MERGE, pas REPLACE).
        
        Règles :
          - Les listes (plots, livestock) sont MERGÉES par ID ou APPENDÉES.
          - Les dicts (location, constraints) sont MERGÉS clé par clé.
          - Les scalaires sont REMPLACÉS.
        
        Args:
            patch: Fragment de données extraites (ex: {"location": {"zone": "Bobo"}})
        
        Returns:
            Le profil complet après fusion.
        """
        with self._get_session() as session:
            row = session.query(UserFarmProfileModel).filter_by(user_id=user_id).first()
            if not row:
                # Crée le profil d'abord
                self.get(user_id)
                row = session.query(UserFarmProfileModel).filter_by(user_id=user_id).first()

            current = row.profile_data or dict(EMPTY_PROFILE)
            merged = self._deep_merge(current, patch)
            row.profile_data = merged
            session.flush()
            logger.info("🔄 Profil mis à jour pour %s (patch: %s)", user_id, list(patch.keys()))
            return merged

    # ── Opérations spécifiques (API claire) ──────────────────────

    def add_plot(
        self,
        user_id: str,
        crop: str,
        area_ha: float = None,
        soil_type: str = None,
        planting_date: str = None,
    ) -> Dict[str, Any]:
        """Ajoute une parcelle au profil."""
        plot = {
            "id": f"p{uuid.uuid4().hex[:6]}",
            "crop": crop,
            "status": "ACTIVE",
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        if area_ha is not None:
            plot["area_ha"] = area_ha
        if soil_type:
            plot["soil_type"] = soil_type
        if planting_date:
            plot["planting_date"] = planting_date

        profile = self.get(user_id)
        plots = profile.get("plots", [])

        # Déduplique par crop + status ACTIVE
        existing = next(
            (p for p in plots if p.get("crop", "").lower() == crop.lower() and p.get("status") == "ACTIVE"),
            None,
        )
        if existing:
            # Mise à jour de la parcelle existante
            if area_ha is not None:
                existing["area_ha"] = area_ha
            if soil_type:
                existing["soil_type"] = soil_type
            if planting_date:
                existing["planting_date"] = planting_date
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            logger.info("🌾 Parcelle %s mise à jour pour %s", crop, user_id)
        else:
            plots.append(plot)
            logger.info("🌾 Nouvelle parcelle %s ajoutée pour %s", crop, user_id)

        return self.merge(user_id, {"plots": plots})

    def set_location(self, user_id: str, zone: str = None, commune: str = None, gps: list = None) -> Dict[str, Any]:
        """Met à jour la localisation."""
        loc = {}
        if zone:
            loc["zone"] = zone
        if commune:
            loc["commune"] = commune
        if gps:
            loc["gps"] = gps
        return self.merge(user_id, {"location": loc})

    def set_constraints(self, user_id: str, **kwargs) -> Dict[str, Any]:
        """Met à jour les contraintes (budget, main d'œuvre, eau, etc.)."""
        return self.merge(user_id, {"constraints": kwargs})

    # ── Génération du contexte injectable (~80 tokens) ───────────

    def to_context(self, user_id: str) -> str:
        """
        Transforme le profil en snippet injectable dans le System Prompt.
        
        Objectif : ~80 tokens max, lisible par le LLM.
        
        Exemple de sortie :
            "PROFIL: Zone=Bobo, Parcelles: 10ha Maïs (semé 15/06, actif), 
             3ha Sorgho (récolté). Contraintes: budget 150k FCFA, 1 puits."
        """
        profile = self.get(user_id)
        if not profile or profile == EMPTY_PROFILE:
            return ""

        parts = []

        # Localisation
        loc = profile.get("location", {})
        if loc:
            loc_str = ", ".join(f"{v}" for v in [loc.get("zone"), loc.get("commune")] if v)
            if loc_str:
                parts.append(f"Zone={loc_str}")

        # Parcelles (le plus important pour les agents)
        plots = profile.get("plots", [])
        active_plots = [p for p in plots if p.get("status") == "ACTIVE"]
        if active_plots:
            plot_strs = []
            for p in active_plots:
                s = p.get("crop", "?")
                if p.get("area_ha"):
                    s = f"{p['area_ha']}ha {s}"
                if p.get("planting_date"):
                    s += f" (semé {p['planting_date']})"
                if p.get("soil_type"):
                    s += f" sol:{p['soil_type']}"
                plot_strs.append(s)
            parts.append(f"Parcelles: {', '.join(plot_strs)}")

        # Bétail
        livestock = profile.get("livestock", [])
        if livestock:
            ls = ", ".join(f"{l.get('count', '?')} {l.get('type', '?')}" for l in livestock)
            parts.append(f"Bétail: {ls}")

        # Équipements
        equip = profile.get("equipment", [])
        if equip:
            parts.append(f"Équipement: {', '.join(equip[:5])}")

        # Contraintes
        constraints = profile.get("constraints", {})
        if constraints:
            c_parts = []
            if constraints.get("budget_fcfa"):
                c_parts.append(f"budget {constraints['budget_fcfa']:,.0f} FCFA")
            if constraints.get("water_access"):
                c_parts.append(f"eau: {constraints['water_access']}")
            if constraints.get("labor_hands"):
                c_parts.append(f"{constraints['labor_hands']} ouvriers")
            if c_parts:
                parts.append(f"Contraintes: {', '.join(c_parts)}")

        if not parts:
            return ""

        return "PROFIL AGRICULTEUR: " + ". ".join(parts) + "."

    # ── Helpers privés ───────────────────────────────────────────

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

    @staticmethod
    def _deep_merge(base: Dict, patch: Dict) -> Dict:
        """
        Fusion profonde : les dicts sont mergés, les listes sont
        mergées par ID ou concaténées, les scalaires sont remplacés.
        """
        result = dict(base)
        for key, value in patch.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = UserFarmProfile._deep_merge(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                # Merge par ID si les éléments ont un "id"
                if value and isinstance(value[0], dict) and "id" in value[0]:
                    existing_ids = {item.get("id"): i for i, item in enumerate(result[key]) if isinstance(item, dict)}
                    for new_item in value:
                        nid = new_item.get("id")
                        if nid and nid in existing_ids:
                            result[key][existing_ids[nid]] = new_item
                        else:
                            result[key].append(new_item)
                else:
                    result[key] = value
            else:
                result[key] = value
        return result
