from fastapi import FastAPI, HTTPException,Form
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
from futur.agent_test.formation_agro import FormationAgro

# Variable globale pour l'instance de l'agent
ml_models = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Chargement unique : l'objet agent contient maintenant toute la logique build()
    print("Initialisation de l'agent FormationAgro...")
    ml_models["formation_agent"] = FormationAgro() 
    yield
    # Nettoyage
    ml_models.clear()
    print("Agent déchargé.")

app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], # Ton frontend Next.js
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.post("/ask-formation")
async def ask_formation(
    query: str = Form(...),
    culture: str = Form("sorgho"),
    niveau: str = Form("debutant"),
    zone: str = Form("Centre"),
    superficie: float = Form(1.0)
):
    agent = ml_models.get("formation_agent")
    if not agent:
        raise HTTPException(status_code=500, detail="Agent non initialisé")
    
    # Construction du profil dynamique à partir des entrées frontend
    sample_profile = {
        "niveau": niveau,
        "culture_actuelle": culture,
        "zone": zone,
        "superficie": superficie
    }
    
    # Utilisation de workflow (Optimisation : assure-toi que 
    # self.wf soit défini dans __init__ pour ne pas re-build à chaque fois)
    workflow = agent.build() 
    
    try:
        result = await workflow.ainvoke({
            "user_query": query, 
            "learner_profile": sample_profile
        })
        
        # Extraction du texte
        text_response = result.get("final_response") or result.get("answer_draft") or str(result)
        
        return {"response": text_response}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)