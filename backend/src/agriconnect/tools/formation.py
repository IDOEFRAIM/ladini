from typing import Any, List, Dict, Optional
import json
import logging
import re
from .formation_advisor import FormationAdvisor

logger = logging.getLogger("FormationCoachTool")

class FormationTool:
    def __init__(self,llm,model_planner="llama-3.3-70b-versatile",model_answer="llama-3.3-70b-versatile"    ):
        self.llm = llm
        self.model_planner = model_planner
        self.model_answer = model_answer
        self._db_url = None
        self._psycopg2 = None
        self.advisor = FormationAdvisor()

    def get_technical_advice(self, crop: str, zone: str, area_ha: float = 1.0) -> str:
        """
        Génère une réponse structurée "Canvas Technique" (V2) pour une culture donnée.
        Utilise FormationAdvisor pour éviter les réponses génériques.
        """
        try:
            canvas = self.advisor.generate_technical_diagnosis(crop, zone, area_ha)
            # Si erreur (culture inconnue), on retourne le message d'erreur
            if canvas.get("error"):
                return canvas["message"]
            
            # Sinon on formate en Markdown
            return self.advisor.format_as_markdown(canvas)
        except Exception as e:
            logger.error(f"Erreur lors de la génération du conseil technique : {e}")
            return "Désolé, une erreur technique m'empêche de récupérer la fiche INERA."

    def configure_db(self, db_url: Optional[str] = None, psycopg2_module: Any = None) -> None:
        self._db_url = db_url
        self._psycopg2 = psycopg2_module

    def _ensure_db(self) -> None:
        if self._db_url and self._psycopg2:
            return
        try:
            from agriconnect.core.settings import settings
            import psycopg2

            self._db_url = settings.DATABASE_URL
            self._psycopg2 = psycopg2
        except Exception:
            self._db_url = None
            self._psycopg2 = None

    def _extract_json_block(self, text: str) -> Dict[str, Any]:
        matches = re.findall(r"\{[\s\S]*?\}", text)
        for match in matches:
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue
        return json.loads(text)

    def _plan_retrieval(self, query: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        fallback = {
            "optimized_query": query,
            "modules": [],
            "prerequisites": [],
            "reasoning": "",
            "warnings": [],
        }

        if not self.llm:
            fallback["warnings"].append("LLM indisponible pour planifier la recherche.")
            return fallback

        profile_text = self._format_profile(profile)
        planner_prompt = (
            "Tu es l'orchestrateur pédagogique d'AgriConnect. "
            "Analyse la question suivante et prépare une recherche RAG.\n"
            f"Profil apprenant : {profile_text}\n"
            f"Question : {query}\n\n"
            'Réponds en JSON avec : {"optimized_query": "...", "modules": ["..."], '
            '"prerequisites": ["..."], "reasoning": "..."}'
        )

        try:
            completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": planner_prompt}],
                temperature=0.2,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content
            if not content:
                raise ValueError("Réponse vide du planificateur.")
            plan = json.loads(content)
            return {
                "optimized_query": plan.get("optimized_query") or query,
                "modules": plan.get("modules", []),
                "prerequisites": plan.get("prerequisites", []),
                "reasoning": plan.get("reasoning", ""),
                "warnings": [],
            }
        except Exception as exc:
            logger.warning("Planification RAG impossible : %s", exc)
            fallback["warnings"].append("Planification RAG automatique indisponible.")
            return fallback

    def _build_context(self, nodes: List[Any]) -> str:
        sections = []
        for idx, node in enumerate(nodes, start=1):
            metadata = node.node.metadata or {}
            label = metadata.get("title") or metadata.get("filename") or f"Source {idx}"
            chunk = node.node.get_content().strip()
            sections.append(f"[Source {idx} | {label}]\n{chunk}")
        return "\n\n".join(sections)

    def _serialize_sources(self, nodes: List[Any]) -> List[Dict[str, Any]]:
        payload: List[Dict[str, Any]] = []
        for idx, node in enumerate(nodes, start=1):
            metadata = node.node.metadata or {}
            payload.append(
                {
                    "index": idx,
                    "title": metadata.get("title"),
                    "filename": metadata.get("filename"),
                    "score": float(node.score) if node.score is not None else None,
                }
            )
        return payload

    def _format_profile(self, profile: Dict[str, Any]) -> str:
        if not profile:
            return "Non renseigné"
        parts: List[str] = []
        for key, value in profile.items():
            if value in (None, "", []):
                continue
            parts.append(f"{key}: {value}")
        return "; ".join(parts) if parts else "Non renseigné"

    def _analyze_request(self, query: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        fallback = {
            "intent": "FORMATION",
            "focus_topics": [],
            "field_actions": [],
            "safety_flags": [],
            "urgency": "NORMAL",
            "warnings": [],
        }

        if not self.llm:
            fallback["warnings"].append("LLM indisponible pour analyser la demande.")
            return fallback

        profile_text = self._format_profile(profile)
        analyzer_prompt = (
            "Tu es l'ingénieur pédagogique expert d'AgriConnect. Ton rôle est de qualifier la demande de l'utilisateur "
            "pour optimiser la recherche documentaire (RAG) et garantir la sécurité des conseils.\n\n"
            
            f"PROFIL APPRENANT : {profile_text}\n"
            f"QUESTION : {query}\n\n"
            
            "CONSIGNES DE GÉNÉRATION JSON :\n"
            "1. intent : Choisir parmi [FORMATION, URGENCE, CONSEIL].\n"
            "3. intent : Choisir parmi [FORMATION, URGENCE, CONSEIL].\n"
            "4. focus_topics : Liste de mots-clés optimisés pour une recherche sémantique (ex: 'entretien culture niébé', 'lutte chenilles').\n"
            "5. field_actions : Liste les catégories techniques à vérifier dans les documents (ex: 'densité de semis', 'dosage engrais'). Ne donne JAMAIS de chiffres ou de méthodes à ce stade.\n"
            "6. safety_flags : Identifie les risques critiques (ex: 'toxicité pesticides', 'santé animale', 'érosion') nécessitant une attention particulière.\n"
            "7. urgency : Choisir selon l'impact sur la récolte : [NORMAL, HAUTE, CRITIQUE].\n\n"
            
            "RÉPONDS UNIQUEMENT SOUS CE FORMAT JSON :\n"
            "{\n"
            '  "intent": "...",\n'
            '  "focus_topics": [],\n'
            '  "field_actions": [],\n'
            '  "safety_flags": [],\n'
            '  "urgency": "...",\n'
            '  "warnings": []\n'
            "}"
        )

        try:
            completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": analyzer_prompt}],
                temperature=0.1,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content
            if not content:
                raise ValueError("Réponse vide de l'analyseur.")
            
            analysis = json.loads(content)
            return {
                "intent": analysis.get("intent", "FORMATION"),
                "focus_topics": analysis.get("focus_topics", []),
                "field_actions": analysis.get("field_actions", []),
                "safety_flags": analysis.get("safety_flags", []),
                "urgency": analysis.get("urgency", "NORMAL"),
                "warnings": analysis.get("warnings", []),
            }
        except Exception as e:
            logger.warning("Analyse de requête impossible : %s", e)
            fallback["warnings"].append("Analyse de requête automatique indisponible.")
            return fallback
     
    def _fallback_answer(
        self,
        query: str,
        profile_text: str,
        prerequisites: List[str],
        modules: List[str],
        sources: List[Dict[str, Any]],
    ) -> str:
        # Construction d'un texte propre pour les sources
        source_titles = []
        for s in sources:
            title = s.get("title") or s.get("filename") or f"Source {s.get('index')}"
            source_titles.append(title)
        
        sources_text = ", ".join(source_titles) if source_titles else "Fiches techniques locales"

        return (
            "Désolé, je rencontre une difficulté technique momentanée pour générer une réponse détaillée.\n\n"
            "Cependant, voici les ressources identifiées pour vous aider :\n\n"
            f"❓ **Question** : {query}\n"
            f"📚 **Sujet** : {', '.join(modules) if modules else 'Agriculture générale'}\n"
            f"📄 **Documents trouvés** : {sources_text}\n\n"
            "Conseil : Vous pouvez consulter ces documents ou reformuler votre question."
        )

    # ---------------- Business rules / lightweight expert system ----------------

    def diagnose_from_query(self, query: str, profile: Dict[str, Any], context: str) -> Dict[str, Any]:
        q = (query or "").lower()
        rules_used = []
        diagnosis = None
        severity = "medium"
        evidence = []

        if any(k in q for k in ("jauniss", "jaunissement", "feuilles jaunes", "chlorose")):
            diagnosis = "Probable carence en azote"
            rules_used.append("symptom:chlorosis->nitrogen_deficiency")
            evidence.append({"symptom": "jaunissement feuilles"})
            severity = "medium"

        if any(k in q for k in ("taches", "nécrose", "moisissure", "mildiou", "tâche")):
            diagnosis = diagnosis or "Suspicion de maladie fongique"
            rules_used.append("symptom:spots->fungal_disease")
            evidence.append({"symptom": "taches foliaires"})

        if any(k in q for k in ("puceron", "insecte", "ravageur", "chenille", "larve")):
            diagnosis = diagnosis or "Infestation d'insectes ravageurs"
            rules_used.append("symptom:pest_terms->pest_infestation")
            evidence.append({"symptom": "ravageur détecté"})

        if "azote" in (context or "").lower():
            diagnosis = "Carence en azote"
            rules_used.append("context:azote")

        if diagnosis is None:
            diagnosis = "Diagnostic inconclus — besoin de précisions"
            severity = "low"
            rules_used.append("fallback:ask_more")

        return {
            "diagnosis": diagnosis,
            "severity": severity,
            "evidence": evidence,
            "rules_used": rules_used,
        }

    def build_action_plan_from_diag(self, diag: Dict[str, Any], crop_sheet: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        d = diag.get("diagnosis", "")
        immediate = []
        seven = []
        risks = []
        sources = []

        if crop_sheet:
            sources.append(f"Fiche: {crop_sheet.get('crop_name')}")

        if "azote" in d.lower() or "carence en azote" in d.lower() or "nitrogen" in d.lower():
            immediate = ["Appliquer un engrais azoté conforme aux doses locales (voir fiche technique)", "Éviter l'arrosage excessif après fertilisation"]
            seven = ["Surveiller amélioration des feuilles sur 7 jours", "Mesurer croissance et noter tout jaunissement persistant"]
            risks = ["Excès d'azote: lessivage, sensibilité aux maladies"]
        elif "maladie" in d.lower() or "fongique" in d.lower():
            immediate = ["Retirer organes fortement touchés", "Appliquer traitement fongicide localement si disponible"]
            seven = ["Surveiller la propagation; espacer les plantes si nécessaire"]
            risks = ["Propagation rapide en conditions humides"]
        elif "infestation" in d.lower() or "ravageur" in d.lower():
            immediate = ["Inspecter densité d'insectes; appliquer traitement ciblé (biologique de préférence)", "Installer pièges si adapté"]
            seven = ["Vérifier efficacité du traitement; répéter si nécessaire selon notice"]
            risks = ["Resistance si traitement inapproprié"]
        else:
            immediate = ["Collecter photos de la plante et préciser stade de culture"]
            seven = ["Faire un suivi et compléter la fiche mémoire de l'utilisateur"]

        return {
            "diagnostic": d,
            "actions_immediates": immediate,
            "actions_7_days": seven,
            "risks": risks,
            "sources": sources,
        }

    # ---------------- DB helpers ----------------

    def db_execute(self, sql: str, params: Optional[tuple] = None, fetch: bool = False):
        self._ensure_db()
        if not self._psycopg2 or not self._db_url:
            raise RuntimeError("DB client not configured")
        conn = None
        try:
            conn = self._psycopg2.connect(self._db_url)
            cur = conn.cursor()
            cur.execute(sql, params or ())
            rows = cur.fetchall() if fetch else None
            conn.commit()
            cur.close()
            return rows
        finally:
            if conn:
                conn.close()

    def get_crop_sheet(self, crop_name: str) -> Optional[Dict[str, Any]]:
        try:
            rows = self.db_execute(
                "SELECT crop_name, lifecycle, needs, seasonality, common_errors, recommendations FROM crop_tech_sheets WHERE LOWER(crop_name)=LOWER(%s)",
                (crop_name,),
                fetch=True,
            )
            if rows:
                rn = rows[0]
                return {
                    "crop_name": rn[0],
                    "lifecycle": rn[1],
                    "needs": rn[2],
                    "seasonality": rn[3],
                    "common_errors": rn[4],
                    "recommendations": rn[5],
                }
        except Exception:
            return None
        return None

    def save_diagnostic(
        self,
        user_id: Optional[str],
        crop_name: Optional[str],
        zone_id: Optional[str],
        query: str,
        diagnosis: str,
        evidence: Any,
        severity: str,
        rules_used: Any,
    ) -> Optional[str]:
        try:
            self.db_execute(
                "INSERT INTO diagnostics (user_id, crop_name, zone_id, query, diagnosis, evidence, severity, rules_used) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, crop_name, zone_id, query, diagnosis, json.dumps(evidence), severity, json.dumps(rules_used)),
            )
            rows = self.db_execute(
                "SELECT id FROM diagnostics WHERE user_id=%s AND query=%s ORDER BY created_at DESC LIMIT 1",
                (user_id, query),
                fetch=True,
            )
            if rows:
                return str(rows[0][0])
        except Exception as exc:
            logger.exception("Failed to save diagnostic: %s", exc)
        return None

    def save_action_plan(self, diagnostic_id: str, plan: Dict[str, Any], created_by: str = "formation_agent") -> Optional[str]:
        try:
            self.db_execute(
                "INSERT INTO action_plans (diagnostic_id, actions_immediate, actions_7_days, risks, sources, created_by) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    diagnostic_id,
                    json.dumps(plan.get("actions_immediates")),
                    json.dumps(plan.get("actions_7_days")),
                    json.dumps(plan.get("risks")),
                    json.dumps(plan.get("sources")),
                    created_by,
                ),
            )
            rows = self.db_execute(
                "SELECT id FROM action_plans WHERE diagnostic_id=%s ORDER BY created_at DESC LIMIT 1",
                (diagnostic_id,),
                fetch=True,
            )
            if rows:
                return str(rows[0][0])
        except Exception as exc:
            logger.exception("Failed to save action plan: %s", exc)
        return None

