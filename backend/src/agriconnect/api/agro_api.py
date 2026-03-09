from typing import Optional
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agriconnect.agents.formation_agro import FormationAgro

logger = logging.getLogger("agro.api")


class FormationRequest(BaseModel):
    question: str
    profile: Optional[dict] = None


app = FastAPI(title="AgriConnect Formation API")

# Allow CORS for local frontend testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event():
    # Instantiate a single long-lived agent for the server
    try:
        app.state.agent = FormationAgro()
        logger.info("FormationAgro instantiated on startup")
    except Exception as e:
        logger.exception("Could not initialize FormationAgro: %s", e)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/formation")
def formation(req: FormationRequest):
    agent: FormationAgro = getattr(app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not available")

    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty question")

    try:
        answer = agent.ask(q, learner_profile=req.profile)
        return {"question": q, "answer": answer}
    except Exception as exc:
        logger.exception("Error running formation agent: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
