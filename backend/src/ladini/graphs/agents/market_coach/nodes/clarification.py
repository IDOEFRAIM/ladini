from typing import Any, Dict

from ladini.domain.profile_requirements import display_name_or_none
from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.llm_gateway import (
    resolve_gateway,
    resolve_profile,
)
from ladini.graphs.agents.market_coach.utils import (
    NO_FABRICATED_FACTS_RULE,
    MarketRuntime,
)

logger = get_node_logger("ClarificationNode")

_MAX_USER_TEXT_IN_PROMPT = 200

# (2026-09-13, chantier State Router — Incrément E) : ce prompt était déjà,
# avant cet incrément, un micro-prompt minimal (aucun catalogue de 41
# intentions, aucun ID, aucun schéma — une description humaine fixe des
# capacités) — seule la traçabilité Langfuse manquait. Version explicite
# ajoutée ici, pas de refonte du prompt (déjà conforme à l'esprit de ce
# chantier).
CLARIFICATION_PROMPT_VERSION = "clarification_v1"


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


def has_out_of_tunnel_location_error(state: Dict[str, Any]) -> bool:
    """Prédicat structurel PARTAGÉ (2026-09-08, correction topologique du
    bloc conversationnel) : « ce tour porte-t-il un point GPS hors-tunnel
    en échec (hors zone / erreur de persistance) qui doit être signalé,
    quel que soit l'état du goal/pending par ailleurs ? »

    Extrait de `_render_out_of_tunnel_location_outcome` ci-dessous pour que
    `cognitive_guard` (désormais seul propriétaire de la décision
    conversationnelle, voir sa docstring) puisse router vers ce nœud SANS
    dupliquer la connaissance du domaine GPS lui-même (les valeurs
    `LOCATION_OUT_OF_ZONE`/`LOCATION_PERSISTENCE_ERROR`, le rôle de
    `PROVIDE_LOCATION`) — un SEUL endroit connaît ces constantes, les deux
    appelants importent seulement ce booléen. Le message à afficher, lui,
    reste ICI (dette GPS non auditée, voir docstring ci-dessous, toujours
    valable)."""
    pending_kind = get_pending_interaction(state).kind
    location_outcome = str(state.get("location_outcome") or "").upper()
    return (
        bool(state.get("location_shared"))
        and location_outcome in {"LOCATION_OUT_OF_ZONE", "LOCATION_PERSISTENCE_ERROR"}
        and pending_kind != InteractionKind.PROVIDE_LOCATION
    )


def _render_out_of_tunnel_location_outcome(state: Dict[str, Any]) -> Dict[str, Any]:
    """(2026-09-08, mandat §10 : dette explicitement assumée, pas déplacée)
    — la gestion de `LOCATION_OUT_OF_ZONE`/`LOCATION_PERSISTENCE_ERROR` est
    une responsabilité de DOMAINE (GPS/livraison) mal placée dans un nœud
    de clarification générique. Elle reste ICI, encapsulée dans ce helper
    clairement nommé, PARCE QUE les flows GPS (`flows/buyer/
    gps_delivery_gate.py`, `core/location.py`) n'ont pas encore été
    inspectés dans ce chantier — le mandat interdit explicitement
    d'inventer leur futur propriétaire sans les auditer. Ne PAS
    généraliser ce helper à d'autres domaines ; le déplacer dès que les
    flows GPS seront audités.

    Retourne `{}` si aucun cas GPS hors-tunnel n'est applicable (le nœud
    appelant poursuit alors son propre traitement)."""
    location_outcome = str(state.get("location_outcome") or "").upper()
    if not has_out_of_tunnel_location_error(state):
        return {}

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


