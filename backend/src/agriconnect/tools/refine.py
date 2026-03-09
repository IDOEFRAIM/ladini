from typing import Any, List, Dict,Annotated
import logging

logger  = logging.getLogger("RefineTool")


class RefineTool:
    def __init__(self, llm, model="llama-3.3-70b-versatile"):
        self.llm = llm
        self.model = model
        self.model_planner = "llama-3.1-8b-instant"  # ✅ Ajout du model_planner


   
    def rewrite_query_node(self, state):
        query = state.get("user_query", "")
        warnings = list(state.get("warnings", []))
        current_retry = state.get("rewrited_retry_count", 0)

        # Limitation anti-boucle
        if current_retry >= 2:
            warnings.append("Abandon après 2 tentatives de reformulation.")
            return{"status": "MAX_RETRIES", "warnings": warnings}
        
        prompt = (
            "Tu es un expert en recherche documentaire agricole. La recherche précédente n'a rien donné.\n"
            f"Reformule cette question pour qu'elle soit plus concise et utilise des mots-clés "
            f"génériques de l'agriculture sahélienne : {query}\n"
            "Réponds uniquement avec la nouvelle requête."
        )
        
        try:
            completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2
            )
            new_query = completion.choices[0].message.content
            warnings.append(f"Requête reformulée ({current_retry+1}/2) : {new_query}")
            return {
                "optimized_query": new_query, 
                "warnings": warnings, 
                "status": "RETRY_SEARCH",
                "rewrited_retry_count": current_retry + 1
            }
        except Exception:
            return {"status": "ERROR", "warnings": warnings}
        
    def critique_node(self, state) :
        answer = state.get("final_response", "")
        context = state.get("retrieved_context", "")
        count = state.get("critique_retry_count", 0)

        prompt = self._build_critique_prompt(answer, context)

        try:
            completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
            )
        except Exception as exc:
            logger.warning("Critique LLM call failed: %s", exc)
            return {"status": "REJECTED", "warnings": ["Critique indisponible."]}

        verdict = self._extract_verdict_from_completion(completion)
        if not verdict:
            return {"status": "REJECTED"}

        if verdict == "FAILED":
            return self._handle_failed_verdict(count)

        if verdict == "PASSED":
            return self._handle_passed_verdict(state)

        return {"status": "REJECTED"}

    def _build_critique_prompt(self, answer: str, context: str) -> str:
        return (
            "RÔLE : Vérificateur de sécurité agricole.\n"
            "TACHE : Compare la RÉPONSE avec le CONTEXTE technique.\n"
            "Si la réponse donne un chiffre (dose, date, pH) qui n'est PAS dans le contexte, ou si elle propose une reponse qui n'est pas en accord avec le contexte, "
            "réponds 'FAILED'. Sinon, réponds 'PASSED'.\n\n"
            f"CONTEXTE : {context}\n"
            f"RÉPONSE : {answer}"
        )

    def _extract_verdict_from_completion(self, completion: Any) -> str:
        try:
            content = completion.choices[0].message.content
            return (content or "").strip().upper()
        except Exception:
            return ""

    def _handle_failed_verdict(self, count: int) -> Dict[str, Any]:
        # On autorise 2 tentatives de correction
        if count < 2:
            return {
                "status": "REJECTED",
                "critique_retry_count": count + 1,
                "warnings": ["Alerte : Hallucination détectée. Tentative de correction."],
            }

        # Sécurité : trop d'échecs, on valide quand même avec un fort warning
        return {
            "status": "VALIDATED",
            "warnings": ["Attention : Réponse non validée techniquement après plusieurs essais."],
        }

    def _handle_passed_verdict(self, state: Dict[str, Any]) -> Dict[str, Any]:
        updates: Dict[str, Any] = {"status": "VALIDATED"}
        if state.get("answer_draft"):
            updates["final_response"] = state.get("answer_draft")
        return updates


    # ------------------------------------------------------------------ #
    # -----------------Routing------------------------------------------ #
    # ------------------------------------------------------------------ #


    def route_after_analyze(self,state) -> str:
        """
        Détermine le chemin suivant l'analyse initiale.
        Fait office de tour de contrôle pour la sécurité et la pertinence.
        """
        # Prefer explicit relevance flag produced by analyzers
        if state.get("is_relevant") is False:
            return "compose"
        if state.get("is_relevant") is True:
            return "retrieve"

        # Fallback to status-based check
        if state.get("status") == "OFF_TOPIC":
            return "compose"
        return "retrieve"
    
    def route_retrieval(self,state)-> str:
        # Si on a trouvé du contexte (divers noms possibles), on va à la rédaction
        if state.get("status") in ("CONTEXT_FOUND", "CONTEXT_READY"):
            return "compose"
        # Si on a atteint le max de retries ou erreur, on rédige quand même (fallback)
        if state.get("status") in ["MAX_RETRIES", "ERROR"]:
            return "compose"
        # Sinon, on tente une reformulation
        return "rewrite"

    def route_after_rewrite(self, state) -> str:
        """Si on a dépassé le max retries dans le Nœud Rewrite, on arrête."""
        if state.get("status") == "MAX_RETRIES":
            return "compose"
        return "retrieve"


