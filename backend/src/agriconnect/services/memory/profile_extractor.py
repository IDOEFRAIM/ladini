"""
Profile Extractor — Extraction automatique d'entités agricoles (Niveau 1b).
===========================================================================

PRINCIPE : À chaque message, un petit modèle RAPIDE et PAS CHER
(llama-3.1-8b-instant) analyse la conversation pour extraire des
faits structurés et mettre à jour la Fiche Ferme.

COÛT : ~200 tokens/appel (modèle 8B, pas 70B). Rentabilisé dès le
2e message car on n'a plus besoin de charger l'historique.

Ce module tourne EN ARRIÈRE-PLAN (async) : il ne bloque pas la réponse.
L'utilisateur reçoit sa réponse normalement pendant que le profil
se met à jour en parallèle.
"""

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger("Memory.ProfileExtractor")

# ─── Prompt d'extraction (~150 tokens d'input) ───────────────────
# Optimisé pour être COURT et PRÉCIS → modèle 8B suffit.

EXTRACTION_PROMPT = """Extrais les FAITS AGRICOLES de ce message. Réponds UNIQUEMENT en JSON.

Catégories possibles :
- plot: {crop, area_ha, soil_type, planting_date, status}
- location: {zone, commune, gps}
- livestock: {type, count}
- equipment: [items]
- constraint: {water_access, budget_fcfa, labor_hands}
- preference: {language, level}

Règles :
- N'extrais QUE les faits explicitement mentionnés. N'invente RIEN.
- area_ha : convertis toutes les surfaces en hectares (1 ha = 10 000 m²).
- budget : en FCFA.
- Si aucun fait agricole → retourne {"facts": []}

Message: {message}

JSON:"""

# Catégories de faits extraits → méthodes de merge correspondantes
FACT_HANDLERS = {
    "plot": "_handle_plot",
    "location": "_handle_location",
    "livestock": "_handle_livestock",
    "equipment": "_handle_equipment",
    "constraint": "_handle_constraint",
    "preference": "_handle_preference",
}


