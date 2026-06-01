import gradio as gr
from agriconnect.agent_test.formation_agro import FormationAgro

# Initialisation
agent = FormationAgro()

async def chat_function(message, history):
    sample_profile = {"niveau": "debutant", "culture_actuelle": "sorgho"}
    
    # 1. Construire le workflow
    wf = agent.build()
    
    # 2. Utiliser ainvoke pour ne pas bloquer l'interface Gradio
    # On passe l'état initial requis
    result = await wf.ainvoke({"user_query": message, "learner_profile": sample_profile})
    
    # 3. Extraction intelligente du texte pour Gradio
    # On vérifie les champs dans l'ordre de priorité
    if isinstance(result, dict):
        text_response = (
            result.get("final_response") or 
            result.get("answer_draft") or 
            str(result) # Fallback en cas de problème
        )
        return text_response
    
    return str(result)

# Interface
demo = gr.ChatInterface(
    fn=chat_function,
    title="AgriConnect - Assistant Formation",
    description="Posez vos questions agronomiques.",
    examples=["Comment faire la rotation des cultures ?", "Quelles sont les maladies du maïs ?"]
)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0",share=True, server_port=7861)