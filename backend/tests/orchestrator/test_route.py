import pytest
from unittest.mock import MagicMock, patch
from agriconnect.graphs.route import Router

## --- FIXTURES ---

@pytest.fixture
def mock_llm():
    """Crée un mock pour le SDK Groq/LLM"""
    llm = MagicMock()
    # Configuration par défaut d'une réponse JSON réussie
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(message=MagicMock(content='{"intent": "CHAT", "selected_experts": [], "reason": "politesse"}'))
    ]
    llm.chat.completions.create.return_value = mock_response
    return llm

@pytest.fixture
def router(mock_llm):
    return Router(llm=mock_llm, ctx=None)

## --- TESTS DE LOGIQUE DE ROUTAGE (Unitaires) ---

def test_map_agent_id_to_key_variants(router):
    """Vérifie que le mapping des IDs agents vers les clés internes est correct."""
    assert router._map_agent_id_to_key("formation_coach") == "formation"
    assert router._map_agent_id_to_key("marketplace_agent") == "marketplace"
    assert router._map_agent_id_to_key("climate_sentinel") == "sentinelle"
    assert router._map_agent_id_to_key("plant_doctor") == "sentinelle"
    assert router._map_agent_id_to_key("unknown_bot") is None

def test_route_flow_logic(router):
    """Vérifie la décision de noeud en fonction de l'état des besoins."""
    # Test Solo
    assert router.route_flow({"needs": {"selected_experts": ["market"]}}) == "SOLO_MARKET"
    # Test Parallèle
    state_multi = {"needs": {"selected_experts": ["market", "formation"], "intent": "COUNCIL"}}
    assert router.route_flow(state_multi) == "PARALLEL_EXPERTS"
    # Test Rejet
    assert router.route_flow({"needs": {"intent": "REJECT"}}) == "REJECT"

## --- TESTS D'ANALYSE AVEC HEURISTIQUES (Robustesse LLM) ---

def test_analyze_needs_heuristics_fallback_reason(router, mock_llm):
    """
    Scénario: Le LLM oublie 'selected_experts' mais mentionne l'expert dans 'reason'.
    Le code doit extraire 'formation' du texte.
    """
    mock_llm.chat.completions.create.return_value.choices[0].message.content = (
        '{"intent": "CHAT", "selected_experts": [], "reason": "L\'utilisateur veut une formation_coach"}'
    )
    
    state = {"requete_utilisateur": "Comment faire du compost ?"}
    out = router.analyze_needs(state)
    
    assert "formation" in out["needs"]["selected_experts"]
    # Vérifie que route_flow suit le mouvement
    assert router.route_flow(out) == "SOLO_FORMATION"

def test_analyze_needs_heuristics_fallback_query(router, mock_llm):
    """
    Scénario: Le LLM ne renvoie rien d'utile, mais les mots-clés de la requête 
    (ex: 'prix') déclenchent l'heuristique manuelle.
    """
    mock_llm.chat.completions.create.return_value.choices[0].message.content = (
        '{"intent": "CHAT", "selected_experts": []}'
    )
    
    state = {"requete_utilisateur": "Quel est le prix du maïs ?"}
    out = router.analyze_needs(state)
    
    # L'heuristique sur "prix" doit forcer "market"
    assert out["needs"]["selected_experts"] == ["market"]

def test_analyze_needs_polite_stay_chat(router, mock_llm):
    """
    Vérifie qu'un simple 'Bonjour' ne déclenche PAS d'expert par erreur.
    """
    mock_llm.chat.completions.create.return_value.choices[0].message.content = (
        '{"intent": "CHAT", "selected_experts": [], "reason": "greeting"}'
    )
    
    state = {"requete_utilisateur": "Bonjour AgriBot"}
    out = router.analyze_needs(state)
    
    assert out["needs"]["selected_experts"] in ([], None)
    assert router.route_flow(out) == "EXECUTE_CHAT"

## --- TESTS D'ERREUR ---

def test_analyze_needs_llm_failure(router, mock_llm):
    """Vérifie que le système ne crash pas si le LLM renvoie un JSON invalide."""
    mock_llm.chat.completions.create.side_effect = Exception("API Down")
    
    state = {"requete_utilisateur": "Aide moi"}
    out = router.analyze_needs(state)
    
    assert out["needs"]["intent"] == "REJECT"
    assert out["needs"]["reason"] == "error"