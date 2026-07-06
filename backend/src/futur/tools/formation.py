import inspect
import json
import logging
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from futur.utils.units import AgriUnitConverter

from .formation_advisor import FormationAdvisor

logger = logging.getLogger("FormationTool")

REFERENCE_PLOT_M2 = 100.0
REFERENCE_PLOT_LABEL = "100 m² (10m x 10m)"
REFERENCE_PLOT_HA = REFERENCE_PLOT_M2 / 10000.0


class FormationTool:
    def __init__(self, llm, json_refs_path: str = "agronomic_refs.json", model_planner: str = "llama-3.3-70b-versatile"):
        self.llm = llm
        self.model_planner = model_planner
        self.json_refs_path = json_refs_path
        self.advisor = FormationAdvisor()
        self.agri_knowledge = self._load_json_refs()

    def _load_json_refs(self) -> Dict[str, Any]:
        """Charge le fichier JSON des références agronomiques."""
        if os.path.exists(self.json_refs_path):
            with open(self.json_refs_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        return {"CULTURES": {}}

    async def analyze_request(self, query: str) -> Dict[str, Any]:
        """Analyse la requête pour identifier l'intention et la culture."""
        if self.llm is None:
            return {"intent": "FORMATION", "identified_crop_type": "UNKNOWN"}

        available_types = list(self.agri_knowledge.get("CULTURES", {}).keys())
        prompt = (
            f"Tu es l'ingénieur pédagogique d'AgriConnect. Analyse : '{query}'\n\n"
            "Extraits en JSON :\n"
            "1. intent : FORMATION, URGENCE ou CONSEIL.\n"
            f"2. identified_crop_type : Choisis strictement parmi {available_types} ou 'UNKNOWN'.\n"
            "3. focus_topics : Liste de mots-clés.\n"
            "4. safety_flags : Risques (pesticides, etc.).\n"
            "5. urgency : NORMAL, HAUTE, CRITIQUE.\n"
        )

        try:
            maybe_completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            completion = await maybe_completion if inspect.isawaitable(maybe_completion) else maybe_completion
            return json.loads(completion.choices[0].message.content)
        except Exception as exc:
            logger.warning("FormationTool.analyze_request fallback: %s", exc)
            return {"intent": "FORMATION", "identified_crop_type": "UNKNOWN"}

    def get_crop_characteristics(self, crop_type: str) -> Dict[str, Any]:
        """Retourne la fiche culture depuis le JSON embarqué."""
        if not crop_type or crop_type == "UNKNOWN":
            return {}

        cultures = self.agri_knowledge.get("CULTURES", {}) or {}
        specs = cultures.get(crop_type, {})

        if not specs and isinstance(crop_type, str):
            def _norm(value: str) -> str:
                value = value.strip()
                value = "".join(
                    ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
                )
                return value.casefold()

            target = _norm(crop_type)
            for key, value in cultures.items():
                if isinstance(key, str) and _norm(key) == target:
                    specs = value or {}
                    break

        if not specs:
            logger.warning("Type %s identifié mais absent du JSON", crop_type)

        return specs

    @staticmethod
    def _context_is_relevant(context: str) -> bool:
        if not context:
            return False
        lowered = context.strip().lower()
        if "aucun document" in lowered or "no document" in lowered:
            return False
        return len(lowered) > 80

    def _guess_crop_from_query(self, query: str) -> Optional[str]:
        if not query:
            return None
        cultures = self.agri_knowledge.get("CULTURES", {}) or {}
        normalized_query = query.lower()
        for name in cultures.keys():
            if isinstance(name, str) and name.lower() in normalized_query:
                return name
        return None

    @staticmethod
    def _extract_numeric_value(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", ".")
            match = re.search(r"[-+]?[0-9]*\.?[0-9]+", cleaned)
            if match:
                try:
                    return float(match.group())
                except ValueError:
                    return None
        return None

    def _translate_fertilizer_plan(self, fert_plan: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
        instructions: List[str] = []
        breakdown: List[Dict[str, Any]] = []
        for step in fert_plan or []:
            per_ha = self._extract_numeric_value(step.get("dose_ha"))
            if per_ha is None or per_ha <= 0:
                continue

            per_plot = round(per_ha * REFERENCE_PLOT_HA, 2)
            local_equiv = AgriUnitConverter.kg_to_local_units(per_plot)
            stage = step.get("moment", "Application")
            product = step.get("produit", "Intrant")
            instructions.append(
                f"{stage} : Apporter {local_equiv} de {product} pour chaque portion de {REFERENCE_PLOT_LABEL} "
                f"(soit l'équivalent de {per_ha:.2f} kg/ha pour vos achats)."
            )
            breakdown.append(
                {
                    "stage": stage,
                    "product": product,
                    "kg_per_ha": round(per_ha, 2),
                    "kg_per_100m2": per_plot,
                    "local_equivalent": local_equiv,
                    "mode": step.get("mode", "Épandage"),
                }
            )

        return instructions, breakdown

    def _build_final_message(
        self,
        meta: Dict[str, Any],
        semis: Dict[str, Any],
        eau: Dict[str, Any],
        phenologie: List[Dict[str, Any]],
        fertilizer_lines: List[str],
        protection: Dict[str, Any],
        guard_fous: List[str],
        diagnostic_questions: List[str],
        rag_relevant: bool,
    ) -> str:
        culture = meta.get("culture") or "Culture non renseignée"
        variety = meta.get("variete_recommandee") or culture
        zone = meta.get("zone") or "Zone inconnue"
        cycle = meta.get("cycle", "Cycle inconnu")
        scientific_name = meta.get("scientific_name", "")
        temperatures = meta.get("temperatures", "")
        thermal_need = meta.get("besoin_thermique", "")

        lines: List[str] = [
            "💡 Mon conseil de formateur : N'ayant pas la taille exacte de votre champ, j'ai préparé toutes les mesures pour une surface standard de 100 m² (une grande parcelle de 10m x 10m). Il vous suffira de multiplier ou diviser ces mesures selon la taille réelle de votre espace !",
            "",
            f"🌱 Culture ciblée : {culture} (Variété : {variety}) — Zone : {zone}",
            f"🧪 Cycle : {cycle} | Nom scientifique : {scientific_name}",
            f"🌡️ Température de référence : {temperatures} | Besoin thermique : {thermal_need}",
        ]

        if not rag_relevant:
            lines.append(
                "📚 Aucun document RAG exploitable aujourd'hui, je m'appuie donc exclusivement sur la fiche technique calibrée INERA."
            )

        lines.extend(
            [
                "",
                "1️⃣ SEMIS & IMPLANTATION",
                f"- Écartements : {semis.get('ecartement', 'Non précisé')}",
                f"- Densité cible : {semis.get('densite_theorique', 'N/A')}",
                f"- Profondeur racinaire : {semis.get('profondeur_racinaire', 'N/A')}",
                f"- Graines par poquet : {semis.get('graines_poquet', 'N/A')}",
            ]
        )

        if phenologie:
            stages = []
            for stage in phenologie:
                bbch = stage.get("bbch") or "?"
                stage_name = stage.get("name") or "Stade"
                stage_gdd = stage.get("gdd_target") or "?"
                stages.append(f"BBCH {bbch} – {stage_name} (GDD cible : {stage_gdd})")
            lines.append(f"- Repères phénologiques : {', '.join(stages)}")

        lines.extend(
            [
                "",
                "2️⃣ EAU & CLIMAT",
                f"- Besoin global : {eau.get('besoin_global', 'N/A')}",
                f"- Stratégie d'irrigation : {eau.get('strategie', 'N/A')}",
                "- Périodes critiques : "
                + (
                    ", ".join(eau.get("stades_critiques_secheresse", []))
                    or "Surveiller les phases de floraison et remplissage."
                ),
            ]
        )

        lines.append("")
        lines.append("3️⃣ FERTILISATION EN UNITÉS LOCALES")
        if fertilizer_lines:
            lines.extend(f"- {line}" for line in fertilizer_lines)
        else:
            lines.append("- Aucun plan d'engrais calibré n'est enregistré pour cette culture.")

        if guard_fous:
            lines.append("")
            lines.append("⚠️ Gardes-fous agronomiques")
            lines.extend(f"- {alert}" for alert in guard_fous)

        pests = protection.get("ravageurs") or []
        diseases = protection.get("maladies") or []
        lines.extend(
            [
                "",
                "4️⃣ SURVEILLANCE & PROTECTION",
                "- Ravageurs majeurs : " + (", ".join(pests) if pests else "non documentés"),
                "- Maladies à surveiller : " + (", ".join(diseases) if diseases else "non documentées"),
            ]
        )

        lines.append("")
        lines.append("✅ Questions de pré-vol à vérifier :")
        questions = diagnostic_questions or []
        if questions:
            lines.extend(f"- [ ] {q}" for q in questions)
        else:
            lines.append("- [ ] Confirmez l'accès à l'eau et la disponibilité des intrants avant de démarrer.")

        return "\n".join(lines).strip()

    async def run_full_diagnostic(
        self,
        query: str,
        crop_type: Optional[str] = None,
        zone: Optional[str] = None,
        rag_context: Optional[str] = None,
        rag_sources: Optional[List[Dict[str, Any]]] = None,
        area_ha: Optional[float] = None,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        sanitized_query = (query or "").strip()
        rag_context_text = (rag_context or "").strip()
        rag_relevant = self._context_is_relevant(rag_context_text)
        resolved_crop = (crop_type or "").strip()
        analysis: Dict[str, Any] = {}

        if not resolved_crop or resolved_crop.upper() == "UNKNOWN":
            try:
                analysis = await self.analyze_request(sanitized_query)
            except Exception as exc:
                logger.warning("FormationTool.analyze_request failed inside run_full_diagnostic: %s", exc)
                warnings.append(f"Analyse automatique indisponible : {exc}")
                analysis = {}
            resolved_crop = (analysis.get("identified_crop_type") or "").strip()

        if not resolved_crop:
            guess = self._guess_crop_from_query(sanitized_query)
            if guess:
                resolved_crop = guess

        if not resolved_crop:
            message = (
                "💡 Mon conseil de formateur : N'ayant pas la taille exacte de votre champ, j'ai préparé toutes les mesures pour une surface standard de 100 m² (une grande parcelle de 10m x 10m). "
                "Cependant, je dois d'abord connaître la culture exacte pour calibrer les doses. Pouvez-vous me préciser le nom de la culture ?"
            )
            return {
                "status": "MISSING_DATA",
                "final_response": message,
                "answer_draft": message,
                "degraded_mode": True,
                "warnings": warnings + ["Culture non identifiée."],
                "rag_context_used": False,
                "rag_context": rag_context_text,
                "rag_sources": rag_sources or [],
            }

        resolved_zone = (zone or analysis.get("zone") or "Centre").strip() or "Centre"
        area_for_query = 1.0  # Spécification : toujours interroger l'advisor sur 1 ha

        try:
            canvas = await self.advisor.generate_technical_diagnosis(
                crop=resolved_crop,
                zone=resolved_zone,
                area_ha=area_for_query,
            )
        except Exception as exc:
            logger.error("FormationAdvisor indisponible: %s", exc)
            message = (
                "💡 Mon conseil de formateur : N'ayant pas la taille exacte de votre champ, j'ai préparé toutes les mesures pour une surface standard de 100 m² (une grande parcelle de 10m x 10m). "
                "Le moteur technique INERA est momentanément indisponible. Réessayez dans quelques minutes."
            )
            return {
                "status": "UNAVAILABLE",
                "final_response": message,
                "answer_draft": message,
                "degraded_mode": True,
                "warnings": warnings + ["FormationAdvisor indisponible."],
                "rag_context_used": False,
                "rag_context": rag_context_text,
                "rag_sources": rag_sources or [],
            }

        if canvas.get("error"):
            message = canvas.get("message") or "Fiche technique indisponible."
            final_text = (
                "💡 Mon conseil de formateur : N'ayant pas la taille exacte de votre champ, j'ai préparé toutes les mesures pour une surface standard de 100 m² (une grande parcelle de 10m x 10m). "
                f"{message}"
            )
            return {
                "status": "UNAVAILABLE",
                "final_response": final_text,
                "answer_draft": final_text,
                "degraded_mode": True,
                "warnings": warnings + [message],
                "rag_context_used": False,
                "rag_context": rag_context_text,
                "rag_sources": rag_sources or [],
            }

        fertilizer_lines, fertilizer_breakdown = self._translate_fertilizer_plan(canvas.get("fertilisation_detailee") or [])
        final_message = self._build_final_message(
            meta=canvas.get("meta", {}),
            semis=canvas.get("semis", {}),
            eau=canvas.get("eau_climat", {}),
            phenologie=canvas.get("phenologie", []),
            fertilizer_lines=fertilizer_lines,
            protection=canvas.get("protection_critique", {}),
            guard_fous=canvas.get("gardes_fous", []),
            diagnostic_questions=canvas.get("diagnostic_questions", []),
            rag_relevant=rag_relevant,
        )

        return {
            "status": "OK",
            "identified_crop": resolved_crop,
            "zone": resolved_zone,
            "area_ha_used": area_for_query,
            "technical_canvas": canvas,
            "fertilizer_instructions": fertilizer_lines,
            "fertilizer_breakdown": fertilizer_breakdown,
            "final_response": final_message,
            "answer_draft": final_message,
            "degraded_mode": False,
            "rag_context_used": rag_relevant,
            "rag_context": rag_context_text,
            "rag_sources": rag_sources or [],
            "warnings": warnings,
        }