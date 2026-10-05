"""Unified onboarding — data-driven slot-filling with free ordering.

Design
------
- ONE LLM call per turn extracts every field it can (role, name, zone, confirm).
- Confirmation during CONFIRM_DETAILS is checked FIRST against the canonical
  deterministic phrase set (`confirmation_phrases.py`, shared with
  `interpreter/routing.py`'s `_interpret_fast_path` — no second, onboarding-only
  lexicon): only free-text OUTSIDE that closed vocabulary falls back to the LLM's
  own YES/NO judgment (mandat 2026-09-26, "'ok' ne confirme pas le profil" — see
  `_fast_confirm_from_text`).
- Any slot can be updated at any moment, including after a confirmation
  prompt — a change re-triggers confirmation with the new summary.
- ``step`` is derived from what is filled, not from a rigid sequence.
- Zone resolution never blocks profile creation (mandat 2026-09-26,
  "onboarding != éligibilité logistique") — see `_resolve_zone`/
  `ZoneResolution` for the COVERED/NEARBY/OUT_OF_COVERAGE trichotomy.
"""
from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from ladini.domain.burkina_regions import region_list_text, resolve_region
from ladini.agents.confirmation_phrases import (
    _CONFIRM_EXACT_PHRASES,
    _REJECT_EXACT_PHRASES,
)

logger = logging.getLogger("Ladini.Agents.Onboarding")


class OnboardingStep(str, Enum):
    COLLECT_ROLE = "COLLECT_ROLE"
    COLLECT_NAME = "COLLECT_NAME"
    COLLECT_ZONE = "COLLECT_ZONE"
    CONFIRM_DETAILS = "CONFIRM_DETAILS"
    CREATE_PROFILE = "CREATE_PROFILE"
    # Dernière étape, STRICTEMENT APRÈS création du profil (jamais bloquante) :
    # une seule invite GPS, puis l'onboarding se termine quoi qu'il arrive au
    # tour suivant — voir _step_collect_location.
    COLLECT_LOCATION = "COLLECT_LOCATION"
    DONE = "DONE"


@dataclass
class OnboardingState:
    step: OnboardingStep = OnboardingStep.COLLECT_ROLE
    phone: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    zone_name: Optional[str] = None
    zone_id: Optional[str] = None
    # Localisation/couverture (mandat 2026-09-26, migration 0005) : `declared_location` est le
    # texte BRUT donné par l'utilisateur ("Somgandé"), jamais transformé/deviné — conservé même
    # quand aucune zone de service ne lui correspond (invariant : "onboarding != éligibilité
    # logistique", jamais de blocage sur la couverture). `coverage_status` (COVERED|NEARBY|
    # OUT_OF_COVERAGE) n'est posé qu'UNE FOIS par valeur de zone traitée — `None` signifie "la
    # zone n'a pas encore été résolue ce tour-ci" (voir la garde dans `run_onboarding_step`, qui
    # évite de re-résoudre indéfiniment la même valeur déjà tranchée).
    declared_location: Optional[str] = None
    coverage_status: Optional[str] = None
    error: Optional[str] = None
    prompt: str = ""
    completed: bool = False
    created_profile: Dict[str, Any] = field(default_factory=dict)
    _filled_slots: Dict[str, bool] = field(default_factory=dict, repr=False)
    # Nombre de fois où on a déjà répondu à une question hors-sujet dans CETTE
    # session d'onboarding — voir [[onboarding-adaptive-questions-2026-08]].
    # Sans ce compteur, une explication IDENTIQUE au mot près se répétait à
    # chaque relance/insulte de l'utilisateur, ce qui a fini par l'agacer
    # ("arrête de m'envoyer ce même texte") plutôt que de le rassurer.
    explain_count: int = 0
    # (2026-10-05) La région n'est redemandée QU'UNE fois (localité inconnue / pays seul) :
    # une 2e réponse non résolue part en OUT_OF_COVERAGE sans bloquer l'onboarding.
    region_clarify_asked: bool = False

    def missing_slots(self) -> List[str]:
        missing: List[str] = []
        if not self.role:
            missing.append("role")
        if not self.name:
            missing.append("name")
        # La zone est "manquante" tant qu'aucune décision de couverture n'a été prise — une
        # fois `coverage_status` posé (même OUT_OF_COVERAGE, `zone_id` alors NULL), la question
        # est TRANCHÉE et ne doit plus être reposée (invariant : onboarding non bloquant).
        if not self.zone_id and not self.zone_name and not self.coverage_status:
            missing.append("zone")
        return missing


