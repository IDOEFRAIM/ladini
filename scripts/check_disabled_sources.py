import os
import sys

sys.path.insert(0, "backend/src")

from agriconnect.services.scraper.scraper_orchestrator import ScraperOrchestrator

orch = ScraperOrchestrator()
print("ENV=", os.getenv("SCRAPER_DISABLED_SOURCES"))
print("DISABLED_COUNT=", len(orch.disabled_sources))
print("DISABLED_SAMPLE=", sorted(list(orch.disabled_sources))[:10])
print("HAS_FAO=", "fao_technical_resources" in orch.disabled_sources)
print("HAS_SIDWAYA=", "sidwaya_actualites" in orch.disabled_sources)
