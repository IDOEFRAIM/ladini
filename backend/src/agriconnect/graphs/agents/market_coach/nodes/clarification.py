import asyncio
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.llm_gateway import (
    resolve_gateway,
    resolve_profile,
)
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    _detect_disambiguation_candidates,
)
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime

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


async def clarification_node(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Noeud de clarification pédagogique.

    Activé quand :
    - L'événement est OUT_OF_SCOPE ou UNKNOWN sans tunnel actif
    - Le cognitive_guard a décidé d'abandonner un tunnel

    Utilise le LLM pour générer une réponse contextualisée, chaleureuse
    et pédagogique plutôt qu'un message d'erreur froid.
    """
    event = str(state.get("interpreted_event") or "").upper()
    current_goal = state.get("current_goal")
    pending_kind = get_pending_interaction(state).kind
    nothing_pending = pending_kind == InteractionKind.NONE
    cognitive = state.get("cognitive_decision") or {}
    cognitive_action = cognitive.get("action", "")
    user_role = str(state.get("user_role") or "PRODUCER").upper()
    user_name = state.get("user_name", "")
    text = state.get("normalized_text") or state.get("user_query") or ""

    # --- LOCATION OUTCOME (précédence — 2026-09-02, "un seul propriétaire
    # de la réponse") --------------------------------------------------
    # Un point GPS partagé HORS d'une étape GPS active (aucun
    # `PendingInteraction.PROVIDE_LOCATION` en cours — quand c'est le cas,
    # `flows/buyer/gps_delivery_gate.py::resolve_gps_stage` s'en charge
    # lui-même, plus bas dans le graphe) doit quand même être commenté UNE
    # fois. Ce commentaire vit ICI et nulle part ailleurs : le webhook
    # (api/routes/twilio_webhook.py`/`whatsapp_webhook.py`) ne fait plus
    # QUE persister le fait brut (`location_outcome`) — il n'a plus le
    # droit d'envoyer de message lui-même. Message déterministe (pas de
    # dépendance LLM) : c'est une simple traduction d'un contrat de
    # domaine déjà fermé (`core/location.py::LocationOutcome`), pas une
    # interprétation de langage libre.
    location_outcome = str(state.get("location_outcome") or "").upper()
    if (
        bool(state.get("location_shared"))
        and location_outcome in {"LOCATION_OUT_OF_ZONE", "LOCATION_PERSISTENCE_ERROR"}
        and pending_kind != InteractionKind.PROVIDE_LOCATION
    ):
        if location_outcome == "LOCATION_OUT_OF_ZONE":
            message = (
                "📍 Cette position semble se trouver hors de notre zone de "
                "livraison (Burkina Faso) — rien n'a été enregistré."
            )
        else:
            message = (
                "Un souci technique a empêché l'enregistrement de ce point "
                "GPS. Vous pouvez le repartager si besoin."
            )
        return {
            "final_response": message,
            "response_strategy": "CLARIFICATION",
            "ag_ui_component": None,
        }

    # Only intervene on specific conditions. `not current_goal` is
    # deliberate for OUT_OF_SCOPE/UNKNOWN/REJECT : when a goal IS active,
    # the tunnel-specific renderers (nodes/rendering/ask.py::render_ask_missing_field,
    # feedback.py::render_recovery) already generate their own adaptive
    # reply for the same deviation — this node only needs to cover the
    # "nothing active" case, which response_strategy.py routes to
    # CLARIFICATION for OUT_OF_SCOPE/UNKNOWN, REJECT, and tunnel abandonment
    # alike. REJECT was added after finding it fell through to the generic
    # "Je n'ai pas bien saisi" fallback with no chance at an adaptive reply
    # (a stray "non" with nothing pending). See
    # [[precommande-architecture-consolidation-2026-08]].
    needs_clarification = (
        event in {"OUT_OF_SCOPE", "UNKNOWN", "REJECT"}
        and nothing_pending
        and not current_goal
    ) or cognitive_action == "abandon_tunnel_max_retries"
    if not needs_clarification:
        return {}
    if _detect_disambiguation_candidates(text.lower(), user_role):
        return {}

    # ── LLM indisponible CE TOUR (2026-09-05, incident "je veux voir les
    # enchères" → UNKNOWN puis clarification identique) ────────────────
    # `input_interpreter` a déjà tenté le LLM Gateway et a échoué avec une
    # panne d'infrastructure (`unknown_reason=TECHNICAL_FAILURE` — voir
    # `interpreter/interpreter_result.py`, panne AUTH/tous providers
    # indisponibles/budget épuisé). Retenter ICI le même Gateway, sur le
    # même tour, ne peut logiquement qu'échouer À NOUVEAU pour la même
    # raison (§11 du brief incident) : c'est un appel réseau pur perte,
    # ET c'est ce qui produisait le double log "mêmes providers, mêmes
    # failures" observé en prod. Repli déterministe honnête à la place —
    # jamais "je n'ai pas bien saisi" pour une panne technique (§12/§15) :
    # c'est un mensonge sur la cause, l'utilisateur n'a rien mal formulé.
    if str(state.get("unknown_reason") or "").upper() == "TECHNICAL_FAILURE":
        logger.info(
            "[ClarificationNode] LLM déjà indisponible ce tour "
            "(unknown_reason=TECHNICAL_FAILURE) — repli déterministe, "
            "aucun second appel Gateway"
        )
        return {
            "final_response": (
                "🔧 Notre assistant intelligent est momentanément "
                "indisponible. Réessayez dans quelques instants — vos "
                "commandes et votre panier restent intacts."
            ),
            "response_strategy": "CLARIFICATION",
            "ag_ui_component": None,
        }

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
        context_parts.append(
            "L'opération précédente a été annulée car je n'arrivais pas à comprendre."
        )
    if text:
        context_parts.append(f'L\'utilisateur a dit : "{_sanitize_for_prompt(text)}"')

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
        # LLM Gateway (2026-09-02) : budget/repli/disjoncteur portés par le
        # Gateway — voir `llm_gateway/gateway.py`.
        completion = await resolve_gateway(mc_runtime).complete(
            profile=resolve_profile(mc_runtime),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=150,
            agent_node="clarification_node",
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
