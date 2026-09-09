import asyncio
import inspect
import re
import time
import unicodedata
from enum import Enum
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import (
    ModerationGateway,
)
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime

logger = get_node_logger("SecurityModerationNode")

# =====================================================================
# security_moderation — question UNIQUE : « cette entrée peut-elle
# continuer vers l'interprétation métier ? » (2026-09-08, refonte
# responsabilités des nœuds d'entrée). Ce nœud est désormais le
# propriétaire UNIQUE de la décision de sécurité conversationnelle — voir
# `SecurityDecision` ci-dessous. Le texte brut/normalisé n'est JAMAIS
# modifié ici (contrairement à l'ancien comportement d'`input_normalizer`,
# qui remplaçait le texte par un faux prompt système sur détection
# d'injection — supprimé, voir §5 du mandat de refonte : les regex ne sont
# que des SIGNAUX déterministes, pas une vérité sémantique absolue ni un
# substitut du message utilisateur).
# =====================================================================


class SecurityDecision(str, Enum):
    """Ensemble fermé — contrat minimal `decision/reason/flags` du mandat
    (§5). RESTRICT est déclaré pour le contrat mais n'a aujourd'hui aucun
    émetteur réel (tous les cas existants sont ALLOW ou BLOCK) — réservé
    pour une future dégradation partielle (ex: throttling) plutôt qu'un
    blocage dur."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    RESTRICT = "RESTRICT"


# Signaux déterministes de détournement de contexte — CONSERVÉS tels quels
# (déplacés depuis `input_normalizer`, mandat §5 : "les regex existantes
# peuvent être conservées comme signaux déterministes"). Vérifiés sur le
# texte NORMALISÉ (jamais muté par ce nœud).
_CONTEXT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all|every)\s+previous\s+instructions", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:admin|administrator|system)", re.IGNORECASE),
    re.compile(r"reset\s+the\s+guardrails", re.IGNORECASE),
    re.compile(r"disable\s+(?:security|moderation)", re.IGNORECASE),
)


def _detect_context_injection(text: str) -> Optional[str]:
    if not text:
        return None
    for pattern in _CONTEXT_INJECTION_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def _injection_blocked_patch(raw_query: Optional[str]) -> Dict[str, Any]:
    """BLOCK sur détection d'injection — ne touche NI `normalized_text` NI
    `translated_text` (mandat §5 : le texte utilisateur n'est jamais
    remplacé). `blocked_user_query` conserve la trace forensique brute
    (voir `core/state.py`, déjà déclaré pour survivre au checkpoint)."""
    return {
        "security_status": "PROMPT_INJECTION_DETECTED",
        "trust_score": 0.0,
        "status": "BLOCKED",
        "response_strategy": "ERROR",
        "final_response": (
            "🚫 Je n'exécute pas d'instructions système. Reformulez votre besoin métier."
        ),
        "ag_ui_component": None,
        "blocked_user_query": raw_query,
    }


# ── Helpers ──────────────────────────────────────────────────────────


def _unwrap(res: Any) -> Dict[str, Any]:
    """Déballe l'enveloppe outil {ok,data,...} → dict métier plat."""
    if not isinstance(res, dict):
        return {}
    if "account_status" in res or "terms" in res or "strikes" in res or "status" in res:
        return res
    data = res.get("data")
    if isinstance(data, dict):
        return data
    return res


def _fold(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_only.lower().strip()


def _with_degradation(
    patch: Dict[str, Any], degraded_reason: Optional[str]
) -> Dict[str, Any]:
    """Rend le mode fail-open OBSERVABLE (2026-09-08, clôture Bloc 1,
    mandat §6) plutôt que de le laisser vivre seulement dans un log. N'est
    JAMAIS appliqué sur un patch de BLOCAGE — un blocage est déjà un signal
    fort en lui-même, pas la peine de le nuancer. `security_degraded` reste
    délibérément NON branché vers `mcp_tool_executor` (mandat : "ne branche
    PAS encore ce signal vers l'executor" — cette dette est explicitement
    reprise lors de l'audit confirmation/executor)."""
    if not degraded_reason:
        return patch
    return {
        **patch,
        "security_degraded": True,
        "security_degraded_reason": degraded_reason,
    }


def _blocked_patch(message: str) -> Dict[str, Any]:
    """Court-circuit dur : compte bloqué/banni — aucune opération n'est traitée."""
    return {
        "security_status": "ACCOUNT_BLOCKED",
        "trust_score": 0.0,
        "status": "BLOCKED",
        "response_strategy": "ERROR",
        "final_response": message,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "StatusComponent"],
            "kwargs": {"type": "error", "reason": "Compte restreint"},
        },
    }


