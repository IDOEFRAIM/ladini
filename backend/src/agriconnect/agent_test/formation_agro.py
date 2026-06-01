"""FormationAgro — thin local test harness.

The full Formation logic now lives in `agriconnect.graphs.agents.formation.nodes`.
This file stays as a small executable wrapper for agronome testing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.formation.graph import get_agent_graph
from agriconnect.graphs.agents.formation.nodes import FormationConfig

logger = logging.getLogger("Agent.FormationAgro")


class FormationAgro:
	"""Standalone-friendly wrapper around the official Formation graph."""

	def __init__(self, llm_client: Any = None, learner_profile: Optional[Dict[str, Any]] = None):
		self.config = FormationConfig(llm_client=llm_client, default_profile=learner_profile)
		# Build lazily to keep import/startup lightweight for tests and Gradio.
		self._app = None

	def build(self):
		"""Return the compiled LangGraph app.

		Expected by `agriconnect.agent_test.client` which calls `agent.build()`
		then uses `ainvoke()` to drive the workflow.
		"""
		if self._app is None:
			self._app = get_agent_graph(config=self.config)
		return self._app

	def ask(self, question: str, learner_profile: Optional[Dict[str, Any]] = None) -> str:
		if not question or not question.strip():
			return "Posez votre question de formation."

		state = {
			"user_query": question,
			"learner_profile": learner_profile or self.config.default_profile,
		}
		try:
			app = self.build()
			result = asyncio.run(app.ainvoke(state))
			if isinstance(result, dict):
				if result.get("final_response"):
					return str(result["final_response"])
				if result.get("answer_draft"):
					return str(result["answer_draft"])
				return json.dumps(result, indent=2, ensure_ascii=False, default=str)
			return str(result)
		except Exception as exc:
			logger.exception("FormationAgro workflow error: %s", exc)
			return f"Erreur interne: {exc}"

	# Backward compat
	handle_question = ask


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	agent = FormationAgro()
	sample_question = "Bonjour, je veux des conseils sur le semis du maïs."
	sample_profile = {"niveau": "debutant", "culture_actuelle": "Maïs", "zone": "Centre", "superficie": 1.0}
	print("Running FormationAgro test for query:\n", sample_question)
	app = agent.build()
	result = asyncio.run(app.ainvoke({"user_query": sample_question, "learner_profile": sample_profile}))
	print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
