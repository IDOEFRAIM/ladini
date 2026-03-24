import asyncio
import logging
import json
import time
import numpy as np
from typing import Optional, Dict, Any, List, Tuple
from agriconnect.core.settings import settings
from agriconnect.core.get_llm import get_llm
from agriconnect.agents.formation_agro import FormationAgro

logger = logging.getLogger("AgriOrchestrator")

# Optional: semantic cache dependency
try:
    from sentence_transformers import SentenceTransformer
    HAS_SEMANTIC_LIB = True
except ImportError:
    HAS_SEMANTIC_LIB = False


class AgriOrchestrator:
    """
    Central Orchestrator responsible for:
    1. Fast Routing (Intent Classification) using a lightweight LLM.
    2. Dispatching to specialized Agents (Formation, Market, etc.).
    3. Caching responses (Semantic + Exact) to reduce latency.
    4. Managing parallel execution of tasks.
    """

    def __init__(self):
        self.llm = get_llm()
        self.formation_agent = FormationAgro(llm_client=self.llm)
        self.router_model = "llama-3.1-8b-instant"  # Fast model for routing
        
        # Cache setup
        self._cache: List[Dict[str, Any]] = [] # List for semantic iteration
        self._cache_ttl = 600  # 10 minutes (user suggested GPTCache logic)
        self._similarity_threshold = 0.92

        self.embedder = None
        if HAS_SEMANTIC_LIB:
            try:
                # Load lightweight embedding model for caching
                self.embedder = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("Semantic Cache initialized with all-MiniLM-L6-v2")
            except Exception as e:
                logger.warning(f"Could not load embedding model for cache: {e}")

    def _get_embedding(self, text: str) -> Optional[np.ndarray]:
        if self.embedder:
            try:
                # synchronous encode, fast for single sentence
                return self.embedder.encode(text)
            except Exception:
                return None
        return None

    def _check_cache(self, query: str) -> Optional[Dict[str, Any]]:
        """Check cache for exact or semantic match."""
        now = time.time()
        
        # Cleanup expired (lazy)
        self._cache = [c for c in self._cache if now - c["timestamp"] < self._cache_ttl]

        query_embedding = self._get_embedding(query)

        best_match = None
        best_score = 0.0

        for entry in self._cache:
            # 1. Exact match (fastest)
            if entry["query"] == query:
                return entry

            # 2. Semantic match
            if query_embedding is not None and entry.get("embedding") is not None:
                # Cosine similarity
                vec_a = query_embedding
                vec_b = entry["embedding"]
                norm_a = np.linalg.norm(vec_a)
                norm_b = np.linalg.norm(vec_b)
                if norm_a > 0 and norm_b > 0:
                    score = np.dot(vec_a, vec_b) / (norm_a * norm_b)
                    if score > best_score:
                        best_score = score
                        best_match = entry

        if best_match and best_score >= self._similarity_threshold:
            logger.info(f"Semantic cache hit ({best_score:.2f}) for: '{query}' ~= '{best_match['query']}'")
            return best_match
            
        return None

    async def _route_request(self, query: str) -> str:
        """
        Determines the intent of the user query.
        Returns: 'FORMATION', 'MARKET', 'WEATHER', 'GREETING', 'UNKNOWN'
        """
        if not query:
            return "UNKNOWN"

        system_prompt = (
            "You are a router. Classify the user query into: FORMATION, MARKET, WEATHER, GREETING, UNKNOWN.\n"
            "Output ONLY the category name."
        )
        
        # Heuristic shortcut for greetings (latency < 10ms)
        ql = query.lower().strip()
        if ql in ["bonjour", "salut", "hello", "hi", "bonsoir"]:
            return "GREETING"

        try:
            # Use run_in_executor for sync LLM call if the client is sync
            if hasattr(self.llm.chat.completions, "create"): 
                response = await asyncio.to_thread(
                    self.llm.chat.completions.create,
                    model=self.router_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": query},
                    ],
                    max_tokens=10,
                    temperature=0.0
                )
                intent = response.choices[0].message.content.strip().upper()
                for key in ["FORMATION", "MARKET", "WEATHER", "GREETING"]:
                    if key in intent: return key
                return "FORMATION"
            return "FORMATION"
        except Exception as e:
            logger.error(f"Routing error: {e}")
            return "FORMATION"

    async def process_query(self, query: str, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Main entry point for processing a user query.
        """
        start_time = time.time()
        
        # 1. Check Cache
        cache_hit = self._check_cache(query)
        if cache_hit:
            return {
                "answer": cache_hit["response"], 
                "intent": cache_hit.get("intent", "CACHE"),
                "source": "cache", 
                "latency": time.time() - start_time
            }

        # 2. Route Request
        intent = await self._route_request(query)
        
        # 3. Dispatch to Agent
        response_text = ""
        
        if intent == "FORMATION":
            response_text = await self.formation_agent.aask(query, learner_profile=profile)
        
        elif intent == "MARKET":
            response_text = "Désolé, le module marché est en cours de maintenance. Veuillez contacter le service commercial."
            
        elif intent == "WEATHER":
            response_text = "Je ne peux pas encore donner la météo précise, mais il est toujours bon de vérifier les prévisions locales."
            
        elif intent == "GREETING":
            response_text = "Bonjour ! Je suis AgriConnect, votre assistant agricole. Comment puis-je vous aider aujourd'hui ?"
            
        else:
            response_text = await self.formation_agent.aask(query, learner_profile=profile)

        # 4. Update Cache (background task ideally, but sync here is fine for small scale)
        # Only cache if response is valid/long enough
        if len(response_text) > 20: 
            self._cache.append({
                "query": query,
                "response": response_text,
                "intent": intent,
                "timestamp": time.time(),
                "embedding": self._get_embedding(query)
            })
            # Limit cache size
            if len(self._cache) > 1000:
                self._cache = self._cache[-800:] # Keep recent 800

        return {
            "answer": response_text,
            "intent": intent,
            "latency": time.time() - start_time
        }


# Global instance
orchestrator = AgriOrchestrator()