@dataclass
class OnboardingResult:
    state: OnboardingState
    response_text: str = ""
    ag_ui_component: Optional[Dict[str, Any]] = None
    status: str = "WAITING_INPUT"

    def to_state_updates(self) -> Dict[str, Any]:
        updates: Dict[str, Any] = {
            "onboarding_step": self.state.step.value,
            "onboarding_prompt": self.response_text,
            "status": self.status,
            "ag_ui_component": self.ag_ui_component,
            "user_role": self.state.role,
            "user_name": self.state.name,
            "zone_name": self.state.zone_name,
            "zone_id": self.state.zone_id,
            "declared_location": self.state.declared_location,
            "coverage_status": self.state.coverage_status,
        }
        if self.state.completed:
            updates["is_onboarding"] = False
            updates["onboarding_step"] = None
            updates["user_phone"] = self.state.phone
            updates["user_name"] = self.state.name
            updates["user_role"] = self.state.role
            updates["zone_name"] = self.state.zone_name
            updates["zone_id"] = self.state.zone_id
            updates["declared_location"] = self.state.declared_location
            updates["coverage_status"] = self.state.coverage_status
            updates["user_context_loaded"] = True
            if self.state.created_profile:
                updates["user_id"] = str(self.state.created_profile.get("id", ""))
        return updates


def _normalize_role(value: Optional[str]) -> Optional[str]:
    """Valide et normalise les sorties de l'extracteur LLM (BUYER|PRODUCER)."""
    if not value:
        return None

    norm_val = str(value).strip().upper()
    if norm_val in {"BUYER", "PRODUCER"}:
        return norm_val

    return None


def _merge_extracted(
    ob_state: OnboardingState, extracted: Dict[str, Optional[str]]
) -> bool:
    """Apply extracted values onto *ob_state*. Return True if any slot changed.

    A change ALWAYS invalidates a prior confirmation — the caller uses this to
    decide whether to re-ask "confirmes-tu ?" after a mid-flow correction.
    """
    changed = False

    role = _normalize_role(extracted.get("role"))
    if role and role != ob_state.role:
        ob_state.role = role
        ob_state._filled_slots["role"] = True
        changed = True

    raw_name = extracted.get("name")
    if raw_name:
        name = str(raw_name).strip()
        if name and name.lower() != (ob_state.name or "").lower():
            ob_state.name = name
            ob_state._filled_slots["name"] = True
            changed = True

    raw_zone = extracted.get("zone")
    if raw_zone:
        zone = str(raw_zone).strip()
        # Comparé à `zone_name` OU `declared_location` : une localité déjà tranchée
        # OUT_OF_COVERAGE n'a plus de `zone_name` (voir plus bas) mais garde son
        # `declared_location` — sans ce second terme, répéter la MÊME localité non couverte
        # d'un tour à l'autre la ferait paraître "changée" à chaque fois et relancerait une
        # résolution DB inutile (jamais un bug fonctionnel, juste un coût réseau superflu et
        # une confirmation ré-invalidée à tort).
        # (`declared_location` seul ne compte que si la zone a été TRANCHÉE : après une relance de
        # région, répéter la même localité doit la re-résoudre — sans repasser par la relance.)
        previous = (
            ob_state.zone_name or (ob_state.declared_location if ob_state.coverage_status else "") or ""
        ).upper()
        if zone and zone.upper() != previous:
            ob_state.zone_name = zone
            ob_state.zone_id = None
            ob_state.declared_location = None
            ob_state.coverage_status = None
            ob_state._filled_slots["zone"] = True
            changed = True

    return changed


def _role_capabilities_message(role: Optional[str]) -> str:
    role_norm = str(role or "").upper()
    if role_norm == "BUYER":
        return (
            "🛒 Patron, maintenant tu peux me demander :\n"
            "• *Chercher un produit* — je fouille les offres autour de toi\n"
            "• *Commander* ou *précommander* — en quelques mots\n"
            "• *Suivre tes commandes* — je te tiens au courant\n\n"
            "Dis-moi ce que tu cherches, je m’en occupe ! 💪"
        )
    if role_norm == "PRODUCER":
        return (
            "🧑🏾‍🌾 Patron, voici ce que je peux faire pour toi :\n"
            "• *Publier un produit* — tes récoltes visibles par les acheteurs\n"
            "• *Gérer ton stock* — entrées, sorties, tout est suivi\n"
            "• *Déclarer une future récolte* — les acheteurs peuvent précommander\n"
            "• *Voir tes commandes* — tout ce qui arrive\n\n"
            "Quel produit veux-tu mettre en avant ? 🌾"
        )
    return (
        "Patron, je suis prêt à t’accompagner ! "
        "Dis-moi si tu veux acheter ou vendre et on démarre ensemble. 💪"
    )


def _unwrap_tool_payload(raw: Any, *, _depth: int = 0) -> Optional[Dict[str, Any]]:
    if raw is None or _depth > 6:
        return None
    if isinstance(raw, dict):
        for key in ("data", "result", "payload", "response"):
            value = raw.get(key)
            if value is not None and value is not raw:
                nested = _unwrap_tool_payload(value, _depth=_depth + 1)
                if nested:
                    return nested
        raw_result = raw.get("raw_result")
        if raw_result is not None:
            nested = _unwrap_tool_payload(raw_result, _depth=_depth + 1)
            if nested:
                return nested
        for key in ("zones", "items", "results"):
            collection = raw.get(key)
            if isinstance(collection, list):
                nested = _unwrap_tool_payload(collection, _depth=_depth + 1)
                if nested:
                    return nested
        return raw
    if isinstance(raw, list):
        for item in raw:
            nested = _unwrap_tool_payload(item, _depth=_depth + 1)
            if nested:
                return nested
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        if stripped[0] in "[{":
            try:
                return _unwrap_tool_payload(json.loads(stripped), _depth=_depth + 1)
            except Exception:
                try:
                    return _unwrap_tool_payload(
                        ast.literal_eval(stripped), _depth=_depth + 1
                    )
                except Exception:
                    return None
        return None
    return None


