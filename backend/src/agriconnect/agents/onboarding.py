"""Unified onboarding — data-driven slot-filling with free ordering.

Design
------
- ONE LLM call per turn extracts every field it can (role, name, zone, confirm).
- No hardcoded acknowledgement lexicon: only the LLM decides YES/NO.
- Any slot can be updated at any moment, including after a confirmation
  prompt — a change re-triggers confirmation with the new summary.
- ``step`` is derived from what is filled, not from a rigid sequence.
"""
from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("AgriConnect.Agents.Onboarding")


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

    def missing_slots(self) -> List[str]:
        missing: List[str] = []
        if not self.role:
            missing.append("role")
        if not self.name:
            missing.append("name")
        if not self.zone_id and not self.zone_name:
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
        }
        if self.state.completed:
            updates["is_onboarding"] = False
            updates["onboarding_step"] = None
            updates["user_phone"] = self.state.phone
            updates["user_name"] = self.state.name
            updates["user_role"] = self.state.role
            updates["zone_name"] = self.state.zone_name
            updates["zone_id"] = self.state.zone_id
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
        if zone and zone.upper() != (ob_state.zone_name or "").upper():
            ob_state.zone_name = zone
            ob_state.zone_id = None
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


def _extract_zone_labels(raw: Any) -> List[str]:
    labels: List[str] = []

    def _handle_entry(entry: Any) -> None:
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("name")
        else:
            label = str(entry).strip()
        if label:
            labels.append(str(label).strip())

    def _walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                _walk(item)
            return
        if isinstance(obj, dict):
            for key in ("data", "zones", "items", "results"):
                val = obj.get(key)
                if isinstance(val, list):
                    _walk(val)
                    return
            _handle_entry(obj)
            return
        if isinstance(obj, str):
            stripped = obj.strip()
            if stripped.startswith(("[", "{")):
                try:
                    parsed = json.loads(stripped)
                    _walk(parsed)
                    return
                except Exception:
                    pass
                try:
                    parsed = ast.literal_eval(stripped)
                    _walk(parsed)
                    return
                except Exception:
                    pass
            parts = [p.strip() for p in stripped.split(",") if p.strip()]
            if len(parts) > 1:
                for part in parts:
                    _handle_entry(part)
            else:
                _handle_entry(stripped)
            return
        _handle_entry(obj)

    _walk(raw)
    return list(dict.fromkeys(labels))


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
    if ob_state.zone_name or ob_state.zone_id:
        known.append(f"zone={ob_state.zone_name or ob_state.zone_id}")
    missing = ", ".join(ob_state.missing_slots()) or "aucun"
    return (
        f"Deja connu : {', '.join(known) or 'rien'}. Encore manquant : {missing}. "
        f"Nombre de fois deja repondu a une question hors-sujet dans cette session : {ob_state.explain_count}."
    )


