from typing import Optional
import logging
import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agriconnect.agents.orchestrator import AgriOrchestrator

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
async def startup_event():
    # Instantiate a single long-lived orchestrator for the server
    try:
        app.state.orchestrator = AgriOrchestrator()
        logger.info("AgriOrchestrator instantiated on startup")
    except Exception as e:
        logger.exception("Could not initialize AgriOrchestrator: %s", e)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/formation")
async def formation(req: FormationRequest):
    orchestrator: AgriOrchestrator = getattr(app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")

    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty question")

    try:
        # Delegate to orchestrator which handles routing, caching, and agents
        result = await orchestrator.process_query(q, profile=req.profile)
        
        # Backwards compatibility: ensure 'answer' is string
        if isinstance(result.get("answer"), (dict, list)):
             import json
             answer_text = json.dumps(result["answer"], ensure_ascii=False, default=str)
        else:
             answer_text = str(result["answer"])

        return {
            "question": q,
            "answer": answer_text,
            "metadata": {
                "intent": result.get("intent", "UNKNOWN"),
                "latency": result.get("latency", 0),
                "source": result.get("source", "model")
            }
        }
    except Exception as exc:
        logger.exception("Error running orchestrator: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