BulkExtractor = Callable[[str, str], Coroutine[Any, Any, Dict[str, Optional[str]]]]


def _context_hint_for_llm(ob_state: OnboardingState) -> str:
    """Grounding hints for the LLM's dynamic reply — what's already known,
    what's missing, how many times this user has already been answered.
    Deliberately NOT canned response text: see [[onboarding-adaptive-questions-2026-08]]."""
    known: List[str] = []
    if ob_state.role:
        known.append(f"role={ob_state.role}")
    if ob_state.name:
        known.append(f"nom={ob_state.name}")
    if ob_state.zone_name or ob_state.zone_id or ob_state.declared_location:
        known.append(f"zone={ob_state.zone_name or ob_state.declared_location or ob_state.zone_id}")
    missing = ", ".join(ob_state.missing_slots()) or "aucun"
    return (
        f"Deja connu : {', '.join(known) or 'rien'}. Encore manquant : {missing}. "
        f"Nombre de fois deja repondu a une question hors-sujet dans cette session : {ob_state.explain_count}."
    )


# (2026-09-18, retour produit) : le premier message précédent noyait la
# SEULE action attendue au premier tour (répondre producteur/acheteur) sous
# deux paragraphes de pitch produit (mission Ladini + détail par rôle) — pas
# limpide sur ce qu'il fallait faire. Le pitch complet existe déjà ailleurs
# (`_EXPLAIN_AGAIN`, affiché SI l'utilisateur demande "c'est quoi Ladini ?")
# — inutile de le dupliquer ici. Ce premier message reste court : qui est
# LADINI (une phrase), LA question à laquelle répondre, et une invite
# explicite à demander plus de détails si besoin.
_WELCOME = (
    "🌾 *Bienvenue patron !* Je suis *LADINI*, ton assistant sur la plateforme "
    "agricole qui te connecte directement à d'autres producteurs et acheteurs, "
    "par simple message WhatsApp.\n\n"
    "Pour commencer, dis-moi : tu es *producteur* (tu vends) "
    "ou *acheteur* (tu achètes) ?\n\n"
    "_💡 Tu peux tout me dire d'un coup — par exemple « Je suis Awa, productrice "
    "à Bobo » — ou avancer étape par étape, comme tu préfères. Tape « c'est quoi "
    "Ladini ? » si tu veux en savoir plus avant de commencer._"
)


# Invite GPS — dernière étape de l'onboarding, jamais bloquante (voir
# _step_collect_location). Pédagogie explicite car beaucoup d'utilisateurs ne
# savent pas partager une position WhatsApp du premier coup.
_LOCATION_PROMPT = (
    "📍 Pour terminer, partage ta position GPS : clique sur le trombone 📎 de "
    "WhatsApp puis sur *Localisation* (Position actuelle). Ça nous aide à te "
    "connecter avec des partenaires proches de chez toi !\n\n"
    "_Tu peux aussi passer cette étape — tu pourras toujours partager ta "
    "position plus tard._"
)

_LOCATION_THANKS = (
    "📍 Position enregistrée, merci patron ! Tu es maintenant connecté aux "
    "meilleures opportunités près de chez toi. 🎉"
)

_LOCATION_SKIPPED = (
    "Pas de souci, on continue sans ! Tu pourras toujours partager ta "
    "position plus tard — il te suffira de renvoyer ta localisation à tout "
    "moment. 😊"
)

# Réponse courte à une question posée EN COURS d'onboarding ("c'est quoi ce
# truc", "comment ça marche", "je crée un compte pour mon père, comment ça se
# passe"). Sans ceci, `run_onboarding_step` répondait toujours par la MÊME
# question de collecte de champ, quel que soit le message reçu — l'agent
# ignorait purement et simplement la question posée (retour utilisateur
# 2026-08-13 : "grand problème d'adaptivité... l'agent ne doit pas être trop
# figé"). Volontairement plus court que `_WELCOME` : ce n'est pas un cold
# start, juste une clarification avant de reprendre la collecte.
_EXPLAIN_AGAIN = (
    "Bien sûr patron, je t'explique ! 😊 *Ladini*, c'est une plateforme "
    "qui connecte directement producteurs et acheteurs agricoles par simple "
    "message WhatsApp — pas d'intermédiaire, pas de déplacement inutile. "
    "Tu peux vendre tes récoltes ou trouver de bons produits selon ton "
    "besoin, et oui, tu peux créer un compte pour quelqu'un d'autre (ex: "
    "ton père) tant que tu réponds pour lui. C'est gratuit et ça prend une "
    "minute pour démarrer !"
)

