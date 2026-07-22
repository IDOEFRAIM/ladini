from typing import Any, Dict, List, Optional
import asyncio
import inspect
import re
import time
import unicodedata

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import ModerationGateway

logger = get_node_logger("SecurityModerationNode")


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


async def _check_account_gate(mc_runtime: MarketRuntime, phone: str) -> Optional[Dict[str, Any]]:
    """Bloque l'entrée si le compte est BLOCKED (annulations) ou BANNED (produits interdits)."""
    try:
        res = await ModerationGateway(mc_runtime).get_account_status(phone)
    except Exception as exc:  # dégradé : ne jamais bloquer un compte sain sur erreur technique
        logger.warning("[SecurityModeration] account gate failed: %s — laissé passer", exc)
        return None

    data = _unwrap(res)
    status = str(data.get("account_status") or "ACTIVE").upper()

    if status == "BANNED":
        return _blocked_patch(
            "🚫 Votre compte a été *banni* de la marketplace suite à des mentions "
            "répétées de produits interdits.\n\n"
            "Pour toute contestation, contactez le service client au *+226 01 47 98 00*."
        )
    if status == "BLOCKED":
        return _blocked_patch(
            "🔒 Votre compte est temporairement *bloqué* en raison d'annulations "
            "répétées de commandes.\n\n"
            "Pour débloquer votre compte, contactez le service client au *+226 01 47 98 00*."
        )
    return None


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


async def _get_prohibited_terms_cached(mc_runtime: MarketRuntime) -> Optional[List[Any]]:
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
                    phone=phone, matched_term=str(matched), excerpt=text, kind="PROHIBITED_PRODUCT",
                )
            )
            strikes = int(rec.get("strikes") or 0)
            banned = bool(rec.get("banned"))
        except Exception as exc:
            logger.warning("[SecurityModeration] record_strike failed: %s", exc)

    logger.warning("[SecurityModeration] PROHIBITED term=%r phone=%s strikes=%s banned=%s",
                   matched, phone, strikes, banned)

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

async def security_moderation(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Vérifie les risques de sécurité, l'état du compte et les produits interdits."""
    # Un blocage déjà décidé en amont (ex: profil indisponible côté normaliseur)
    # est respecté tel quel — on ne l'écrase pas.
    if str(state.get("status") or "").upper() == "BLOCKED" and state.get("final_response"):
        return {"security_status": state.get("security_status") or "PROFILE_UNAVAILABLE"}

    text = state.get("normalized_text") or state.get("user_query") or ""
    phone = str(state.get("user_phone") or "").strip()

    # 0. Gate d'entrée : compte bloqué (annulations) ou banni (produits interdits).
    if phone:
        gate = await _check_account_gate(mc_runtime, phone)
        if gate is not None:
            return gate

    if not text:
        return {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None}

    # 1. Produits interdits (déterministe, avant l'analyse scam LLM/keyword).
    prohibited = await _check_prohibited(mc_runtime, phone, text)
    if prohibited is not None:
        return prohibited

    # 2. Détection de scam (service existant).
    security = getattr(mc_runtime, "security", None)
    if security is None or not hasattr(security, "moderate_content"):
        return {"security_status": "SAFE", "trust_score": 0.8, "ag_ui_component": None}

    try:
        _call = security.moderate_content(text)
        if inspect.isawaitable(_call) or asyncio.iscoroutine(_call):
            raw = await asyncio.wait_for(_call, timeout=5.0)
        else:
            raw = _call
    except asyncio.TimeoutError:
        logger.warning("[SecurityModeration] moderate_content timed out — defaulting to SAFE (degraded)")
        return {"security_status": "SAFE", "trust_score": 0.3, "ag_ui_component": None}
    except Exception as mod_exc:
        logger.warning("[SecurityModeration] moderate_content failed: %s — defaulting to SAFE (degraded)", mod_exc)
        return {"security_status": "SAFE", "trust_score": 0.3, "ag_ui_component": None}

    if not isinstance(raw, dict):
        return {"security_status": "SAFE", "trust_score": 0.5, "ag_ui_component": None}

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

    return {"security_status": "SAFE", "trust_score": 1.0, "ag_ui_component": None}