class ProfileExtractor:
    """
    Extracteur d'entités agricoles à partir des messages.
    
    Pipeline :
      1. Message brut → LLM 8B (extraction JSON)
      2. JSON validé → UserFarmProfile.merge()
      3. Profil mis à jour en DB (incrémental)
    
    Usage :
        extractor = ProfileExtractor(llm_client, user_profile_service)
        await extractor.extract_and_update(user_id, message)
    """

    def __init__(self, llm_client, user_profile):
        """
        Args:
            llm_client: SDK Groq/OpenAI (pour le modèle 8B rapide).
            user_profile: Instance de UserFarmProfile.
        """
        self._llm = llm_client
        self._profile = user_profile
        self._model = "llama-3.1-8b-instant"  # Rapide, pas cher

    def extract_and_update(self, user_id: str, message: str) -> Dict[str, Any]:
        """
        Point d'entrée principal : extrait les faits et met à jour le profil.
        
        Args:
            user_id: Identifiant de l'agriculteur.
            message: Message brut de l'utilisateur.
        
        Returns:
            {"extracted": [...], "profile_updated": bool}
        """
        if not message or len(message.strip()) < 5:
            return {"extracted": [], "profile_updated": False}

        # 1. Extraction via LLM léger
        facts = self._extract_facts(message)
        if not facts:
            return {"extracted": [], "profile_updated": False}

        # 2. Application des faits au profil
        updated = False
        for fact in facts:
            try:
                fact_type = fact.get("type", "")
                handler_name = FACT_HANDLERS.get(fact_type)
                if handler_name:
                    handler = getattr(self, handler_name)
                    handler(user_id, fact.get("data", {}))
                    updated = True
            except Exception as e:
                logger.warning("Erreur application fact %s: %s", fact, e)

        if updated:
            logger.info("✅ Profil %s mis à jour avec %d faits", user_id, len(facts))

        return {"extracted": facts, "profile_updated": updated}

    # ── Extraction LLM ───────────────────────────────────────────

    def _extract_facts(self, message: str) -> List[Dict[str, Any]]:
        """Appelle le LLM 8B pour extraire les faits structurés."""
        if not self._llm:
            return []

        try:
            prompt = EXTRACTION_PROMPT.format(message=message)
            completion = self._llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,  # Déterministe pour extraction
                max_tokens=300,   # JSON court
            )
            raw = completion.choices[0].message.content.strip()
            return self._parse_facts(raw)
        except Exception as e:
            logger.warning("Extraction LLM échouée: %s", e)
            return []

    def _parse_facts(self, raw_json: str) -> List[Dict[str, Any]]:
        """Parse et valide le JSON retourné par le LLM."""
        try:
            # Nettoyer les artefacts markdown
            if "```" in raw_json:
                raw_json = raw_json.split("```")[1]
                if raw_json.startswith("json"):
                    raw_json = raw_json[4:]
            
            data = json.loads(raw_json)
            
            # Format attendu : {"facts": [...]}
            if isinstance(data, dict):
                facts = data.get("facts", [])
                if isinstance(facts, list):
                    return [f for f in facts if isinstance(f, dict) and "type" in f]
                # Support format plat (un seul fait)
                if "type" in data:
                    return [data]
            
            return []
        except (json.JSONDecodeError, KeyError) as e:
            logger.debug("JSON invalide du LLM: %s", e)
            return []

    # ── Handlers par type de fait ────────────────────────────────

    def _handle_plot(self, user_id: str, data: Dict[str, Any]) -> None:
        """Ajoute ou met à jour une parcelle."""
        crop = data.get("crop")
        if not crop:
            return
        self._profile.add_plot(
            user_id=user_id,
            crop=crop,
            area_ha=data.get("area_ha"),
            soil_type=data.get("soil_type"),
            planting_date=data.get("planting_date"),
        )

    def _handle_location(self, user_id: str, data: Dict[str, Any]) -> None:
        """Met à jour la localisation."""
        self._profile.set_location(
            user_id=user_id,
            zone=data.get("zone"),
            commune=data.get("commune"),
            gps=data.get("gps"),
        )

    def _handle_livestock(self, user_id: str, data: Dict[str, Any]) -> None:
        """Ajoute du bétail."""
        if not data.get("type"):
            return
        profile = self._profile.get(user_id)
        livestock = profile.get("livestock", [])
        # Mise à jour si même type
        existing = next((l for l in livestock if l.get("type", "").lower() == data["type"].lower()), None)
        if existing:
            existing["count"] = data.get("count", existing.get("count"))
        else:
            livestock.append({"type": data["type"], "count": data.get("count")})
        self._profile.merge(user_id, {"livestock": livestock})

    def _handle_equipment(self, user_id: str, data: Dict[str, Any]) -> None:
        """Ajoute des équipements (dédupliqués)."""
        items = data if isinstance(data, list) else data.get("items", [])
        if not items:
            return
        profile = self._profile.get(user_id)
        current_equip = set(e.lower() for e in profile.get("equipment", []))
        new_equip = list(profile.get("equipment", []))
        for item in items:
            if isinstance(item, str) and item.lower() not in current_equip:
                new_equip.append(item)
                current_equip.add(item.lower())
        self._profile.merge(user_id, {"equipment": new_equip})

    def _handle_constraint(self, user_id: str, data: Dict[str, Any]) -> None:
        """Met à jour les contraintes."""
        clean = {k: v for k, v in data.items() if v is not None}
        if clean:
            self._profile.set_constraints(user_id, **clean)

    def _handle_preference(self, user_id: str, data: Dict[str, Any]) -> None:
        """Met à jour les préférences."""
        clean = {k: v for k, v in data.items() if v is not None}
        if clean:
            self._profile.merge(user_id, {"preferences": clean})
