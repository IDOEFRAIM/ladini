from formation_agro import FormationAgro
import json
def test_formation_agro():
    agent = FormationAgro()
    sample_question = "Bonjour, peux tu me conseiller sur les meilleurs techniques de semer le riz."
    sample_profile = {"niveau": "debutant", "culture_actuelle": "sorgho"}
    print("Running FormationAgro test for query:\n", sample_question)
    wf = agent.build()
    result = wf.invoke({"user_query": sample_question, "learner_profile": sample_profile})
    print(json.dumps(result, ensure_ascii=False, indent=2))

test_formation_agro()