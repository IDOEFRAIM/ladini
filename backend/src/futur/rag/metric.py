import json
from typing import List, Dict
from .components import get_groq_sdk

class RAGEvaluator:
    def __init__(self):
        self.llm = get_groq_sdk()

    def evaluate_all(self, query: str, context: str, answer: str) -> Dict[str, float]:
        """
        Runs all RAG metrics and returns a dictionary of scores (0.0 to 1.0).
        """
        print(f"Evaluating RAG Triad for query: '{query[:50]}...'")
        
        scores = {
            "context_relevance": self.evaluate_context_relevance(query, context),
            "faithfulness": self.evaluate_faithfulness(query, context, answer),
            "answer_relevance": self.evaluate_answer_relevance(query, answer)
        }
        
        # Calculate overall score (average)
        scores["overall_score"] = sum(scores.values()) / len(scores)
        return scores

    def _call_llm_judge(self, prompt: str) -> float:
        """
        Helper to call LLM and parse a single float score (0-1) or JSON.
        We expect the LLM to return strictly a JSON object with a 'score' field.
        """
        try:
            response = self.llm.chat.completions.create(
                messages=[
                    {"role": "system", "content": "You are an impartial judge for evaluating RAG systems. Output ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.1-8b-instant",
                response_format={"type": "json_object"},
                temperature=0.0
            )
            
            content = response.choices[0].message.content
            if content is None:
                return 0.0
            data = json.loads(content)
            return float(data.get("score", 0.0))
        except Exception as e:
            print(f"Evaluation Error: {e}")
            return 0.0

    def evaluate_context_relevance(self, query: str, context: str) -> float:
        """
        Measures: Quality of Context given Query (P(C|Q))
        Does the retrieved context contain the information needed to answer the query?
        """
        prompt = (
            f"Query: {query}\n"
            f"Retrieved Context: {context}\n\n"
            "Task: Evaluate if the retrieved context contains sufficient information to answer the query. "
            "Ignore whether the answer is present or not, just check the information content matches the topic.\n"
            "Return a JSON object with a 'score' field between 0.0 (irrelevant) and 1.0 (perfectly relevant) and a 'reason' field."
        )
        return self._call_llm_judge(prompt)

    def evaluate_faithfulness(self, query: str, context: str, answer: str) -> float:
        """
        Measures: Quality of Answer given Context (P(A|C))
        Is the answer derived purely from the context? (Hallucination check)
        """
        prompt = (
            f"Context: {context}\n"
            f"Generated Answer: {answer}\n\n"
            "Task: Evaluate if the answer is faithful to the context. "
            "A score of 1.0 means every claim in the answer is supported by the context. "
            "A score of 0.0 means the answer contains hallucinations not found in the text.\n"
            "Return a JSON with 'score' (0.0-1.0) and 'reason'."
        )
        return self._call_llm_judge(prompt)

    def evaluate_answer_relevance(self, query: str, answer: str) -> float:
        """
        Measures: Quality of Answer given Query (P(A|Q))
        Does the answer directly address the user's question?
        """
        prompt = (
            f"Query: {query}\n"
            f"Answer: {answer}\n\n"
            "Task: Evaluate if the answer is relevant and helpful for the query. "
            "It should directly address the user's intent.\n"
            "Return a JSON with 'score' (0.0-1.0) and 'reason'."
        )
        return self._call_llm_judge(prompt)

# Quick test if run directly
if __name__ == "__main__":
    evaluator = RAGEvaluator()
    
    # Mock Data
    q = "Dois-je traiter mes tomates aujourd'hui?"
    c = "Document A: Il est prévu de la pluie aujourd'hui (>10mm). Document B: Ne jamais traiter avant la pluie."
    a = "Non, ne traitez pas aujourd'hui car il va pleuvoir et le traitement serait lessivé."
    
    results = evaluator.evaluate_all(q, c, a)
    print("Evaluation Results:", json.dumps(results, indent=2))