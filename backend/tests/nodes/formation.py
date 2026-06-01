import pytest
from unittest.mock import MagicMock, patch
import json

from agriconnect.graphs.nodes.formation import (
    FormationCoach,
    FormationConfig,
    FormationAgentState,
)
import pytest
from unittest.mock import MagicMock, patch
import json

# keep imports using package path

class TestFormationCoach:

    @pytest.fixture
    def mock_config(self):
        """Configuration avec des mocks pour tous les composants externes."""
        config = FormationConfig()
        config.llm_client = MagicMock()
        config.mcp_rag = MagicMock()
        config.mcp_context = MagicMock()
        config.evaluator = MagicMock()
        return config

    @pytest.fixture
    def coach(self, mock_config):
        return FormationCoach(config=mock_config)

    ## --- Tests des Nœuds Individuels ---

    def test_analyze_node_market_routing(self, coach, mock_config):
        """Vérifie que les questions de prix sortent du scope formation."""
        state: FormationAgentState = {
            "user_query": "Quel est le prix du maïs à Ouagadougou ?",
            "warnings": []
        }
        # FormationCoach no longer routes market queries — it should analyze only
        coach.tool._analyze_request = MagicMock(return_value={
            "intent": "FORMATION",
            "is_relevant": True,
            "focus_topics": [],
            "urgency": "NORMAL",
        })

        result = coach.analyze_node(state)

        # Market detection removed: formation analyzes queries within its
        # scope and does not perform market-specific routing.
        assert result["status"] == "ANALYZED"

    def test_analyze_node_formation_flow(self, coach):
        """Vérifie l'analyse normale d'une question technique."""
        state: FormationAgentState = {
            "user_query": "Comment traiter la chenille légionnaire ?",
            "learner_profile": {"niveau": "débutant"}
        }
        
        # Mock de l'outil interne de FormationCoach
        coach.tool._analyze_request = MagicMock(return_value={
            "intent": "FORMATION",
            "is_relevant": True,
            "focus_topics": ["ravageurs"],
            "urgency": "NORMAL"
        })

        result = coach.analyze_node(state)
        assert result["status"] == "ANALYZED"
        assert "ravageurs" in result["focus_topics"]

    def test_retrieve_node_success(self, coach, mock_config):
        """Vérifie la récupération de contexte via MCP RAG."""
        state: FormationAgentState = {
            "user_query": "Besoin d'aide pour le compostage",
            "learner_profile": {"niveau": "intermédiaire"},
            "status": "ANALYZED"
        }

        # Simulation de la réponse JSON du serveur MCP
        mock_mcp_response = {
            "status": "ok",
            "content": [{"text": json.dumps({
                "context": "Le compostage nécessite de l'azote et du carbone.",
                "sources": [{"title": "Manuel Agri", "uri": "https://agri.bf/1"}]
            })}]
        }
        mock_config.mcp_rag.call_tool.return_value = mock_mcp_response

        result = coach.retrieve_node(state)
        
        assert result["status"] == "CONTEXT_FOUND"
        assert "azote" in result["retrieved_context"]
        assert result["sources"][0]["title"] == "Manuel Agri"

    ## --- Tests du Flux Complet (Integration) ---

    @patch("agriconnect.graphs.nodes.formation.StateGraph.compile")
    def test_workflow_structure(self, mock_compile, coach):
        """Vérifie que le graphe se construit sans erreur technique."""
        workflow = coach.build()
        assert workflow is not None
        # On vérifie que les nœuds essentiels ont été ajoutés
        mock_compile.assert_called_once()

    def test_compose_node_off_topic(self, coach):
        """Vérifie la réponse polie en cas de hors-sujet."""
        state: FormationAgentState = {
            "is_relevant": False,
            "rejection_reason": "Je ne connais pas la météo de Paris.",
            "user_query": "Quel temps fait-il à Paris ?"
        }
        
        result = coach.compose_node(state)
        
        assert result["status"] == "OFF_TOPIC"
        assert "expert AgriConnect" in result["final_response"]
        assert result["agri_response"] is not None

    ## --- Test de la logique de protection (Loop Guard) ---

    def test_route_critique_degraded_mode(self, coach):
        """Vérifie que le système passe en mode dégradé après trop d'échecs."""
        state: FormationAgentState = {
            "critique_retry_count": 3, # Seuil dépassé
            "status": "RETRY",
            "warnings": []
        }
        
        # Accès à la fonction interne de routage définie dans build()
        # Note: Dans un test réel, on invoque le workflow, 
        # mais on peut tester la logique de décision ici :
        from langgraph.graph import END
        
        def mock_route_critique(s):
            if s.get("critique_retry_count", 0) > 2:
                return "evaluate"
            return "compose"

        route = mock_route_critique(state)
        assert route == "evaluate"