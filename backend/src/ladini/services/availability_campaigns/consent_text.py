"""Détection DÉTERMINISTE (sans LLM, sans I/O) d'un arrêt / d'un consentement dans un message entrant.

Ce n'est PAS un routeur conversationnel : c'est une étape de conformité (comme la garde de maintenance) qui
s'applique AVANT le graphe, sur des familles de formulations fermées. Elle est volontairement prudente :

* un mot d'arrêt nu (« stop », « arrêt », « désinscription »…) est une désinscription (convention WhatsApp) ;
* une phrase d'arrêt doit viser les ENVOIS (« arrêtez de m'envoyer… », « je ne veux plus recevoir… ») — « arrêtez la
  commande » n'est JAMAIS une désinscription ;
* un consentement exige une demande explicite de recevoir les disponibilités, ou un « oui » à la question de
  consentement (décidé par l'appelant : ce module ne sait pas ce qui a été envoyé avant).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

OPT_OUT = "OPT_OUT"
OPT_IN_REQUEST = "OPT_IN_REQUEST"
YES = "YES"


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[’'`]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_BARE_STOP = {
    "stop", "stop stop", "arret", "arrete", "arretez", "stoppez", "stoppe",
    "desinscription", "desinscrire", "desinscris moi", "desinscrivez moi",
    "desabonnement", "desabonner", "desabonnez moi", "desabonne moi", "unsubscribe",
    "annuler l abonnement", "annuler abonnement",
}

# Verbe d'arrêt + cible « envois » : exige les deux.
_STOP_VERB = r"(?:arret\w*|stopp?\w*|cesse\w*|plus de|ne plus|n envoyez plus|ne m envoyez plus|ne veux plus|veux plus|desinscri\w*|desabonn\w*)"
_SENDING_TARGET = r"(?:m envoyer|envoy\w*|recevoir|messages?|notifications?|annonces?|disponibilites?|offres?|promos?|publicites?|sms|whatsapp)"
_STOP_PHRASE = re.compile(rf"\b{_STOP_VERB}\b.*\b{_SENDING_TARGET}\b|\b{_SENDING_TARGET}\b.*\b{_STOP_VERB}\b")
_NOT_STOP_CONTEXT = re.compile(r"\b(commande|livraison|paiement|panier|achat|precommande|offre de prix)\b")

_OPT_IN_PHRASE = re.compile(
    r"\b(?:je veux|je voudrais|j aimerais|envoyez moi|envoie moi|abonnez moi|inscrivez moi|je souhaite|veuillez m)\b.*"
    r"\b(?:recevoir|disponibilites?|offres?)\b.*\b(?:disponibilites?|offres?|produits?|annonces?|messages?)\b"
    r"|\b(?:abonnez moi|inscrivez moi|abonne moi|inscris moi)\b"
    r"|\bje m abonne\b|\bje m inscris\b"
)

_YES_WORDS = {
    "oui", "ouais", "ok", "okay", "d accord", "dacord", "daccord", "yes", "oui merci", "oui je veux", "oui svp",
    "oui s il vous plait", "oui volontiers", "avec plaisir", "volontiers", "je veux bien", "oui d accord",
}


def classify(text: str) -> Optional[str]:
    """`OPT_OUT` / `OPT_IN_REQUEST` / `YES` / None. `YES` n'est qu'un « oui » nu : à n'interpréter comme un
    consentement que si la question de consentement vient d'être posée (décision de l'appelant)."""
    n = _norm(text)
    if not n or len(n.split()) > 14:
        return None
    if n in _BARE_STOP:
        return OPT_OUT
    if _STOP_PHRASE.search(n) and not _NOT_STOP_CONTEXT.search(n):
        return OPT_OUT
    if _OPT_IN_PHRASE.search(n):
        return OPT_IN_REQUEST
    if n in _YES_WORDS:
        return YES
    return None


OPT_OUT_CONFIRMATION = (
    "C'est noté : vous ne recevrez plus nos messages de disponibilités. "
    "Si vous vouliez annuler une commande en cours, écrivez « annuler ». "
    "Pour les recevoir de nouveau, écrivez « je veux recevoir les disponibilités »."
)
OPT_IN_CONFIRMATION = (
    "Merci, c'est enregistré : vous recevrez de temps en temps les disponibilités du moment. "
    "Écrivez STOP à tout moment pour ne plus les recevoir."
)
CONSENT_QUESTION = (
    "Souhaitez-vous recevoir de temps en temps les disponibilités de produits agricoles sur LADINI ? "
    "Répondez « oui » pour accepter. Vous pourrez écrire STOP à tout moment."
)
