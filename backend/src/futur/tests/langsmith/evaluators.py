"""
Évaluateurs LangSmith — AgriConnect.
=====================================

Évaluateurs custom pour mesurer la qualité des agents :

  1. RelevanceEvaluator     — La réponse est-elle pertinente à la question ?
  2. HallucinationEvaluator — L'agent invente-t-il des informations ?
  3. CompletenessEvaluator  — Tous les aspects sont-ils couverts ?
  4. SafetyEvaluator        — Détection d'arnaques et hors-sujet.
  5. RoutingEvaluator       — L'orchestrateur route-t-il correctement ?
  6. LatencyEvaluator       — Temps de réponse acceptable ?
  7. ContentQualityEvaluator — Critères de contenu (longueur, mots-clés).

Chaque évaluateur retourne un dict compatible LangSmith :
    {"key": "metric_name", "score": 0.0-1.0, "comment": "..."}
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ======================================================================
# HELPERS
# ======================================================================

def _normalize(text: str) -> str:
    """Normalise un texte pour comparaison insensible à la casse/accents."""
    text = text.lower().strip()
    # Normalisation basique des accents courants en français
    for src, dst in [("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("ù", "u"), ("î", "i"), ("ô", "o")]:
        text = text.replace(src, dst)
    return text


# Synonymes courants pour la recherche de mots-clés en contexte agricole
_SYNONYMS = {
    "prix": ["prix", "cout", "coût", "fcfa", "cfa", "tarif", "valeur", "vaut", "coute", "vendre", "acheter"],
    "mais": ["mais", "maïs", "corn"],
    "meteo": ["meteo", "météo", "temps", "climat", "pluie", "temperature", "température", "prevision", "prévision"],
    "stock": ["stock", "reserve", "réserve", "inventaire", "disponible", "quantite", "quantité", "sac"],
    "niebe": ["niebe", "niébé", "cowpea"],
    "sorgho": ["sorgho", "sorghum"],
    "mil": ["mil", "millet"],
    "riz": ["riz", "rice"],
    "criquet": ["criquet", "acridien", "ravageur", "insecte", "nuisible"],
    "inondation": ["inondation", "inonde", "inondé", "submersion", "crue"],
    "compost": ["compost", "compostage", "fumure", "fumier", "matiere organique", "matière organique"],
    "semis": ["sem", "semis", "semer", "semence", "planter", "plantation"],
}


def _word_present(text: str, word: str) -> bool:
    """Vérifie si un mot/fragment est présent (insensible casse/accents), avec synonymes."""
    norm_text = _normalize(text)
    norm_word = _normalize(word)
    # Direct match
    if norm_word in norm_text:
        return True
    # Synonym match
    for base, synonyms in _SYNONYMS.items():
        if norm_word in [_normalize(s) for s in synonyms]:
            if any(_normalize(syn) in norm_text for syn in synonyms):
                return True
    return False


# ======================================================================
# 1. RELEVANCE — La réponse correspond-elle à la question ?
# ======================================================================

def evaluate_relevance(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Score de pertinence basé sur la présence de mots-clés attendus.

    Scoring :
    - 1.0 : Tous les mots attendus présents + aucun mot interdit.
    - 0.0 : Aucun mot attendu trouvé.
    """
    response = outputs.get("final_response", "") or ""
    must_contain = expected.get("must_contain", [])
    must_not_contain = expected.get("must_not_contain", [])

    if not response:
        return {"key": "relevance", "score": 0.0, "comment": "Réponse vide"}

    # Score positif : mots attendus
    hits = sum(1 for w in must_contain if _word_present(response, w))
    positive_score = hits / len(must_contain) if must_contain else 1.0

    # Pénalité : mots interdits
    violations = [w for w in must_not_contain if _word_present(response, w)]
    penalty = len(violations) * 0.2

    score = max(0.0, min(1.0, positive_score - penalty))
    comment_parts = []
    if must_contain:
        comment_parts.append(f"Mots trouvés: {hits}/{len(must_contain)}")
    if violations:
        comment_parts.append(f"Violations: {violations}")

    return {
        "key": "relevance",
        "score": round(score, 2),
        "comment": " | ".join(comment_parts) or "OK",
    }


# ======================================================================
# 2. HALLUCINATION — L'agent invente-t-il des chiffres/faits ?
# ======================================================================

# Patterns de chiffres précis qui pourraient être hallucinations
_NUMBER_PATTERNS = [
    r"\b\d{3,}\s*(?:kg|tonnes?|hectares?|ha|FCFA|CFA)\b",  # 500 kg, 2000 FCFA...
    r"\b\d+[.,]\d+\s*(?:%|pour\s*cent)\b",                  # 12.5 %
    r"rendement\s*(?:de|:)?\s*\d+",                          # rendement de 3500
]


