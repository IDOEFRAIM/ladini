from typing import Any, Dict
from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    _detect_disambiguation_candidates,
)
import asyncio

logger = get_node_logger("ClarificationNode")

_MAX_USER_TEXT_IN_PROMPT = 200


def _sanitize_for_prompt(text: str) -> str:
    """Nettoie le texte utilisateur avant injection dans un prompt LLM.

    - Tronque à _MAX_USER_TEXT_IN_PROMPT caractères
    - Supprime les retours à la ligne (vecteur d'injection classique)
    - Remplace les guillemets doubles pour ne pas casser la structure du prompt
    """
    if not text:
        return ""
    clean = text.replace("\r", " ").replace("\n", " ").replace('"', "'").strip()
    return clean[:_MAX_USER_TEXT_IN_PROMPT]


async def clarification_node(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Noeud de clarification pédagogique.

    Activé quand :
    - L'événement est OUT_OF_SCOPE ou UNKNOWN sans tunnel actif
    - Le cognitive_guard a décidé d'abandonner un tunnel

    Utilise le LLM pour générer une réponse contextualisée, chaleureuse
    et pédagogique plutôt qu'un message d'erreur froid.
    """
    event = str(state.get("interpreted_event") or "").upper()
    current_goal = state.get("current_goal")
    expected_input = str(state.get("expected_input") or "NONE").upper()
    cognitive = state.get("cognitive_decision") or {}
    cognitive_action = cognitive.get("action", "")
    user_role = str(state.get("user_role") or "PRODUCER").upper()
    user_name = state.get("user_name","") 
    text = state.get("normalized_text") or state.get("user_query") or ""

    # Only intervene on specific conditions
    needs_clarification = (
        (event in {"OUT_OF_SCOPE", "UNKNOWN"} and expected_input == "NONE" and not current_goal)
        or cognitive_action == "abandon_tunnel_max_retries"
    )
    if not needs_clarification:
        return {}
    if _detect_disambiguation_candidates(text.lower(), user_role):
        return {}

    # Try LLM-powered clarification
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return {}  # fallback handled by final_response CLARIFICATION

    # Refonte double-rôle : tout utilisateur peut vendre ET acheter — la
    # description des capacités ne doit plus être restreinte au rôle de
    # session par défaut (sous peine de suggérer que l'autre moitié des
    # actions est indisponible).
    capabilities = (
        "enregistrer une récolte, mettre en vente un produit, gérer votre "
        "stock, répondre aux demandes d'acheteurs, chercher des produits "
        "agricoles, lancer un appel d'offres, ou suivre vos commandes"
    )

    context_parts = []
    if cognitive_action == "abandon_tunnel_max_retries":
        context_parts.append("L'opération précédente a été annulée car je n'arrivais pas à comprendre.")
    if text:
        context_parts.append(f"L'utilisateur a dit : \"{_sanitize_for_prompt(text)}\"")

    prompt = (
        f"Tu es un assistant commercial agricole WhatsApp au Burkina Faso.\n"
        f"Ton style : coach amical, encourageant, patient.\n"
        f"L'utilisateur ({user_name or 'un producteur'}, rôle {user_role}) "
        f"a envoyé un message que tu ne comprends pas.\n"
        f"{'  '.join(context_parts)}\n\n"
        f"Tu peux l'aider à : {capabilities}.\n"
        f"Explique brièvement ce que tu peux faire et encourage-le à reformuler.\n"
        f"Donne 2-3 exemples concrets de phrases qu'il pourrait dire.\n"
        f"Réponds en 2-3 phrases max, en français simple et direct."
    )

    try:
        completion = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: llm.chat.completions.create(
                    model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.4,
                    max_tokens=150,
                )
            ),
            timeout=8.0,
        )
        result = (completion.choices[0].message.content or "").strip()
        if result:
            return {
                "final_response": result,
                "response_strategy": "CLARIFICATION",
                "ag_ui_component": None,
            }
    except Exception as exc:
        logger.warning("[ClarificationNode] LLM call failed: %s", exc)

    return {}