async def _check_account_gate(
    mc_runtime: MarketRuntime, phone: str
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Bloque l'entrée si le compte est BLOCKED (annulations) ou BANNED
    (produits interdits).

    Retourne `(block_patch, degraded_reason)` — `degraded_reason` non-None
    signale un FAIL-OPEN délibéré (2026-09-08, clôture Bloc 1, mandat §6) :
    la panne de cette passerelle ne bloque jamais un compte potentiellement
    sain (dégradation acceptée), mais ne doit plus non plus être INVISIBLE —
    voir `security_degraded`/`security_degraded_reason` dans
    `_security_moderation_impl`. Documenté explicitement : un ALLOW
    conversationnel en mode dégradé ne vaut PAS autorisation d'exécuter une
    mutation sensible — cette distinction sera reprise lors de l'audit
    confirmation/executor (Bloc transactionnel), ce signal n'est PAS encore
    branché vers l'exécuteur ici."""
    try:
        res = await ModerationGateway(mc_runtime).get_account_status(phone)
    except (
        Exception
    ) as exc:  # dégradé : ne jamais bloquer un compte sain sur erreur technique
        logger.warning(
            "[SecurityModeration] account gate failed: %s — laissé passer (dégradé)", exc
        )
        return None, "ACCOUNT_GATE_UNAVAILABLE"

    data = _unwrap(res)
    status = str(data.get("account_status") or "ACTIVE").upper()

    if status == "BANNED":
        return (
            _blocked_patch(
                "🚫 Votre compte a été *banni* de la marketplace suite à des mentions "
                "répétées de produits interdits.\n\n"
                "Pour toute contestation, contactez le service client au *+226 01 47 98 00*."
            ),
            None,
        )
    if status == "BLOCKED":
        return (
            _blocked_patch(
                "🔒 Votre compte est temporairement *bloqué* en raison d'annulations "
                "répétées de commandes.\n\n"
                "Pour débloquer votre compte, contactez le service client au *+226 01 47 98 00*."
            ),
            None,
        )
    return None, None


def _match_prohibited(text: str, terms: list[str]) -> Optional[str]:
    """Retourne le premier terme interdit présent dans le texte (matching replié)."""
    folded = _fold(text)
    if not folded:
        return None
    words = set(re.findall(r"[a-z0-9]+", folded))
    for term in terms:
        t = _fold(term)
        if not t:
            continue
        if " " in t:
            if t in folded:
                return term
        elif t in words:
            return term
    return None


# Cache client des termes interdits (Phase 4) : la liste est globale et quasi
# statique (le serveur la cache déjà 300s côté DB) — sans ce cache, CHAQUE
# message payait un aller-retour MCP complet (stdio inter-processus en prod).
# NB : le gate compte (`get_account_status`) reste volontairement NON caché —
# un blocage/bannissement doit s'appliquer au message suivant.
_TERMS_CACHE_TTL_SECONDS = 300.0
_terms_cache: Dict[str, Any] = {"at": 0.0, "terms": None}


async def _get_prohibited_terms_cached(
    mc_runtime: MarketRuntime,
) -> Optional[List[Any]]:
    now = time.monotonic()
    cached = _terms_cache["terms"]
    if cached is not None and (now - _terms_cache["at"]) < _TERMS_CACHE_TTL_SECONDS:
        return cached
    try:
        terms_res = _unwrap(await ModerationGateway(mc_runtime).get_prohibited_terms())
    except Exception as exc:
        logger.warning("[SecurityModeration] prohibited terms fetch failed: %s", exc)
        # Secours : un cache périmé vaut mieux qu'aucun filtre du tout.
        return cached
    terms = terms_res.get("terms") or []
    if isinstance(terms, list) and terms:
        _terms_cache["terms"] = terms
        _terms_cache["at"] = now
        return terms
    return cached


async def _check_prohibited(
    mc_runtime: MarketRuntime, phone: str, text: str
) -> Optional[Dict[str, Any]]:
    """Détecte un produit interdit ; journalise un strike et bannit au-delà du seuil."""
    terms = await _get_prohibited_terms_cached(mc_runtime)
    if not isinstance(terms, list) or not terms:
        return None

    matched = _match_prohibited(text, terms)
    if not matched:
        return None

    # Enregistrement du strike (best-effort) + décision de bannissement.
    strikes = 0
    banned = False
    if phone:
        try:
            rec = _unwrap(
                await ModerationGateway(mc_runtime).record_moderation_strike(
                    phone=phone,
                    matched_term=str(matched),
                    excerpt=text,
                    kind="PROHIBITED_PRODUCT",
                )
            )
            strikes = int(rec.get("strikes") or 0)
            banned = bool(rec.get("banned"))
        except Exception as exc:
            logger.warning("[SecurityModeration] record_strike failed: %s", exc)

    logger.warning(
        "[SecurityModeration] PROHIBITED term=%r phone=%s strikes=%s banned=%s",
        matched,
        phone,
        strikes,
        banned,
    )

    if banned:
        return _blocked_patch(
            "🚫 Votre compte a été *banni* : les produits interdits (drogues, armes, etc.) "
            "n'ont pas leur place sur AgriConnect.\n\n"
            "Pour toute contestation, contactez le service client au *+226 01 47 98 00*."
        )

    # Avertissement transparent : on indique où en est l'utilisateur (N/3) avant
    # bannissement (déclenché au-delà de 3 mentions).
    limit = 3
    warn_line = (
        f"⚠️ Avertissement *{strikes}/{limit}* — au-delà, votre compte sera "
        "*banni définitivement*."
        if strikes
        else "⚠️ Toute nouvelle mention rapproche votre compte d'un *bannissement*."
    )
    return {
        "security_status": "PROHIBITED_PRODUCT",
        "trust_score": 0.0,
        "status": "BLOCKED",
        "response_strategy": "ERROR",
        "final_response": (
            "⛔ Ce produit est *interdit* sur AgriConnect et ne peut pas être échangé.\n\n"
            f"{warn_line}"
        ),
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "StatusComponent"],
            "kwargs": {"type": "error", "reason": "Produit interdit"},
        },
    }