async def clarification_node(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Noeud de clarification — EXÉCUTEUR d'une décision déjà prise.

    Ce nœud ne décide jamais « faut-il clarifier ? » — cette question
    appartient exclusivement à `cognitive_guard` (voir sa docstring et
    `nodes/routing.py::_route_after_cognitive_guard`). Le graphe compilé
    n'atteint ce nœud que par DEUX actions :

    - CLARIFY — hors-tunnel sans rien d'actif (OUT_OF_SCOPE/UNKNOWN/REJECT),
      ou point GPS hors-tunnel en échec (`has_out_of_tunnel_location_error`,
      priorité absolue) ;
    - ABANDON_ACTIVE_GOAL — tunnel abandonné après trop d'échecs ;
      `response_strategy` est déjà à "CLARIFICATION" avant ce nœud (posé
      par `core/conversation_reset.py`) — ce nœud l'ENRICHIT d'un message
      LLM contextualisé s'il peut, sans quoi le récap générique déjà posé
      suffit (voir `_route_after_clarification`, `core/graph_builder.py`).

    RECOVER_ACTIVE_GOAL ne passe PLUS par ici (2026-09-08, clôture Bloc 1,
    mandat §29) : prouvé no-op structurel pour cette action (la relance
    RECOVERY est entièrement rendue par `interpreter/strategy.py` depuis
    `cognitive_action` seul), `nodes/routing.py::_COGNITIVE_ACTION_ROUTES`
    route désormais cette action DIRECTEMENT vers `response_strategy`. Le
    early-return ci-dessous reste un garde-fou de compatibilité (appel
    direct hors graphe, test) — plus jamais exercé par le graphe compilé.

    Ce nœud choisit seulement QUEL message produire (GPS déterministe /
    panne technique déterministe / LLM pédagogique), jamais SI un message
    est dû.
    """
    cognitive = state.get("cognitive_decision") or {}
    cognitive_action = cognitive.get("action", "")
    # (2026-09-08, mandat §10) : plus de défaut PRODUCER — voir
    # graphs/roles.py::normalize_role, même correctif.
    user_role = str(state.get("user_role") or "").upper()
    user_name = display_name_or_none(state.get("user_name")) or ""
    text = state.get("normalized_text") or state.get("user_query") or ""

    # --- LOCATION OUTCOME (précédence — 2026-09-02, "un seul propriétaire
    # de la réponse") — encapsulé, voir docstring de
    # `_render_out_of_tunnel_location_outcome` pour la justification.
    # Reste un check DIRECT (pas seulement `action=="CLARIFY"`) : c'est la
    # garantie structurelle que le message GPS prime même si un futur appel
    # de ce nœud omettait de re-vérifier `cognitive_action` en amont.
    location_patch = _render_out_of_tunnel_location_outcome(state)
    if location_patch:
        return location_patch

    # Garde-fou de compatibilité (appel direct hors graphe compilé) — voir
    # docstring : RECOVER_ACTIVE_GOAL n'atteint plus jamais ce nœud via le
    # graphe réel.
    if cognitive_action == ConversationAction.RECOVER_ACTIVE_GOAL:
        return {}

    # Garde-fou de compatibilité : ce nœud ne rend un message QUE pour les
    # deux décisions qui l'exigent. Toute autre valeur (contrat non
    # respecté par l'appelant — invocation directe hors graphe compilé,
    # test unitaire, futur bug de routage) est un no-op sûr plutôt qu'un
    # rendu inventé sans mandat.
    if cognitive_action not in {
        ConversationAction.CLARIFY,
        ConversationAction.ABANDON_ACTIVE_GOAL,
    }:
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
    if cognitive_action == ConversationAction.ABANDON_ACTIVE_GOAL:
        context_parts.append(
            "L'opération précédente a été annulée car je n'arrivais pas à comprendre."
        )
    if text:
        context_parts.append(f'L\'utilisateur a dit : "{_sanitize_for_prompt(text)}"')

    prompt = (
        f"Tu es un assistant commercial agricole WhatsApp au Burkina Faso.\n"
        f"Ton style : coach amical, encourageant, patient.\n"
        # (2026-09-08, mandat §10 "double rôle") : ne plus présumer
        # "producteur" par défaut — un même utilisateur peut acheter ET
        # vendre, quel que soit son rôle de profil enregistré.
        f"L'utilisateur ({user_name or 'vous'}, rôle {user_role or 'non précisé'}) "
        f"a envoyé un message que tu ne comprends pas.\n"
        f"{'  '.join(context_parts)}\n\n"
        f"Tu peux l'aider à : {capabilities}.\n"
        f"Explique brièvement ce que tu peux faire et encourage-le à reformuler.\n"
        f"Donne 2-3 exemples concrets de phrases qu'il pourrait dire.\n"
        f"Réponds en 2-3 phrases max, en français simple et direct.\n"
        f"{NO_FABRICATED_FACTS_RULE}"
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
            extra_metadata={
                "prompt_family": "clarification",
                "prompt_version": CLARIFICATION_PROMPT_VERSION,
                "cognitive_action": str(cognitive_action),
            },
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