# 2e question (ou plus) dans la MÊME session d'onboarding : ne PAS répéter
# `_EXPLAIN_AGAIN` mot pour mot — l'utilisateur l'a déjà lu. On raccourcit et
# on répond directement à l'inquiétude la plus fréquente à ce stade (est-ce
# une arnaque ?) plutôt que de re-dérouler le pitch produit.
_EXPLAIN_AGAIN_SHORT = (
    "Je comprends ta prudence patron, c'est normal de vérifier ! 🙏 "
    "Ladini est un vrai service gratuit, sans engagement — déjà "
    "utilisé par des producteurs et acheteurs près de chez toi. Donne-moi "
    "juste ton nom et ta région pour avancer, tu peux toujours changer "
    "d'avis après."
)


def _ack_line(ob_state: OnboardingState) -> str:
    parts: List[str] = []
    if ob_state._filled_slots.get("name") and ob_state.name:
        parts.append(f"Enchanté patron *{ob_state.name}* ! 🤝")
    if ob_state._filled_slots.get("role") and ob_state.role:
        if ob_state.role == "BUYER":
            parts.append("Parfait, tu cherches de bons produits — je suis là pour ça !")
        else:
            parts.append(
                "Super, un producteur qui veut vendre plus — on va faire du bon travail ensemble !"
            )
    if ob_state._filled_slots.get("zone"):
        # Trois issues possibles de la résolution de zone (voir `_resolve_zone`) : jamais un
        # rejet sec (mandat 2026-09-26, "onboarding != éligibilité logistique") — la localité
        # déclarée est TOUJOURS conservée et l'onboarding continue dans tous les cas.
        if ob_state.coverage_status == "NEARBY" and ob_state.zone_name:
            parts.append(
                f"J'ai noté *{ob_state.declared_location}* — je te rattache à "
                f"*{ob_state.zone_name}* pour nos services 📍"
            )
        elif ob_state.coverage_status == "OUT_OF_COVERAGE" and ob_state.declared_location:
            parts.append(
                f"J'ai bien noté *{ob_state.declared_location}*. Ladini n'y est pas encore "
                "présent, mais je peux quand même créer ton profil et te prévenir dès "
                "l'ouverture de la zone ! 🙌"
            )
        elif ob_state.zone_name:
            parts.append(f"D'accord, je retiens *{ob_state.zone_name}* ✅")
    return " ".join(parts)


def _missing_labels(ob_state: OnboardingState) -> List[str]:
    labels: List[str] = []
    if not ob_state.role:
        labels.append("si tu achètes ou tu produis")
    if not ob_state.name:
        labels.append("ton nom")
    if not ob_state.zone_id and not ob_state.coverage_status:
        labels.append("ta région")
    return labels


# Question dédiée et chaleureuse par champ manquant, avec un mini « pourquoi »
# qui donne du sens à la demande (au lieu d'un « Il me manque X » sec).
_FIELD_QUESTIONS: Dict[str, str] = {
    "role": (
        "Patron, dis-moi : tu es *producteur* (tu vends tes récoltes) "
        "ou *acheteur* (tu cherches à acheter) ? 🌾"
    ),
    "name": (
        "Comment tu t'appelles patron ? _(comme ça je te reconnais à chaque fois !)_"
    ),
    # (2026-09-18, retour produit) : demande la RÉGION (large) en premier réflexe — plus
    # facilement reconnaissable par l'utilisateur qu'une ville précise. Depuis le 2026-09-26,
    # une réponse plus précise ("Somgandé") n'est cependant plus jamais un motif de blocage :
    # voir `_resolve_zone`, qui accepte N'IMPORTE QUELLE localité et rattache/déclare hors
    # couverture plutôt que de redemander une "région valide".
    "zone": (
        "Tu es dans quelle *région* ? "
        "_(pour te connecter avec les meilleurs partenaires près de chez toi)_ 📍"
    ),
}


def _collect_prompt(ob_state: OnboardingState) -> str:
    ack = _ack_line(ob_state)
    missing = _missing_labels(ob_state)
    if not missing:
        return ack or "Merci !"

    # Un seul champ manquant → question dédiée, chaleureuse et guidante.
    if len(missing) == 1:
        key = "role" if not ob_state.role else "name" if not ob_state.name else "zone"
        question = _FIELD_QUESTIONS.get(key, f"Il me manque {missing[0]}.")
        return f"{ack} {question}".strip()

    # Plusieurs champs manquants → invitation légère à tout donner d'un coup.
    joined = ", ".join(missing[:-1]) + f" et {missing[-1]}"
    ask = (
        f"Patron, il me faut encore : {joined}. "
        "Tu peux tout m'écrire en une phrase, c'est facile ! 🙂"
    )
    return f"{ack} {ask}".strip()