# ── Node ─────────────────────────────────────────────────────────────


def _decision_for(state: Dict[str, Any], patch: Dict[str, Any]) -> SecurityDecision:
    """Traduit CE tour vers le vocabulaire fermé `SecurityDecision`.

    (2026-09-08, revue de validation) : lit l'état EFFECTIF après patch
    (`patch.get("status", state.get("status"))`), pas seulement le patch
    brut — le chemin "blocage déjà décidé en amont" (`_security_moderation_impl`,
    1er early-return) ne réaffirme délibérément PAS `status` dans son
    propre patch (déjà `BLOCKED` dans `state` via le reducer du nœud
    précédent, `replace_value` n'a pas besoin d'être répété) ; une lecture
    qui ne regarderait QUE `patch` classerait ce cas à tort en `ALLOW`."""
    effective_status = patch.get("status", state.get("status"))
    if str(effective_status or "").upper() == "BLOCKED":
        return SecurityDecision.BLOCK
    return SecurityDecision.ALLOW


async def security_moderation(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Vérifie les risques de sécurité, l'état du compte et les produits
    interdits — point d'entrée public. Ajoute `security_decision`
    (vocabulaire fermé `SecurityDecision`) au patch retourné : c'est le
    SEUL champ que le routeur (`nodes/routing.py::_route_after_security`)
    a besoin de lire (mandat de revue, Invariant C — routeur trivial, un
    seul champ, aucune reclassification)."""
    patch = await _security_moderation_impl(state, mc_runtime)
    decision = _decision_for(state, patch)
    patch = {**patch, "security_decision": decision.value}
    logger.debug(
        "[SecurityModeration] decision=%s security_status=%s",
        decision.value,
        patch.get("security_status"),
    )
    return patch


async def _security_moderation_impl(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    # (2026-09-08, clôture Bloc 1, mandat §5) : ce nœud est désormais le
    # PREMIER maillon décisionnel du tour (`input_normalizer -> security_
    # moderation -> session_bootstrap`) — `PROFILE_UNAVAILABLE` est posé
    # PAR `session_bootstrap`, qui s'exécute APRÈS ce nœud, jamais avant.
    # Un `status=="BLOCKED"` déjà présent ici ne peut donc plus venir du
    # chemin nominal du graphe compilé — ce garde reste un repli DÉFENSIF
    # pour un appel hors de ce chemin (ancien checkpoint persisté avant ce
    # correctif, appel direct de ce nœud dans un test/script, future couche
    # appelante qui poserait `status` en amont) : on respecte tel quel un
    # blocage déjà décidé plutôt que de l'écraser, sans supposer QUI l'a posé.
    if str(state.get("status") or "").upper() == "BLOCKED" and state.get(
        "final_response"
    ):
        return {
            "security_status": state.get("security_status") or "PROFILE_UNAVAILABLE"
        }

    text = state.get("normalized_text") or state.get("user_query") or ""
    phone = str(state.get("user_phone") or "").strip()

    # 0. Détournement de contexte (prompt injection) — vérifié EN PREMIER,
    # déterministe, sans réseau. Signal, pas un remplacement de texte.
    injection_trigger = _detect_context_injection(text)
    if injection_trigger:
        logger.warning(
            "[SecurityModeration] Prompt injection détectée | trigger=%s | phone=%s",
            injection_trigger,
            phone[-4:] if phone else "?",
        )
        return _injection_blocked_patch(state.get("user_query") or text)

    # (2026-09-08, clôture Bloc 1, mandat §6 — "formaliser la dette security
    # fail-open") : ce nœud reste délibérément fail-open sur panne externe
    # (ne jamais bloquer un utilisateur sain pour une panne technique) —
    # mais ce mode dégradé était auparavant INVISIBLE (un simple log). Un
    # ALLOW conversationnel en mode dégradé ne vaut PAS autorisation
    # d'exécuter une mutation sensible : cette distinction est du ressort de
    # l'audit confirmation/executor (Bloc transactionnel, hors périmètre
    # ici) — ce nœud se contente de rendre le signal OBSERVABLE.
    degraded_reason: Optional[str] = None

    # 1. Gate d'entrée : compte bloqué (annulations) ou banni (produits interdits).
    if phone:
        gate, gate_degraded = await _check_account_gate(mc_runtime, phone)
        if gate is not None:
            return gate
        degraded_reason = gate_degraded

    if not text:
        return _with_degradation(
            {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None},
            degraded_reason,
        )

    # 2. Produits interdits (déterministe, avant l'analyse scam LLM/keyword).
    prohibited = await _check_prohibited(mc_runtime, phone, text)
    if prohibited is not None:
        return prohibited

    # 3. Détection de scam (service existant).
    security = getattr(mc_runtime, "security", None)
    if security is None or not hasattr(security, "moderate_content"):
        return _with_degradation(
            {"security_status": "SAFE", "trust_score": 0.8, "ag_ui_component": None},
            degraded_reason,
        )

    try:
        _call = security.moderate_content(text)
        if inspect.isawaitable(_call) or asyncio.iscoroutine(_call):
            raw = await asyncio.wait_for(_call, timeout=5.0)
        else:
            raw = _call
    except asyncio.TimeoutError:
        logger.warning(
            "[SecurityModeration] moderate_content timed out — defaulting to SAFE (degraded)"
        )
        return _with_degradation(
            {"security_status": "SAFE", "trust_score": 0.3, "ag_ui_component": None},
            "MODERATION_TIMEOUT",
        )
    except Exception as mod_exc:
        logger.warning(
            "[SecurityModeration] moderate_content failed: %s — defaulting to SAFE (degraded)",
            mod_exc,
        )
        return _with_degradation(
            {"security_status": "SAFE", "trust_score": 0.3, "ag_ui_component": None},
            "MODERATION_UNAVAILABLE",
        )

    if not isinstance(raw, dict):
        return _with_degradation(
            {"security_status": "SAFE", "trust_score": 0.5, "ag_ui_component": None},
            degraded_reason,
        )

    status_raw = str(raw.get("status") or "").upper()
    is_scam = bool(raw.get("is_scam"))
    if is_scam or status_raw in {"SCAM_DETECTED", "BLOCKED"}:
        return {
            "security_status": "SCAM_DETECTED",
            "security_reason": raw.get("reason") or "Contenu suspect détecté.",
            "trust_score": 0.0,
            "status": "BLOCKED",
            "response_strategy": "ERROR",
            "final_response": (
                "Désolé, votre message contient des éléments suspects. "
                "Pour votre sécurité, je ne peux pas continuer cette opération."
            ),
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "StatusComponent"],
                "kwargs": {"type": "error", "reason": "Contenu suspect détecté"},
            },
        }

    return _with_degradation(
        {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None},
        degraded_reason,
    )
