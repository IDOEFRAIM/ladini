"""Lecture d'UN slot de profil attendu (nom d'affichage, région) — extracteur STRUCTURÉ, jamais une réplique.

Quand Ladini vient de demander « quel nom dois-je utiliser ? », le message suivant est interprété D'ABORD comme une réponse
possible à CE slot (contexte demandé + extraction structurée + validation) — pas par l'ancien extracteur d'inscription, qui
cherchait « un vrai nom de personne » et répondait lui-même avec le discours de création de compte.

L'extracteur ne renvoie que des FAITS : `{kind, value, confidence}` —

    ANSWER        le message répond au slot ; `value` = la valeur seule (« Restau chez Zouba », sans « c'est »/« mon nom c'est »)
    INTERRUPTION  l'utilisateur change de sujet (prix, produits…) : jamais forcé comme un nom
    REFUSAL       il ne veut pas / plus tard
    QUESTION      il demande pourquoi
    UNCLEAR       rien d'exploitable

La RÉGION se résout d'abord de façon déterministe (17 régions, chefs-lieux, surnoms — `domain/burkina_regions.py`) ; le LLM n'est
consulté que si cela ne suffit pas. Le NOM est un nom d'affichage : une personne OU un établissement (restaurant, hôtel,
société…). Aucune liste de phrases : le contexte du slot attendu guide le modèle ; le repli SANS LLM est volontairement strict.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from ladini.domain.burkina_regions import resolve_region
from ladini.domain.profile_requirements import ProfileField, is_placeholder_display_name

logger = logging.getLogger("Ladini.Market.ProfileSlot")

ANSWER, INTERRUPTION, REFUSAL, QUESTION, UNCLEAR = "ANSWER", "INTERRUPTION", "REFUSAL", "QUESTION", "UNCLEAR"
_KINDS = {ANSWER, INTERRUPTION, REFUSAL, QUESTION, UNCLEAR}
_MAX_NAME_LENGTH = 80


@dataclass(frozen=True)
class SlotReading:
    kind: str
    value: Optional[str] = None
    confidence: float = 0.0
    #: "deterministic" (région), "llm", ou "fallback" (LLM indisponible).
    source: str = "llm"


_EXPECTED = {
    ProfileField.NAME: (
        "un NOM D'AFFICHAGE : le nom d'une personne OU d'un établissement (restaurant, hôtel, société, coopérative, "
        "boutique…), avec ou sans mots d'introduction (« c'est », « moi c'est », « mon nom c'est »). Renvoie UNIQUEMENT le "
        "nom, tel que dit, sans les mots d'introduction ; garde le type d'établissement s'il fait partie du nom "
        "(« Restau chez Zouba » reste « Restau chez Zouba »)."
    ),
    ProfileField.REGION: (
        "une RÉGION ou une localité du Burkina Faso (ex. Kadiogo, Guiriko, Ouagadougou, Bobo). Renvoie la localité dite, sans "
        "les mots d'introduction (« je suis à », « dans le »)."
    ),
}

_SYSTEM_PROMPT = (
    "SLOT DE PROFIL — Ladini vient de poser UNE question précise à l'utilisateur et attend la valeur d'un champ. Tu dis si son "
    "message y répond, et si oui laquelle.\n\n"
    "Champ attendu : {expected}\n\n"
    "Réponds STRICTEMENT en JSON : {{\"kind\": \"ANSWER\"|\"INTERRUPTION\"|\"REFUSAL\"|\"QUESTION\"|\"UNCLEAR\", "
    "\"value\": string|null, \"confidence\": nombre entre 0 et 1}}\n"
    "- ANSWER : le message donne la valeur du champ attendu (value = la valeur seule).\n"
    "- INTERRUPTION : l'utilisateur demande/dit autre chose (voir des prix, des produits, changer de sujet) — value=null.\n"
    "- REFUSAL : il refuse de donner l'information ou veut le faire plus tard — value=null.\n"
    "- QUESTION : il demande pourquoi/à quoi ça sert — value=null.\n"
    "- UNCLEAR : rien d'exploitable — value=null.\n"
    "N'invente JAMAIS une valeur ; en cas de doute, UNCLEAR. Ne rédige aucune réponse à l'utilisateur."
)

_INTRO = re.compile(
    r"^(?:bonjour|salut|bonsoir)?[\s,;:!.-]*(?:moi\s+c['’ ]?est|je\s+m['’ ]?appelle|mon\s+nom\s+(?:c['’ ]?est|est)|c['’ ]?est|je\s+suis)\s+",
    re.IGNORECASE,
)


def _strip_intro(text: str) -> str:
    return _INTRO.sub("", (text or "").strip()).strip(" .!,;:")


def _valid_name(candidate: Any) -> Optional[str]:
    value = str(candidate or "").strip().strip(" .!,;:\"'")
    if not value or len(value) > _MAX_NAME_LENGTH or "?" in value or is_placeholder_display_name(value):
        return None
    return value


def _fallback_name(text: str) -> SlotReading:
    """SANS LLM : une réponse courte, uniquement des lettres (traits d'union/apostrophes DANS un mot) — jamais une phrase."""
    cleaned = _strip_intro(text)
    tokens = cleaned.split()
    introduced = cleaned != (text or "").strip(" .!,;:")
    # Sans LLM, rien ne distingue « plus tard » de « Wend Konta » par le sens : on n'accepte que ce qui a la FORME d'un nom
    # dit — un seul mot, une introduction explicite (« c'est … »), ou des mots tous en majuscule initiale. Le reste est
    # redemandé (jamais pris pour un nom).
    if len(tokens) > 1 and not introduced and not all(t[:1].isupper() for t in tokens):
        return SlotReading(UNCLEAR, source="fallback")
    if not cleaned or len(tokens) > 5 or not all(re.fullmatch(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", t) for t in tokens):
        return SlotReading(UNCLEAR, source="fallback")
    value = _valid_name(cleaned)
    return SlotReading(ANSWER, value, 0.5, "fallback") if value else SlotReading(UNCLEAR, source="fallback")


async def _ask_llm(mc_runtime: Any, text: str, field: ProfileField, question: str) -> Optional[dict[str, Any]]:
    if getattr(mc_runtime, "llm", None) is None:
        return None
    try:
        from ladini.graphs.agents.market_coach.llm_gateway import (
            resolve_gateway,
            resolve_profile,
        )

        completion = await resolve_gateway(mc_runtime).complete(
            profile=resolve_profile(mc_runtime),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT.format(expected=_EXPECTED[field])},
                {"role": "user", "content": f"Question posée : {question}\nMessage utilisateur : \"\"\"{text.strip()}\"\"\""},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=120,
            agent_node="profile_slot",
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001 - repli déterministe strict
        logger.warning("PROFILE_SLOT_LLM_ERROR | %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


async def read_profile_slot(mc_runtime: Any, text: str, field: ProfileField, *, question: str = "") -> SlotReading:
    """Interprète `text` comme réponse au slot `field`. Jamais d'effet de bord ; jamais une réplique conversationnelle."""
    text = (text or "").strip()
    if not text:
        return SlotReading(UNCLEAR)

    if field is ProfileField.REGION:
        hit = resolve_region(text)
        if hit.status == "RESOLVED" and hit.region is not None:
            return SlotReading(ANSWER, hit.region.name, 1.0, "deterministic")

    payload = await _ask_llm(mc_runtime, text, field, question)
    if payload is not None and str(payload.get("kind") or "").upper() not in _KINDS:
        payload = None  # réponse hors contrat (pas de `kind` valide) : même repli strict que sans modèle
    if payload is None:
        return _fallback_name(text) if field is ProfileField.NAME else SlotReading(UNCLEAR, source="fallback")

    kind = str(payload.get("kind") or "").upper()
    kind = kind if kind in _KINDS else UNCLEAR
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    value = _valid_name(payload.get("value")) if (kind == ANSWER and field is ProfileField.NAME) else (
        str(payload.get("value") or "").strip() or None if kind == ANSWER else None
    )
    if kind == ANSWER and value is None:
        kind = UNCLEAR
    logger.info("profile_gate_field_extracted | field=%s | kind=%s | confidence=%.2f", field.value, kind, confidence)
    return SlotReading(kind, value, confidence, "llm")


__all__ = ["ANSWER", "INTERRUPTION", "QUESTION", "REFUSAL", "UNCLEAR", "SlotReading", "read_profile_slot"]