_WELCOME = (
    "🌾 *Bienvenue patron !* Je suis *LADINI*, ton assistant personnel sur *AgriConnect*.\n\n"
    "*AgriConnect*, c'est quoi ?* C'est ta plateforme agricole qui te connecte "
    "directement aux bons partenaires — acheteurs et producteurs — près de chez toi, "
    "par simple message WhatsApp. Pas de déplacement inutile, pas d'intermédiaire.\n\n"
    "🧑🏾‍🌾 *Tu produis ?* Publie tes récoltes, gère ton stock, reçois des commandes "
    "et fais connaître tes produits aux acheteurs de ta région.\n"
    "🛒 *Tu achètes ?* Trouve les meilleurs produits frais autour de toi, compare "
    "les prix et commande en quelques mots.\n\n"
    "Pour qu'on démarre ensemble, dis-moi : tu es *producteur* (tu vends) "
    "ou *acheteur* (tu achètes) ?\n\n"
    "_💡 Tu peux tout me dire d'un coup — par exemple « Je suis Awa, productrice "
    "à Bobo » — ou avancer étape par étape, comme tu préfères !_"
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
    "Bien sûr patron, je t'explique ! 😊 *AgriConnect*, c'est une plateforme "
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
    "AgriConnect est un vrai service gratuit, sans engagement — déjà "
    "utilisé par des producteurs et acheteurs près de chez toi. Donne-moi "
    "juste ton nom et ta ville pour avancer, tu peux toujours changer "
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
    if ob_state._filled_slots.get("zone") and ob_state.zone_name:
        parts.append(f"Zone notée : *{ob_state.zone_name}* ✅")
    return " ".join(parts)


def _missing_labels(ob_state: OnboardingState) -> List[str]:
    labels: List[str] = []
    if not ob_state.role:
        labels.append("si tu achètes ou tu produis")
    if not ob_state.name:
        labels.append("ton nom")
    if not ob_state.zone_id:
        labels.append("ta ville ou province")
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
    "zone": (
        "Tu es dans quelle *ville ou province* ? "
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

    is_question = False
    llm_reply: Optional[str] = None
    if text and llm_extract_all:
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

    # Zone : validate against catalog only if we have a name but no id yet.
    if ob_state.zone_name and not ob_state.zone_id:
        await _resolve_zone_id(ob_state, mcp_runtime)
        if not ob_state.zone_id:
            failed = ob_state.zone_name
            ob_state.zone_name = None
            hint = await _build_zone_catalog_hint(mcp_runtime)
            if hint:
                msg = (
                    f"Je n'ai pas trouve la zone '{failed}'. "
                    f"Merci d'indiquer une ville ou province valide.{hint}"
                )
            else:
                msg = (
                    f"Je n'ai pas pu verifier la zone '{failed}'. "
                    "Indiquez simplement votre ville ou province "
                    "(ex : Ouagadougou, Bobo-Dioulasso, Koudougou)."
                )
            ob_state.step = OnboardingStep.COLLECT_ROLE
            return OnboardingResult(state=ob_state, response_text=msg)

    confirm = extracted.get("confirm")
    all_present = bool(
        ob_state.role and ob_state.name and ob_state.zone_id and ob_state.phone
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
    prompt = _collect_prompt(ob_state)
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


async def _resolve_zone_id(ob_state: OnboardingState, mcp_runtime: Any) -> bool:
    if not ob_state.zone_name or ob_state.zone_id or mcp_runtime is None:
        return bool(ob_state.zone_id)

    try:
        zone_response = await mcp_runtime.call_db(
            "get_zone_by_name", name=ob_state.zone_name
        )
        logger.debug("[_resolve_zone_id] raw response: %s", zone_response)
        payload = _unwrap_tool_payload(zone_response)

        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("status") == "error":
                logger.warning(
                    "[_resolve_zone_id] MCP error for '%s': %s",
                    ob_state.zone_name,
                    payload.get("error") or payload.get("message"),
                )
                return False
            zone_id = payload.get("zone_id") or payload.get("id")
            zone_label = (
                payload.get("zone_name") or payload.get("name") or payload.get("label")
            )

            if zone_id:
                ob_state.zone_id = str(zone_id)
            if zone_label:
                ob_state.zone_name = str(zone_label)
    except Exception as exc:
        logger.error(
            "[_resolve_zone_id] Exception for '%s': %s",
            ob_state.zone_name,
            exc,
            exc_info=True,
        )
        return False

    return bool(ob_state.zone_id)


async def _build_zone_catalog_hint(mcp_runtime: Any) -> str:
    if mcp_runtime is None:
        return ""

    try:
        zone_catalog = await mcp_runtime.call_db("get_available_zones") or []
        # NB: on n'appelle PAS `_unwrap_tool_payload` ici — elle est conçue
        # pour dérouler un objet UNIQUE (`Optional[Dict]`) et, sur une
        # liste, retourne dès que le PREMIER élément se déroule avec succès
        # au lieu de la liste entière. Sur une réponse `get_available_zones`
        # (plusieurs zones), ça ne gardait que la toute première ville de la
        # DB — d'où l'incident "l'agent ne cite que Bobo Dioulasso comme
        # zone valide" (2026-08-26). `_extract_zone_labels` déroule déjà
        # correctement l'enveloppe {"data": [...]} elle-même, sur la liste
        # complète.
        labels = _extract_zone_labels(zone_catalog)
        if labels:
            sample = ", ".join(labels[:10])
            return f" Zones valides : {sample}."
    except Exception as exc:
        logger.warning("[_build_zone_catalog_hint] %s", exc)
        return ""

    return ""


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
    zone = ob_state.zone_name or "(non renseignée)"
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
                    f"tu fais maintenant partie de la communauté AgriConnect ! 🎉\n\n"
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