async def run_onboarding_step(
    ob_state: OnboardingState,
    user_text: str,
    mcp_runtime: Any,
    *,
    extracted_entities: Optional[Dict[str, Any]] = None,
    llm_extract_all: Optional[BulkExtractor] = None,
    location_shared: bool = False,
) -> OnboardingResult:
    """One-shot slot-filling with free ordering and mid-flow corrections.

    Design principles :
    - ONE LLM call per turn extracts every field it can (role, name, zone, confirm).
    - No hardcoded acknowledgement tokens : the LLM alone decides YES/NO.
    - Any field can be updated at any time (even after "confirm ?") — a change
      invalidates the confirmation and re-asks it with the new summary.
    - The state machine is data-driven : ``step`` is derived from what is filled,
      not from a rigid sequence.
    - ``COLLECT_LOCATION`` is the sole EXCEPTION to free ordering : one single
      shot, always resolved to DONE on the very next turn regardless of
      content (fail-safe UX — see ``_step_collect_location``).
    """
    ob_state._filled_slots = {}
    text = (user_text or "").strip()

    if not isinstance(ob_state.step, OnboardingStep):
        ob_state.step = OnboardingStep.COLLECT_ROLE

    # Étape finale GPS : toujours résolue en un tour, quel que soit le
    # contenu du message (position partagée ou non) — jamais bloquante.
    if ob_state.step == OnboardingStep.COLLECT_LOCATION:
        return _step_collect_location(ob_state, location_shared=location_shared)

    # Welcome : cold start (no text yet, no data collected).
    if not text and not ob_state.role and not ob_state.name and not ob_state.zone_name:
        ob_state.step = OnboardingStep.COLLECT_ROLE
        return OnboardingResult(
            state=ob_state,
            response_text=_WELCOME,
            ag_ui_component={
                "type": "ChoiceComponent",
                "props": {
                    "title": "Tu es...",
                    "options": [
                        {
                            "label": "🛒 Acheteur — je cherche des produits",
                            "value": "BUYER",
                        },
                        {
                            "label": "🧑🏾‍🌾 Producteur — je vends mes récoltes",
                            "value": "PRODUCER",
                        },
                    ],
                },
            },
        )

    # Bulk extract : one shot, everything the LLM can pull from this turn.
    extracted: Dict[str, Optional[str]] = {
        "role": None,
        "name": None,
        "zone": None,
        "confirm": None,
    }
    entities = extracted_entities or {}
    if entities:
        extracted["role"] = entities.get("role")
        extracted["name"] = entities.get("name")
        extracted["zone"] = (
            entities.get("zone")
            or entities.get("zone_name")
            or entities.get("location")
        )

    # Confirmation DÉTERMINISTE en premier (mandat 2026-09-26, "'ok' ne confirme pas le
    # profil") — vocabulaire fermé CANONIQUE (`confirmation_phrases.py`, le MÊME que
    # `interpreter/routing.py::_interpret_fast_path` pour tous les autres flows, jamais un
    # second lexique propre à l'onboarding), consulté UNIQUEMENT quand une confirmation est
    # réellement active (`CONFIRM_DETAILS` — mandat §8 : "'ok' ne doit être interprété comme
    # confirmation QUE si une confirmation active existe"). Un match EXACT signifie que le
    # message entier n'est QUE cette phrase — rien d'autre à extraire, l'appel LLM est donc
    # sauté entièrement (même principe que le fast-path déterministe partout ailleurs : "un
    # oui exact ne doit jamais coûter un appel LLM").
    _confirmation_active = ob_state.step == OnboardingStep.CONFIRM_DETAILS
    _bare_text = text.strip(" .!?,;:").lower()
    _fast_confirm: Optional[str] = None
    if _confirmation_active:
        if _bare_text in _CONFIRM_EXACT_PHRASES:
            _fast_confirm = "YES"
        elif _bare_text in _REJECT_EXACT_PHRASES:
            _fast_confirm = "NO"

    is_question = False
    llm_reply: Optional[str] = None
    if _fast_confirm is not None:
        extracted["confirm"] = _fast_confirm
    elif text and llm_extract_all:
        try:
            llm_out = await llm_extract_all(text, _context_hint_for_llm(ob_state))
        except Exception:
            llm_out = {}
        for key in ("role", "name", "zone", "confirm"):
            if not extracted.get(key) and llm_out.get(key):
                extracted[key] = llm_out.get(key)
        is_question = bool(llm_out.get("is_question"))
        llm_reply = llm_out.get("reply") or None

    changed = _merge_extracted(ob_state, extracted)

    # Zone : résolue au plus UNE fois par valeur (voir la garde `coverage_status is None` —
    # sans elle, une localité déjà tranchée OUT_OF_COVERAGE relancerait une requête DB à
    # CHAQUE tour suivant). Mandat 2026-09-26 : JAMAIS de rejet sec bloquant — les 3 issues
    # de `_resolve_zone` (COVERED/NEARBY/OUT_OF_COVERAGE) laissent TOUJOURS l'onboarding
    # continuer dans le MÊME tour (voir `_ack_line` pour le message adapté à chacune).
    region_question: Optional[str] = None
    if ob_state.zone_name and not ob_state.zone_id and ob_state.coverage_status is None:
        declared = ob_state.zone_name
        resolution = await _resolve_zone(
            declared, mcp_runtime, allow_clarify=not ob_state.region_clarify_asked
        )
        if resolution.status == "NEEDS_REGION":
            # Région non déterminée : on la redemande (une fois), sans rien inventer.
            ob_state.region_clarify_asked = True
            ob_state.declared_location = declared
            ob_state.zone_name = None
            region_question = resolution.question
        else:
            ob_state.declared_location = resolution.declared or declared
            ob_state.coverage_status = resolution.status
            ob_state.zone_id = resolution.zone_id
            ob_state.zone_name = resolution.zone_name
            ob_state._filled_slots["zone"] = True

    confirm = extracted.get("confirm")
    # La zone est "réglée" dès que `coverage_status` est posé — COVERED/NEARBY (zone_id connu)
    # ET OUT_OF_COVERAGE (zone_id volontairement NULL) comptent également comme réglée :
    # l'invariant du mandat est que la COUVERTURE ne conditionne jamais la création du profil,
    # seulement les services qui en dépendront ensuite.
    all_present = bool(
        ob_state.role
        and ob_state.name
        and ob_state.phone
        and (ob_state.zone_id or ob_state.coverage_status)
    )

    # Create : all slots filled, explicit YES, nothing modified this turn.
    if all_present and confirm == "YES" and not changed:
        ob_state.step = OnboardingStep.CREATE_PROFILE
        return await _step_create_profile(ob_state, mcp_runtime)

    # Confirmation : slots complete but awaiting explicit YES (or a correction).
    if all_present:
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        summary = _build_confirmation_prompt(ob_state)
        if changed:
            prefix_parts = [_ack_line(ob_state), "J'ai mis a jour vos informations."]
            summary = " ".join(p for p in prefix_parts if p).strip() + " " + summary
        elif confirm == "NO":
            summary = (
                "Pas de souci — dites-moi ce que je dois corriger "
                "(le bon nom, le bon role ou la bonne zone). "
                f"Recapitulatif actuel : {summary}"
            )
        return OnboardingResult(
            state=ob_state,
            response_text=summary,
            ag_ui_component=_confirmation_component(),
        )

    # Still collecting : ask for whichever slot(s) remain, in a friendly free-order tone.
    ob_state.step = OnboardingStep.COLLECT_ROLE
    prompt = region_question or _collect_prompt(ob_state)
    if is_question:
        ob_state.explain_count += 1
        # Le LLM génère la réponse adaptée à CE message précis (voir
        # `_llm_extract_onboarding_all` / [[onboarding-adaptive-questions-2026-08]]) —
        # les textes canned ne sont qu'un filet de sécurité si l'appel LLM a
        # échoué (timeout, erreur, pas de client LLM injecté en test).
        explanation = llm_reply or (
            _EXPLAIN_AGAIN if ob_state.explain_count <= 1 else _EXPLAIN_AGAIN_SHORT
        )
        prompt = f"{explanation}\n\n{prompt}"
    return OnboardingResult(state=ob_state, response_text=prompt)