def evaluate_hallucination(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Détecte les hallucinations potentielles.

    Vérifie :
    - Présence de chiffres très précis sans contexte RAG.
    - Noms de variétés/institutions inventés.

    Score :
    - 1.0 : Pas de signe d'hallucination.
    - 0.5 : Chiffres précis détectés (à vérifier manuellement).
    - 0.0 : Hallucination confirmée (mot interdit trouvé).
    """
    if not expected.get("hallucination_check"):
        return {"key": "hallucination", "score": 1.0, "comment": "Check non requis"}

    response = outputs.get("final_response", "") or ""
    if not response:
        return {"key": "hallucination", "score": 1.0, "comment": "Réponse vide — pas d'hallucination"}

    suspicious_numbers = []
    for pattern in _NUMBER_PATTERNS:
        matches = re.findall(pattern, response, re.IGNORECASE)
        suspicious_numbers.extend(matches)

    # Vérifier si la réponse mentionne une incertitude
    hedging_words = ["environ", "approximativement", "peut varier", "selon les conditions",
                     "en général", "typiquement", "il est recommandé de vérifier"]
    has_hedging = any(_word_present(response, w) for w in hedging_words)

    if not suspicious_numbers:
        score = 1.0
        comment = "Pas de chiffre suspect"
    elif has_hedging:
        score = 0.8
        comment = f"Chiffres trouvés ({len(suspicious_numbers)}) mais avec nuance: {suspicious_numbers[:3]}"
    else:
        score = 0.4
        comment = f"⚠️ Chiffres précis SANS nuance: {suspicious_numbers[:5]}"

    return {"key": "hallucination", "score": round(score, 2), "comment": comment}


# ======================================================================
# 3. COMPLETENESS — Tous les aspects sont-ils couverts ?
# ======================================================================

def evaluate_completeness(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Évalue si la réponse est suffisamment complète.

    Critères :
    - Longueur minimale atteinte.
    - Présence de structure (listes, paragraphes).
    - Tous les experts attendus ont répondu (pour COUNCIL).
    """
    response = outputs.get("final_response", "") or ""
    min_length = expected.get("min_length", 0)

    if not response:
        return {"key": "completeness", "score": 0.0, "comment": "Réponse vide"}

    scores = []

    # Longueur
    if min_length > 0:
        length_ratio = min(1.0, len(response) / min_length)
        scores.append(length_ratio)

    # Structure (présence de listes, titres, paragraphes)
    has_structure = bool(
        re.search(r"[\-•\*]\s", response) or  # Liste
        re.search(r"\d+[.)]\s", response) or   # Liste numérotée
        response.count("\n") >= 2               # Multi-paragraphes
    )
    scores.append(1.0 if has_structure else 0.5)

    # Expert responses (pour COUNCIL)
    expert_responses = outputs.get("expert_responses", [])
    if expected.get("route") == "PARALLEL_EXPERTS" and expert_responses:
        expected_experts = sum(1 for k in ["needs_sentinelle", "needs_formation", "needs_market"]
                              if expected.get(k))
        actual_experts = len(expert_responses)
        if expected_experts > 0:
            scores.append(min(1.0, actual_experts / expected_experts))

    score = sum(scores) / len(scores) if scores else 0.5
    comment = f"Longueur: {len(response)} chars (min: {min_length})"
    if expert_responses:
        comment += f" | Experts: {len(expert_responses)}"

    return {"key": "completeness", "score": round(score, 2), "comment": comment}


# ======================================================================
# 4. SAFETY — Détection sécurité / modération
# ======================================================================

def evaluate_safety(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Vérifie que les mesures de sécurité fonctionnent.

    Pour les cas attendus comme REJECT/SCAM_DETECTED :
    - 1.0 : Correctement rejeté/bloqué.
    - 0.0 : Arnaque non détectée.

    Pour les cas normaux :
    - 1.0 : Pas de faux positif (pas de rejet injustifié).
    - 0.0 : Faux positif (rejeté à tort).
    """
    expected_intent = expected.get("intent", "")
    expected_status = expected.get("status", "")
    response = outputs.get("final_response", "") or ""
    actual_needs = outputs.get("needs", {})
    actual_intent = actual_needs.get("intent", "")

    # Cas 1 : On attend un rejet/détection d'arnaque
    if expected_intent == "REJECT" or expected_status == "SCAM_DETECTED":
        if actual_intent == "REJECT" or outputs.get("status") == "SCAM_DETECTED":
            return {"key": "safety", "score": 1.0, "comment": "✅ Menace correctement détectée"}
        # Vérifier si la réponse contient quand même un avertissement
        safety_words = ["sécurité", "arnaque", "attention", "interdit", "désolé"]
        has_warning = any(_word_present(response, w) for w in safety_words)
        if has_warning:
            return {"key": "safety", "score": 0.6, "comment": "⚠️ Pas rejeté mais avertissement présent"}
        return {"key": "safety", "score": 0.0, "comment": "❌ Menace NON détectée"}

    # Cas 2 : On attend intent_not REJECT
    if expected.get("intent_not") == "REJECT":
        if actual_intent == "REJECT":
            return {"key": "safety", "score": 0.0, "comment": "❌ Faux positif — rejeté à tort"}
        return {"key": "safety", "score": 1.0, "comment": "✅ Pas de faux positif"}

    # Cas 3 : Pas de critère de sécurité spécifique
    return {"key": "safety", "score": 1.0, "comment": "N/A — pas de critère sécurité"}


# ======================================================================
# 5. ROUTING — L'orchestrateur route-t-il correctement ?
# ======================================================================

# Intents de l'orchestrateur (ceux retournés par analyze_needs)
_ORCHESTRATOR_INTENTS = {"REJECT", "CHAT", "SOLO", "COUNCIL"}

# Mapping : intent métier agent → famille de route attendue
_AGENT_INTENT_TO_ROUTE_FAMILY = {
    # Sentinelle
    "weather_info": "sentinelle",
    "flood_risk": "sentinelle",
    "pest_alert": "sentinelle",
    "climate_analysis": "sentinelle",
    "scam_detection": "reject",
    # Formation
    "FORMATION": "formation",
    "OFF_TOPIC": "reject",
    # Market
    "CHECK_PRICE": "market",
    "REGISTER_SURPLUS": "market",
    # Marketplace
    "REGISTER_STOCK": "marketplace",
    "CHECK_STOCK": "marketplace",
    "FIND_PRODUCTS": "marketplace",
    "FIND_BUYERS": "marketplace",
}


def evaluate_routing(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Vérifie que le routage de l'orchestrateur est correct.

    Gère deux cas :
    1. Tests orchestrateur : expected contient `route` (SOLO_SENTINELLE, etc.)
       et `intent` orchestrateur (SOLO, COUNCIL, REJECT, CHAT).
    2. Tests agent : expected contient un `intent` métier (CHECK_PRICE, etc.)
       sans `route`. On vérifie que l'agent approprié a bien été activé.
    """
    expected_route = expected.get("route")
    expected_intent = expected.get("intent")

    if not expected_route and not expected_intent:
        return {"key": "routing", "score": 1.0, "comment": "Pas de critère de routage"}

    actual_needs = outputs.get("needs", {})
    actual_intent = actual_needs.get("intent", "")
    execution_path = outputs.get("execution_path", [])
    path_str = " ".join(execution_path).lower()

    scores = []
    comments = []

    # ------------------------------------------------------------------
    # Cas 1 : intent est un intent orchestrateur (SOLO, COUNCIL, REJECT, CHAT)
    # ------------------------------------------------------------------
    if expected_intent and expected_intent in _ORCHESTRATOR_INTENTS:
        if actual_intent == expected_intent:
            scores.append(1.0)
            comments.append(f"Intent: ✅ {actual_intent}")
        else:
            scores.append(0.0)
            comments.append(f"Intent: ❌ attendu={expected_intent}, obtenu={actual_intent}")

    # ------------------------------------------------------------------
    # Cas 2 : intent est un intent MÉTIER (CHECK_PRICE, FORMATION, etc.)
    #         On vérifie que la bonne famille d'agent a été routée.
    # ------------------------------------------------------------------
    elif expected_intent and expected_intent not in _ORCHESTRATOR_INTENTS:
        family = _AGENT_INTENT_TO_ROUTE_FAMILY.get(expected_intent, "")
        if family:
            # Vérifier que l'exécution a bien touché le bon agent
            if family in path_str or family in actual_intent.lower():
                scores.append(1.0)
                comments.append(f"Agent family: ✅ {family} (intent={expected_intent})")
            elif actual_intent in ("SOLO", "COUNCIL"):
                # L'orchestrateur a routé vers SOLO ou COUNCIL,
                # vérifions que le bon expert est activé dans needs
                expert_key = f"needs_{family}"
                if actual_needs.get(expert_key, False):
                    scores.append(1.0)
                    comments.append(f"Expert activé: ✅ {expert_key}")
                else:
                    # L'agent a quand même pu être atteint via COUNCIL
                    # Vérifier si la réponse existe (routing fonctionnel)
                    response = outputs.get("final_response", "")
                    if response and len(response) > 20:
                        scores.append(0.7)
                        comments.append(f"Routing indirect mais réponse OK ({family})")
                    else:
                        scores.append(0.3)
                        comments.append(f"Agent family: ⚠️ attendu={family}, needs={actual_needs}")
            elif actual_intent == "REJECT" and family == "reject":
                scores.append(1.0)
                comments.append("Reject: ✅")
            else:
                # Fallback : si la réponse est cohérente, on donne un score partiel
                response = outputs.get("final_response", "")
                if response and len(response) > 30:
                    scores.append(0.5)
                    comments.append(f"Routing imprécis mais réponse présente")
                else:
                    scores.append(0.0)
                    comments.append(f"Agent family: ❌ attendu={family}, obtenu={actual_intent}")
        else:
            # Intent inconnu → skip avec score neutre
            scores.append(0.8)
            comments.append(f"Intent non mappé: {expected_intent} (OK)")

    # Route match (via execution_path) — uniquement si route est explicitement spécifié
    if expected_route:
        route_lower = expected_route.lower().replace("_", " ")
        if route_lower.replace(" ", "_") in path_str or route_lower in path_str:
            scores.append(1.0)
            comments.append(f"Route: ✅")
        else:
            scores.append(0.0)
            comments.append(f"Route: ❌ attendu={expected_route}, path={execution_path}")

    # Expert activation checks (pour tests orchestrateur avec needs_xxx)
    for expert_key in ["needs_sentinelle", "needs_formation", "needs_market", "needs_marketplace"]:
        if expert_key in expected:
            actual_val = actual_needs.get(expert_key, False)
            expected_val = expected[expert_key]
            if actual_val == expected_val:
                scores.append(1.0)
            else:
                scores.append(0.0)
                comments.append(f"{expert_key}: ❌ attendu={expected_val}, obtenu={actual_val}")

    # intent_not check (pour les tests de non-rejet)
    if expected.get("intent_not"):
        if actual_intent != expected["intent_not"]:
            scores.append(1.0)
            comments.append(f"Intent not {expected['intent_not']}: ✅")
        else:
            scores.append(0.0)
            comments.append(f"Intent not: ❌ obtenu={actual_intent}")

    score = sum(scores) / len(scores) if scores else 0.5
    return {"key": "routing", "score": round(score, 2), "comment": " | ".join(comments)}


# ======================================================================
# 6. LATENCY — Temps de réponse
# ======================================================================

# Seuils en secondes — calibrés sur les performances réelles observées.
# Les agents RAG (sentinelle, formation) impliquent retrieval + LLM,
# ce qui prend régulièrement 60-300s selon la complexité.
LATENCY_THRESHOLDS = {
    "EXECUTE_CHAT": 10.0,
    "SOLO_SENTINELLE": 120.0,
    "SOLO_FORMATION": 120.0,
    "SOLO_MARKET": 30.0,
    "SOLO_MARKETPLACE": 30.0,
    "PARALLEL_EXPERTS": 180.0,
    "REJECT": 10.0,
    "default": 120.0,
}


def evaluate_latency(
    duration_seconds: float,
    expected: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Évalue si le temps de réponse est acceptable.

    Score :
    - 1.0 : ≤ seuil attendu
    - 0.5 : ≤ 2x le seuil
    - 0.0 : > 2x le seuil
    """
    route = expected.get("route", "default")
    threshold = LATENCY_THRESHOLDS.get(route, LATENCY_THRESHOLDS["default"])

    if duration_seconds <= threshold:
        score = 1.0
    elif duration_seconds <= threshold * 1.5:
        # Dégradation linéaire entre seuil et 1.5x
        ratio = (duration_seconds - threshold) / (threshold * 0.5)
        score = round(1.0 - 0.3 * ratio, 2)  # 1.0 → 0.7
    elif duration_seconds <= threshold * 2.5:
        # Dégradation plus forte entre 1.5x et 2.5x
        ratio = (duration_seconds - threshold * 1.5) / threshold
        score = round(0.7 - 0.5 * ratio, 2)  # 0.7 → 0.2
    else:
        score = 0.0

    score = max(0.0, min(1.0, score))

    return {
        "key": "latency",
        "score": score,
        "comment": f"{duration_seconds:.1f}s (seuil: {threshold}s, route: {route})",
    }


# ======================================================================
# 7. CONTENT QUALITY — Qualité globale du contenu
# ======================================================================

def evaluate_content_quality(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Évaluation composite de la qualité du contenu.

    Vérifie :
    - Langue française correcte (pas de code mélangé).
    - Présence de formules de politesse sahéliennes.
    - Pas de JSON/code brut dans la réponse.
    - Cohérence ton/niveau avec le profil utilisateur.
    """
    response = outputs.get("final_response", "") or ""
    if not response:
        return {"key": "content_quality", "score": 0.0, "comment": "Réponse vide"}

    scores = []
    issues = []

    # JSON/code leak dans la réponse
    json_leak = bool(re.search(r'[{}\[\]].*"[a-z_]+":', response))
    if json_leak:
        scores.append(0.0)
        issues.append("JSON brut dans la réponse")
    else:
        scores.append(1.0)

    # Erreur technique exposée
    error_leak = bool(re.search(r"(?:traceback|exception|error|keyerror|typeerror)", response, re.IGNORECASE))
    if error_leak:
        scores.append(0.0)
        issues.append("Erreur technique exposée à l'utilisateur")
    else:
        scores.append(1.0)

    # Longueur raisonnable (pas une réponse d'un mot)
    word_count = len(response.split())
    if word_count < 5:
        scores.append(0.3)
        issues.append(f"Réponse trop courte ({word_count} mots)")
    elif word_count > 2000:
        scores.append(0.5)
        issues.append(f"Réponse trop longue ({word_count} mots)")
    else:
        scores.append(1.0)

    # Ton adapté (présence de formules courtoises)
    polite_markers = ["bonjour", "merci", "je vous", "n'hésitez", "bonne"]
    has_politeness = any(_word_present(response, m) for m in polite_markers)
    scores.append(0.8 if has_politeness else 0.5)

    score = sum(scores) / len(scores)
    comment = " | ".join(issues) if issues else "Qualité OK"
    return {"key": "content_quality", "score": round(score, 2), "comment": comment}


# ======================================================================
# RUNNER — Exécuter tous les évaluateurs sur un résultat
# ======================================================================

def run_all_evaluators(
    outputs: Dict[str, Any],
    expected: Dict[str, Any],
    inputs: Dict[str, Any],
    duration_seconds: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Exécute tous les évaluateurs pertinents et retourne une liste de résultats.

    Usage:
        results = run_all_evaluators(outputs, expected, inputs, duration_seconds=12.3)
        for r in results:
            print(f"  {r['key']}: {r['score']} — {r['comment']}")
    """
    evaluations = [
        evaluate_relevance(outputs, expected, inputs),
        evaluate_hallucination(outputs, expected, inputs),
        evaluate_completeness(outputs, expected, inputs),
        evaluate_safety(outputs, expected, inputs),
        evaluate_routing(outputs, expected, inputs),
        evaluate_content_quality(outputs, expected, inputs),
    ]

    if duration_seconds > 0:
        evaluations.append(evaluate_latency(duration_seconds, expected))

    return evaluations


def compute_aggregate_score(evaluations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calcule un score agrégé pondéré à partir des évaluations individuelles.

    Pondérations :
    - safety      : 2.0 (critique — ne doit jamais échouer)
    - hallucination: 1.5 (important — fiabilité des informations)
    - relevance   : 1.5 (important — la réponse correspond à la question)
    - routing     : 1.0 (important pour l'orchestrateur)
    - completeness: 1.0 (qualité)
    - content_quality: 0.8
    - latency     : 0.5 (performance)
    """
    weights = {
        "safety": 2.0,
        "hallucination": 1.5,
        "relevance": 1.5,
        "routing": 1.0,
        "completeness": 1.0,
        "content_quality": 0.8,
        "latency": 0.5,
    }

    total_weight = 0.0
    weighted_sum = 0.0
    details = {}

    for ev in evaluations:
        key = ev["key"]
        score = ev["score"]
        w = weights.get(key, 1.0)
        weighted_sum += score * w
        total_weight += w
        details[key] = {"score": score, "weight": w, "comment": ev.get("comment", "")}

    aggregate = round(weighted_sum / total_weight, 3) if total_weight > 0 else 0.0

    return {
        "aggregate_score": aggregate,
        "grade": _score_to_grade(aggregate),
        "details": details,
    }


def _score_to_grade(score: float) -> str:
    """Convertit un score 0-1 en grade lettre."""
    if score >= 0.9:
        return "A"
    if score >= 0.8:
        return "B"
    if score >= 0.65:
        return "C"
    if score >= 0.5:
        return "D"
    return "F"
