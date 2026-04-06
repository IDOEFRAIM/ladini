from __future__ import annotations

from typing import Dict, Optional

from .institutional_pdf_harvester import InstitutionalPdfHarvester
from .registry import register_scraper


@register_scraper("meteo_burkina", "weather_bulletin")
class MeteoBurkinaScraper:
    """Lightweight Meteo Burkina adapter powered by InstitutionalPdfHarvester."""

    def __init__(self, config: Optional[Dict] = None):
        cfg = dict(config or {})
        cfg.setdefault("base_url", "https://meteoburkina.bf")
        cfg.setdefault(
            "search_urls",
            ["https://meteoburkina.bf/produits/bulletin-agrometeologique-decadaire"],
        )
        cfg.setdefault(
            "include_patterns",
            [r"bulletin", r"situation", r"agrometeo", r"decadaire"],
        )
        self.engine = InstitutionalPdfHarvester(config=cfg)

    def run(self) -> Dict:
        return self.engine.run_harvest()