@dataclass(frozen=True)
class ZoneResolution:
    """Issue de `_resolve_zone` — jamais un simple booléen (mandat 2026-09-26) : la
    couverture a TROIS états distincts, pas juste trouvé/pas-trouvé."""

    #: "COVERED" (zone de service résolue directement), "NEARBY" (localité connue rattachée
    #: à une zone de service parente — `zone_id`/`zone_name` portent alors CETTE zone
    #: parente, jamais la localité elle-même), "OUT_OF_COVERAGE" (aucune correspondance à
    #: aucun niveau — `zone_id`/`zone_name` restent `None`).
    status: str
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    #: valeur CANONIQUE à stocker comme `declared_location` (nom de région) quand elle est connue.
    declared: Optional[str] = None
    #: statut "NEEDS_REGION" : question de relance à poser à la place de la suite.
    question: Optional[str] = None


async def _resolve_zone(
    declared_location: str, mcp_runtime: Any, *, allow_clarify: bool = False
) -> ZoneResolution:
    """Résout *declared_location* (le texte BRUT de l'utilisateur) en zone de SERVICE,
    JAMAIS en bloquant : voir `ZoneResolution` pour les 3 issues possibles.

    1. Zone RACINE (région) — comportement historique inchangé (`get_zone_by_name`,
       `parent_id IS NULL`, voir docs/ONBOARDING_ZONE_REGION_LEVEL_2026-09-18.md).
    2. Sinon, résolution HIÉRARCHIQUE (mandat 2026-09-26) : *declared_location* matche une
       localité connue à N'IMPORTE QUEL niveau (`get_zone_hierarchy_by_name`) dont la région
       racine EST une zone de service — rattachement automatique à cette région (ex: un
       quartier rattaché à sa ville-région englobante), JAMAIS deviné/halluciné : uniquement
       si cette hiérarchie existe déjà dans `governance.zones` (aucun nom de localité/région
       en dur ici — voir `test_zone_not_found_error_message_never_cites_hardcoded_city_
       examples`, qui verrouille précisément cette absence).
    3. Sinon, HORS COUVERTURE — jamais un rejet : `declared_location` reste seul de vérité,
       `zone_id` reste `None`, l'onboarding continue quand même (voir l'appelant)."""
    if not declared_location or mcp_runtime is None:
        return ZoneResolution(status="OUT_OF_COVERAGE")

    # (2026-10-05) Contrat d'onboarding : l'identité géographique est une des 17 RÉGIONS
    # (`domain/burkina_regions.py`, source unique). La valeur stockée est TOUJOURS le nom
    # canonique, quelle que soit la saisie (« Ouaga », « kadiogo », chef-lieu...). Le lien
    # vers `governance.zones` (opérationnel) se fait ensuite par le nom canonique, puis par le
    # chef-lieu (anciennes lignes racines nommées d'après la ville) ; sans ligne, `zone_id`
    # reste NULL mais la région est bien enregistrée — jamais un blocage.
    region_hit = resolve_region(declared_location)
    if region_hit.status == "RESOLVED" and region_hit.region is not None:
        region = region_hit.region
        for candidate in (region.name, region.capital):
            found = await _lookup_root_zone(candidate, mcp_runtime)
            if found and resolve_region(found[1]).region == region:
                return ZoneResolution(
                    status="COVERED", zone_id=found[0], zone_name=region.name, declared=region.name
                )
        return ZoneResolution(status="COVERED", zone_name=region.name, declared=region.name)

    if region_hit.status == "AMBIGUOUS":
        return _unresolved_region(declared_location, region_hit, allow_clarify)

    # Localité inconnue du référentiel des 17 régions : repli historique sur la DB (zone
    # racine, puis hiérarchie) — jamais deviné, uniquement si la hiérarchie existe déjà.
    found = await _lookup_root_zone(declared_location, mcp_runtime)
    if found:
        canon = resolve_region(found[1]).region
        return ZoneResolution(
            status="COVERED", zone_id=found[0], zone_name=canon.name if canon else found[1],
            declared=canon.name if canon else None,
        )

    try:
        hierarchy_response = await mcp_runtime.call_db(
            "get_zone_hierarchy_by_name", name=declared_location
        )
        payload = _unwrap_tool_payload(hierarchy_response)
        if isinstance(payload, dict) and payload.get("status") != "error":
            root = payload.get("root")
            if isinstance(root, dict) and root.get("id"):
                canon = resolve_region(root.get("name")).region
                return ZoneResolution(
                    status="NEARBY", zone_id=str(root["id"]),
                    zone_name=canon.name if canon else str(root.get("name") or ""),
                )
    except Exception as exc:
        logger.error(
            "[_resolve_zone] Exception (hiérarchie) pour '%s': %s",
            declared_location,
            exc,
            exc_info=True,
        )

    return _unresolved_region(declared_location, region_hit, allow_clarify)


