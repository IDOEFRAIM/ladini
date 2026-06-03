import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from agriconnect.api.routes.market import router as market_router
from agriconnect.api.routes.twilio_webhook import router as twilio_router # 1. Import

logger = logging.getLogger("AgriConnect.API")

app = FastAPI(
    title="AgriConnect MarketCoach API",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Inclus tes routers
app.include_router(market_router, prefix="/api")
app.include_router(twilio_router, prefix="/api") # Ton endpoint sera /api/twilio/webhook

@app.get("/health")
async def health_check():
    return {
        "status": "healthy", 
        "components": ["redis", "celery_worker"]
    }