async def _lookup_root_zone(name: str, mcp_runtime: Any) -> Optional[Tuple[str, str]]:
    """(id, libellé) de la zone RACINE `name` (`get_zone_by_name`), ou `None`."""
    try:
        zone_response = await mcp_runtime.call_db("get_zone_by_name", name=name)
        payload = _unwrap_tool_payload(zone_response)
        if isinstance(payload, dict) and payload.get("status") != "error":
            zone_id = payload.get("zone_id") or payload.get("id")
            label = payload.get("zone_name") or payload.get("name") or payload.get("label")
            if zone_id:
                return str(zone_id), str(label or name)
    except Exception as exc:
        logger.error("[_resolve_zone] Exception (région) pour '%s': %s", name, exc, exc_info=True)
    return None


def _unresolved_region(declared: str, hit: Any, allow_clarify: bool) -> ZoneResolution:
    """Localité non résolue en région : on REDEMANDE la région une seule fois (l'utilisateur ne
    recommence pas l'onboarding) ; s'il ne la donne toujours pas, hors couverture sans blocage."""
    if not allow_clarify:
        return ZoneResolution(status="OUT_OF_COVERAGE")
    if hit.status == "AMBIGUOUS":
        head = "Dans quelle *région* es-tu ? 📍"
    else:
        head = f"Dans quelle *région* se trouve *{str(declared).strip()}* ? 📍"
    return ZoneResolution(status="NEEDS_REGION", question=head + "\n_" + region_list_text() + "_")


def _confirmation_component() -> Dict[str, Any]:
    return {
        "type": "ChoiceComponent",
        "props": {
            "title": "On confirme patron ?",
            "options": [
                {"label": "✅ Oui, c'est bon !", "value": "OUI"},
                {"label": "✏️ Non, je corrige", "value": "NON"},
            ],
        },
    }


def _build_confirmation_prompt(ob_state: OnboardingState) -> str:
    name = ob_state.name or "(non renseigné)"
    role = ob_state.role or "(non renseigné)"
    role_label = (
        "Acheteur" if role == "BUYER" else "Producteur" if role == "PRODUCER" else role
    )
    if ob_state.zone_id and ob_state.zone_name:
        zone = ob_state.zone_name
    elif ob_state.declared_location:
        # OUT_OF_COVERAGE (mandat 2026-09-26) : la localité déclarée reste affichée telle
        # quelle — jamais une zone de service inventée pour "faire joli" dans le récap.
        zone = f"{ob_state.declared_location} (hors couverture actuelle)"
    else:
        zone = "(non renseignée)"
    return (
        f"Patron, voici ton récapitulatif :\n"
        f"👤 *Nom :* {name}\n"
        f"🏷️ *Rôle :* {role_label}\n"
        f"📍 *Zone :* {zone}\n\n"
        "Tout est bon ? Confirme et on est partis ! 🚀"
    )


def _step_collect_location(
    ob_state: OnboardingState, location_shared: bool
) -> OnboardingResult:
    """Étape finale, non-bloquante : la position GPS (si envoyée) a déjà été
    persistée par le webhook Twilio en tâche de fond — cette fonction ne fait
    que conclure l'onboarding et phraser l'accusé de réception, quel que soit
    le contenu du message reçu à ce tour."""
    ob_state.completed = True
    ob_state.step = OnboardingStep.DONE
    message = _LOCATION_THANKS if location_shared else _LOCATION_SKIPPED
    return OnboardingResult(state=ob_state, response_text=message, status="SUCCESS")


async def _step_create_profile(
    ob_state: OnboardingState,
    mcp_runtime: Any,
) -> OnboardingResult:
    if mcp_runtime is None:
        return OnboardingResult(
            state=ob_state,
            response_text="Service indisponible. Veuillez reessayer dans quelques instants.",
            status="ERROR",
        )

    if not ob_state.phone:
        ob_state.step = OnboardingStep.COLLECT_ROLE
        return OnboardingResult(
            state=ob_state,
            response_text="Impossible de creer votre profil sans numero de telephone. Veuillez reessayer.",
            status="ERROR",
        )

    if not ob_state.role:
        ob_state.step = OnboardingStep.COLLECT_ROLE
        return OnboardingResult(
            state=ob_state,
            response_text="Le rôle n'est pas défini. Êtes-vous acheteur ou producteur ?",
            status="ERROR",
        )

    create_payload = {
        "phone": ob_state.phone,
        "name": ob_state.name,
        "role": ob_state.role,
        "zone_id": ob_state.zone_id,
        "declared_location": ob_state.declared_location,
        # Mandat 2026-09-26 : jamais NULL en base une fois le profil créé — la zone a
        # forcément été traitée pour que `all_present` soit vrai (voir `run_onboarding_step`),
        # mais un défaut explicite protège quand même contre un état amont incohérent.
        "coverage_status": ob_state.coverage_status or "COVERED",
    }

    try:
        raw_result = await mcp_runtime.call_db(
            "create_user_profile", data=create_payload
        )

        # Parsing robuste (dict ou JSON string)
        if isinstance(raw_result, str):
            try:
                import ast

                result_dict = ast.literal_eval(raw_result)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                # `except:` NU auparavant : il interceptait AUSSI KeyboardInterrupt
                # et SystemExit, rendant le worker non-interruptible sur ce chemin.
                # On ne capte que ce que `literal_eval` peut réellement lever.
                result_dict = {}
        else:
            result_dict = raw_result if isinstance(raw_result, dict) else {}

        # 1. Cas Succès
        if result_dict.get("status") == "success" or "data" in result_dict:
            profile = await _reload_profile(mcp_runtime, ob_state.phone)
            ob_state.created_profile = profile
            # Ne pas conclure ici : dernière étape non-bloquante GPS avant DONE.
            ob_state.step = OnboardingStep.COLLECT_LOCATION
            ob_state.error = None
            return OnboardingResult(
                state=ob_state,
                response_text=(
                    f"✅ *Bienvenue patron {ob_state.name} !* Ton profil est prêt, "
                    f"tu fais maintenant partie de la communauté Ladini ! 🎉\n\n"
                    f"{_role_capabilities_message(ob_state.role)}\n\n"
                    f"{_LOCATION_PROMPT}"
                ),
                status="SUCCESS",
            )

        # 2. Cas Conflit (Téléphone déjà utilisé)
        error_msg = str(result_dict.get("message", "")).lower()
        if "already exists" in error_msg or "unique" in error_msg:
            profile = await _reload_profile(mcp_runtime, ob_state.phone)
            ob_state.created_profile = profile
            ob_state.step = OnboardingStep.COLLECT_LOCATION
            return OnboardingResult(
                state=ob_state,
                response_text=(
                    f"Content de te revoir patron *{ob_state.name}* ! 😊 "
                    f"Ton compte est déjà actif, on continue !\n\n"
                    f"{_role_capabilities_message(ob_state.role)}\n\n"
                    f"{_LOCATION_PROMPT}"
                ),
                status="SUCCESS",
            )

        # 3. Cas Erreur métier
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        return OnboardingResult(
            state=ob_state,
            response_text=f"Impossible de valider le profil : {result_dict.get('message', 'Erreur inconnue')}",
            status="ERROR",
        )

    except Exception:
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        return OnboardingResult(
            state=ob_state,
            response_text="Une erreur technique est survenue. Veuillez réessayer plus tard.",
            status="ERROR",
        )


async def _reload_profile(mcp_runtime: Any, phone: Optional[str]) -> Dict[str, Any]:
    if not phone or mcp_runtime is None:
        return {}
    try:
        data = await mcp_runtime.call_db("get_user_by_phone", phone=phone)
        if isinstance(data, dict):
            if data.get("data") and isinstance(data["data"], dict):
                return data["data"]
            return data
        return {}
    except Exception:
        return {}
